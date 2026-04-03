"""OpenClaw Local Control Agent -- manages local OpenClaw gateway instances."""

import asyncio
import base64
import hashlib
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx
import psutil
import websockets
from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="OpenClaw Local Agent")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

def _load_instances_config() -> dict:
    """Load instance definitions from config.json.

    Copy config.example.json to config.json and edit for your setup.
    """
    config_path = Path(os.getenv(
        "OPENCLAW_AGENT_CONFIG",
        str(Path(__file__).parent / "config.json"),
    ))
    if not config_path.exists():
        print(f"ERROR: Config file not found: {config_path}")
        print("Copy config.example.json to config.json and customise it.")
        raise SystemExit(1)
    with open(config_path, encoding="utf-8") as fh:
        raw = json.load(fh)
    instances = {}
    for instance_id, cfg in raw.get("instances", {}).items():
        instances[instance_id] = {
            "port": int(cfg["port"]),
            "state_dir": Path(cfg["state_dir"]),
            "start_script": Path(cfg["start_script"]),
            "start_cmd": cfg["start_cmd"],
            "display_name": cfg.get("display_name", instance_id),
        }
    return instances


INSTANCES = _load_instances_config()

LOCAL_GATEWAY_HOST = os.getenv("OPENCLAW_PUBLIC_HOST", "").strip()
LOCAL_GATEWAY_SCHEME = os.getenv("OPENCLAW_PUBLIC_SCHEME", "http").strip() or "http"
INSTANCE_GATEWAY_URL_ENV = {
    "jay": "OPENCLAW_JAY_GATEWAY_URL",
    "jayhova": "OPENCLAW_JAYHOVA_GATEWAY_URL",
    "jarvis": "OPENCLAW_JARVIS_GATEWAY_URL",
}
PROXY_BASE_PREFIX = "/proxy"
PROXY_HEADER_BASE_PATH = "x-openclaw-proxy-base-path"
HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}


class ModelUpdateRequest(BaseModel):
    model: str


def _load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _normalize_base_path(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if not text.startswith("/"):
        text = f"/{text}"
    return text.rstrip("/") or "/"


def _canonical_model_key(provider: str, model_id: str) -> str:
    model_id = model_id.strip()
    prefix = f"{provider}/"
    return model_id if model_id.startswith(prefix) else f"{prefix}{model_id}"


def _read_instance_config(instance_id: str) -> dict:
    return _load_json(INSTANCES[instance_id]["state_dir"] / "openclaw.json")


def _read_agent_model_catalog(instance_id: str) -> dict:
    return _load_json(INSTANCES[instance_id]["state_dir"] / "agents" / "main" / "agent" / "models.json")


def _build_gateway_url(instance_id: str, cfg: dict) -> str | None:
    override = os.getenv(INSTANCE_GATEWAY_URL_ENV.get(instance_id, ""), "").strip()
    if override:
        return override
    if not LOCAL_GATEWAY_HOST:
        return None
    base_path = _normalize_base_path(cfg.get("gateway", {}).get("controlUi", {}).get("basePath"))
    path = "/" if not base_path or base_path == "/" else f"{base_path}/"
    return urlunsplit((LOCAL_GATEWAY_SCHEME, f"{LOCAL_GATEWAY_HOST}:{INSTANCES[instance_id]['port']}", path, "", ""))


def _proxy_base_path(instance_id: str) -> str:
    return f"{PROXY_BASE_PREFIX}/{instance_id}"


def _instance_gateway_details(instance_id: str) -> dict:
    cfg = _read_instance_config(instance_id)
    gateway_cfg = cfg.get("gateway", {})
    auth_cfg = gateway_cfg.get("auth", {})
    base_path = _normalize_base_path(gateway_cfg.get("controlUi", {}).get("basePath")) or "/"
    auth_mode = str(auth_cfg.get("mode") or "").strip()
    token = str(auth_cfg.get("token") or "").strip() or None
    return {
        "config": cfg,
        "base_path": base_path,
        "http_base_url": urlunsplit(("http", f"127.0.0.1:{INSTANCES[instance_id]['port']}", base_path, "", "")),
        "ws_url": urlunsplit(("ws", f"127.0.0.1:{INSTANCES[instance_id]['port']}", base_path, "", "")),
        "auth_mode": auth_mode,
        "auth_token": token if auth_mode == "token" else None,
    }


def _join_proxy_path(base_path: str, remainder: str) -> str:
    trimmed_base = base_path.rstrip("/")
    trimmed_remainder = remainder.lstrip("/")
    if not trimmed_base or trimmed_base == "":
        return f"/{trimmed_remainder}" if trimmed_remainder else "/"
    if not trimmed_remainder:
        return f"{trimmed_base}/"
    return f"{trimmed_base}/{trimmed_remainder}"


def _filter_proxy_request_headers(headers) -> dict[str, str]:
    forwarded: dict[str, str] = {}
    for key, value in headers.items():
        if key.lower() in HOP_BY_HOP_HEADERS:
            continue
        forwarded[key] = value
    return forwarded


def _rewrite_proxy_location(location: str, upstream_base_path: str, proxy_base_path: str) -> str:
    if not location:
        return location
    parsed = urlsplit(location)
    path = parsed.path or ""
    if upstream_base_path == "/":
        if path == "/":
            new_path = f"{proxy_base_path}/"
        elif path.startswith("/"):
            new_path = _join_proxy_path(proxy_base_path, path)
        else:
            return location
    else:
        upstream_root = upstream_base_path.rstrip("/")
        if path == upstream_root:
            new_path = f"{proxy_base_path}/"
        elif path.startswith(f"{upstream_root}/"):
            suffix = path[len(upstream_root) :]
            new_path = f"{proxy_base_path}{suffix}"
        else:
            return location
    return urlunsplit((parsed.scheme, parsed.netloc, new_path, parsed.query, parsed.fragment))


def _rewrite_bootstrap_payload(payload: dict, upstream_base_path: str, proxy_base_path: str) -> dict:
    rewritten = dict(payload)
    original_base = str(rewritten.get("basePath") or upstream_base_path or "").strip() or upstream_base_path
    rewritten["basePath"] = proxy_base_path
    avatar = rewritten.get("assistantAvatar")
    if isinstance(avatar, str):
        original_base = _normalize_base_path(original_base) or "/"
        if original_base == "/" and avatar.startswith("/"):
            rewritten["assistantAvatar"] = _join_proxy_path(proxy_base_path, avatar)
        elif original_base != "/" and avatar.startswith(original_base):
            suffix = avatar[len(original_base) :]
            rewritten["assistantAvatar"] = f"{proxy_base_path}{suffix}"
    return rewritten


def _inject_control_ui_bootstrap_html(html_text: str, proxy_base_path: str, auth_token: str | None) -> str:
    script = (
        f"window.__OPENCLAW_CONTROL_UI_BASE_PATH__ = {json.dumps(proxy_base_path)};"
        "try {"
        "localStorage.removeItem('openclaw.device.auth.v1');"
        "const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';"
        f"const scope = `${{proto}}//${{location.host}}{proxy_base_path}`;"
        f"sessionStorage.setItem(`openclaw.control.token.v1:${{scope}}`, {json.dumps(auth_token or '')});"
        # Force-clear stale localStorage settings so the Control UI does not
        # reuse a gatewayUrl saved from a different instance.
        "localStorage.removeItem('openclaw.control.settings.v1:default');"
        "localStorage.removeItem('openclaw.control.settings.v1');"
        # Also clear the scope-specific key so the fresh basePath wins.
        f"localStorage.removeItem(`openclaw.control.settings.v1:${{scope}}`);"
        "} catch {}"
    )
    tag = f"<script>{script}</script>"
    if "<head>" in html_text:
        return html_text.replace("<head>", f"<head>{tag}", 1)
    return f"{tag}{html_text}"


def _augment_csp_for_inline_script(csp: str | None, script_text: str) -> str | None:
    if not csp:
        return csp
    digest = base64.b64encode(hashlib.sha256(script_text.encode("utf-8")).digest()).decode("ascii")
    hash_token = f"'sha256-{digest}'"
    directives = [part.strip() for part in csp.split(";") if part.strip()]
    for index, directive in enumerate(directives):
        if not directive.startswith("script-src"):
            continue
        if hash_token in directive:
            return "; ".join(directives)
        directives[index] = f"{directive} {hash_token}"
        return "; ".join(directives)
    directives.append(f"script-src 'self' {hash_token}")
    return "; ".join(directives)


def _inject_connect_token(raw_message: str, auth_token: str | None) -> str:
    if not auth_token:
        return raw_message
    try:
        parsed = json.loads(raw_message)
    except json.JSONDecodeError:
        return raw_message
    if not isinstance(parsed, dict):
        return raw_message
    if parsed.get("type") != "req" or parsed.get("method") != "connect":
        return raw_message
    params = parsed.get("params")
    if not isinstance(params, dict):
        return raw_message
    auth = params.get("auth")
    if auth is None:
        auth = {}
    if not isinstance(auth, dict):
        return raw_message
    token = str(auth.get("token") or "").strip()
    password = str(auth.get("password") or "").strip()
    if token or password:
        return raw_message
    auth["token"] = auth_token
    params["auth"] = auth
    parsed["params"] = params
    return json.dumps(parsed, separators=(",", ":"))


def _origin_for_websocket_url(ws_url: str) -> str:
    parsed = urlsplit(ws_url)
    scheme = "https" if parsed.scheme == "wss" else "http"
    return urlunsplit((scheme, parsed.netloc, "", "", ""))


async def _proxy_http_request(
    request: Request,
    instance_id: str,
    path: str,
    *,
    proxy_base_override: str | None = None,
) -> Response:
    if instance_id not in INSTANCES:
        raise HTTPException(404, f"Unknown instance: {instance_id}")
    gateway = _instance_gateway_details(instance_id)
    proxy_base_path = proxy_base_override or request.headers.get(PROXY_HEADER_BASE_PATH) or _proxy_base_path(instance_id)
    upstream_path = _join_proxy_path(gateway["base_path"], path)
    upstream_url = urlunsplit(("http", f"127.0.0.1:{INSTANCES[instance_id]['port']}", upstream_path, request.url.query, ""))
    body = await request.body()
    forwarded_headers = _filter_proxy_request_headers(request.headers)
    # Inject auth token for upstream gateways that require it
    if gateway.get("auth_token"):
        forwarded_headers["Authorization"] = f"Bearer {gateway['auth_token']}"
    async with httpx.AsyncClient(follow_redirects=False, timeout=30.0) as client:
        upstream = await client.request(
            request.method,
            upstream_url,
            content=body,
            headers=forwarded_headers,
        )

    response_headers: dict[str, str] = {}
    for key, value in upstream.headers.items():
        lower = key.lower()
        if lower in HOP_BY_HOP_HEADERS:
            continue
        if lower == "location":
            value = _rewrite_proxy_location(value, gateway["base_path"], proxy_base_path)
        response_headers[key] = value

    content_type = upstream.headers.get("content-type", "")
    is_bootstrap = path.strip("/") == "__openclaw/control-ui-config.json"
    if is_bootstrap:
        try:
            payload = upstream.json()
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            content = json.dumps(
                _rewrite_bootstrap_payload(payload, gateway["base_path"], proxy_base_path),
                separators=(",", ":"),
            ).encode("utf-8")
            response_headers["content-type"] = "application/json; charset=utf-8"
            return Response(content=content, status_code=upstream.status_code, headers=response_headers)

    if "text/html" in content_type.lower():
        html_text = upstream.content.decode(upstream.encoding or "utf-8", errors="replace")
        script_text = (
            f"window.__OPENCLAW_CONTROL_UI_BASE_PATH__ = {json.dumps(proxy_base_path)};"
            "try {"
            "localStorage.removeItem('openclaw.device.auth.v1');"
            "const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';"
            f"const scope = `${{proto}}//${{location.host}}{proxy_base_path}`;"
            f"sessionStorage.setItem(`openclaw.control.token.v1:${{scope}}`, {json.dumps(gateway['auth_token'] or '')});"
            "localStorage.removeItem('openclaw.control.settings.v1:default');"
            "localStorage.removeItem('openclaw.control.settings.v1');"
            f"localStorage.removeItem(`openclaw.control.settings.v1:${{scope}}`);"
            "} catch {}"
        )
        injected = _inject_control_ui_bootstrap_html(html_text, proxy_base_path, gateway["auth_token"])
        csp = response_headers.get("Content-Security-Policy") or response_headers.get("content-security-policy")
        rewritten_csp = _augment_csp_for_inline_script(csp, script_text)
        if rewritten_csp:
            response_headers["Content-Security-Policy"] = rewritten_csp
            response_headers.pop("content-security-policy", None)
        return Response(content=injected.encode("utf-8"), status_code=upstream.status_code, headers=response_headers)

    return Response(content=upstream.content, status_code=upstream.status_code, headers=response_headers)


async def _relay_browser_to_upstream(browser: WebSocket, upstream, auth_token: str | None) -> None:
    injected = False
    while True:
        message = await browser.receive()
        message_type = message.get("type")
        if message_type == "websocket.disconnect":
            await upstream.close()
            return
        text = message.get("text")
        if text is not None:
            if not injected:
                text = _inject_connect_token(text, auth_token)
                injected = True
            await upstream.send(text)
            continue
        data = message.get("bytes")
        if data is not None:
            await upstream.send(data)


async def _relay_upstream_to_browser(browser: WebSocket, upstream) -> None:
    async for message in upstream:
        if isinstance(message, bytes):
            await browser.send_bytes(message)
        else:
            await browser.send_text(message)


async def _proxy_websocket_request(websocket: WebSocket, instance_id: str) -> None:
    if instance_id not in INSTANCES:
        await websocket.close(code=4404)
        return
    gateway = _instance_gateway_details(instance_id)
    await websocket.accept()
    try:
        async with websockets.connect(
            gateway["ws_url"],
            open_timeout=10,
            origin=_origin_for_websocket_url(gateway["ws_url"]),
        ) as upstream:
            browser_task = asyncio.create_task(
                _relay_browser_to_upstream(websocket, upstream, gateway["auth_token"])
            )
            upstream_task = asyncio.create_task(_relay_upstream_to_browser(websocket, upstream))
            done, pending = await asyncio.wait(
                {browser_task, upstream_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            for task in done:
                exc = task.exception()
                if exc:
                    raise exc
    except WebSocketDisconnect:
        return
    except Exception:
        await websocket.close(code=1011)


def _collect_available_models(cfg: dict, catalog_cfg: dict) -> list[dict]:
    model_cfg = cfg.get("agents", {}).get("defaults", {}).get("model", {})
    if isinstance(model_cfg, str):
        current_model = model_cfg.strip()
    else:
        current_model = str(model_cfg.get("primary", "") or "").strip()
    configured_models = cfg.get("agents", {}).get("defaults", {}).get("models", {})
    if not isinstance(configured_models, dict):
        configured_models = {}
    discovered_providers = catalog_cfg.get("providers", {})
    direct_providers = cfg.get("models", {}).get("providers", {})

    entries: dict[str, dict] = {}

    def ensure_entry(model_key: str, *, name: str | None = None, alias: str | None = None) -> None:
        provider = model_key.split("/", 1)[0] if "/" in model_key else ""
        entry = entries.setdefault(
            model_key,
            {
                "id": model_key,
                "provider": provider,
                "name": model_key,
                "alias": None,
            },
        )
        if name and entry["name"] == model_key:
            entry["name"] = name
        if alias and not entry["alias"]:
            entry["alias"] = alias

    for raw_key, meta in configured_models.items():
        model_key = str(raw_key or "").strip()
        if not model_key:
            continue
        alias = meta.get("alias") if isinstance(meta, dict) else None
        ensure_entry(model_key, alias=str(alias).strip() if alias else None)

    for provider, provider_cfg in (direct_providers or {}).items():
        for model in provider_cfg.get("models", []) or []:
            model_id = str(model.get("id") or "").strip()
            if not model_id:
                continue
            ensure_entry(_canonical_model_key(provider, model_id), name=str(model.get("name") or "").strip() or None)

    for provider, provider_cfg in (discovered_providers or {}).items():
        for model in provider_cfg.get("models", []) or []:
            model_id = str(model.get("id") or "").strip()
            if not model_id:
                continue
            ensure_entry(_canonical_model_key(provider, model_id), name=str(model.get("name") or "").strip() or None)

    if current_model:
        ensure_entry(current_model)

    ordered = sorted(entries.values(), key=lambda item: (item["id"] != current_model, item["provider"], item["name"].lower()))
    for item in ordered:
        item["selected"] = item["id"] == current_model
    return ordered


def _build_instance_metadata(instance_id: str) -> dict:
    cfg = _read_instance_config(instance_id)
    model_cfg = cfg.get("agents", {}).get("defaults", {}).get("model", {})
    current_model = ""
    fallbacks: list[str] = []
    if isinstance(model_cfg, str):
        current_model = model_cfg.strip()
    elif isinstance(model_cfg, dict):
        current_model = str(model_cfg.get("primary") or "").strip()
        fallbacks = [str(item).strip() for item in model_cfg.get("fallbacks", []) if str(item).strip()]

    return {
        "current_model": current_model or None,
        "fallback_models": fallbacks,
        "available_models": _collect_available_models(cfg, _read_agent_model_catalog(instance_id)),
        "gateway_url": _build_gateway_url(instance_id, cfg),
        "gateway_base_path": _normalize_base_path(cfg.get("gateway", {}).get("controlUi", {}).get("basePath")) or "/",
    }


def _apply_primary_model(instance_id: str, model_key: str) -> dict:
    cfg_path = INSTANCES[instance_id]["state_dir"] / "openclaw.json"
    cfg = _read_instance_config(instance_id)
    agents = cfg.setdefault("agents", {})
    defaults = agents.setdefault("defaults", {})
    existing_model_cfg = defaults.get("model", {})
    fallbacks: list[str] = []
    if isinstance(existing_model_cfg, dict):
        fallbacks = [str(item).strip() for item in existing_model_cfg.get("fallbacks", []) if str(item).strip()]
    defaults["model"] = {"primary": model_key, **({"fallbacks": fallbacks} if fallbacks else {})}
    model_registry = defaults.get("models")
    if not isinstance(model_registry, dict):
        model_registry = {}
        defaults["models"] = model_registry
    existing_entry = model_registry.get(model_key)
    model_registry[model_key] = existing_entry if isinstance(existing_entry, dict) else {}
    meta = cfg.setdefault("meta", {})
    meta["lastTouchedAt"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    _write_json(cfg_path, cfg)
    return _build_instance_metadata(instance_id)


def _find_instance_processes(port: int) -> list[psutil.Process]:
    """Find all node.exe processes running an OpenClaw gateway on the given port."""
    results = []
    for proc in psutil.process_iter(["pid", "name", "cmdline", "create_time"]):
        try:
            if proc.info["name"] and proc.info["name"].lower() == "node.exe":
                cmdline = " ".join(proc.info["cmdline"] or [])
                if "openclaw" in cmdline.lower() and f"--port {port}" in cmdline:
                    results.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return results


def _get_instance_status(instance_id: str) -> dict:
    cfg = INSTANCES[instance_id]
    procs = _find_instance_processes(cfg["port"])
    metadata = _build_instance_metadata(instance_id)
    if not procs:
        return {
            "id": instance_id,
            "name": cfg["display_name"],
            "status": "stopped",
            "port": cfg["port"],
            "location": "local",
            **metadata,
        }

    # Use the oldest process as the "main" one for uptime
    main_proc = min(procs, key=lambda p: p.info["create_time"])
    try:
        mem_mb = sum(p.memory_info().rss for p in procs) / (1024 * 1024)
        uptime_seconds = time.time() - main_proc.info["create_time"]
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        mem_mb = 0
        uptime_seconds = 0

    return {
        "id": instance_id,
        "name": cfg["display_name"],
        "status": "running",
        "port": cfg["port"],
        "location": "local",
        "pids": [p.pid for p in procs],
        "memory_mb": round(mem_mb, 1),
        "uptime_seconds": round(uptime_seconds),
        **metadata,
    }


@app.get("/status")
def get_status():
    return {iid: _get_instance_status(iid) for iid in INSTANCES}


@app.get("/status/{instance_id}")
def get_instance_status(instance_id: str):
    if instance_id not in INSTANCES:
        raise HTTPException(404, f"Unknown instance: {instance_id}")
    return _get_instance_status(instance_id)


@app.post("/start/{instance_id}")
def start_instance(instance_id: str):
    if instance_id not in INSTANCES:
        raise HTTPException(404, f"Unknown instance: {instance_id}")

    cfg = INSTANCES[instance_id]
    procs = _find_instance_processes(cfg["port"])
    if procs:
        return {"message": f"{cfg['display_name']} is already running", "status": "running"}

    if not cfg["start_script"].exists():
        raise HTTPException(500, f"Start script not found: {cfg['start_script']}")

    # Launch detached -- the script handles its own respawn loop
    subprocess.Popen(
        cfg["start_cmd"],
        cwd=str(cfg["state_dir"]),
        creationflags=subprocess.CREATE_NO_WINDOW,
        close_fds=True,
    )

    return {"message": f"{cfg['display_name']} starting", "status": "starting"}


@app.post("/stop/{instance_id}")
def stop_instance(instance_id: str):
    if instance_id not in INSTANCES:
        raise HTTPException(404, f"Unknown instance: {instance_id}")

    cfg = INSTANCES[instance_id]
    procs = _find_instance_processes(cfg["port"])
    if not procs:
        return {"message": f"{cfg['display_name']} is already stopped", "status": "stopped"}

    killed = []
    for proc in procs:
        try:
            # Kill the process tree (parent + children)
            children = proc.children(recursive=True)
            for child in children:
                child.kill()
            proc.kill()
            killed.append(proc.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
            pass  # already gone or permission issue

    # Also kill any PowerShell respawn wrapper that might restart the process
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            if proc.info["name"] and proc.info["name"].lower() in ("powershell.exe", "pwsh.exe"):
                cmdline = " ".join(proc.info["cmdline"] or [])
                state_dir_name = cfg["state_dir"].name  # e.g. ".openclaw-jayhova"
                if state_dir_name in cmdline and "start-gateway" in cmdline.lower():
                    proc.kill()
                    killed.append(proc.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    return {"message": f"{cfg['display_name']} stopped", "killed_pids": killed, "status": "stopped"}


@app.post("/model/{instance_id}")
def set_instance_model(instance_id: str, payload: ModelUpdateRequest):
    if instance_id not in INSTANCES:
        raise HTTPException(404, f"Unknown instance: {instance_id}")
    model_key = payload.model.strip()
    if not model_key or "/" not in model_key:
        raise HTTPException(400, "Model must be in provider/model format")
    metadata = _apply_primary_model(instance_id, model_key)
    return {
        "message": f"{INSTANCES[instance_id]['display_name']} model updated",
        "model": metadata["current_model"],
        "available_models": metadata["available_models"],
    }


@app.api_route("/proxy/{instance_id}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
@app.api_route("/proxy/{instance_id}/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def proxy_instance_http(request: Request, instance_id: str, path: str = ""):
    return await _proxy_http_request(request, instance_id, path)


@app.websocket("/proxy/{instance_id}")
@app.websocket("/proxy/{instance_id}/")
@app.websocket("/proxy/{instance_id}/{path:path}")
async def proxy_instance_websocket(websocket: WebSocket, instance_id: str):
    await _proxy_websocket_request(websocket, instance_id)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=9830, access_log=False)
