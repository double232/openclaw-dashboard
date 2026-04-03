"""OpenClaw Dashboard Backend -- aggregates status from local agent + cloud systemd."""

import asyncio
import base64
import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx
import websockets
from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

LOCAL_AGENT_URL = os.getenv("LOCAL_AGENT_URL", "http://127.0.0.1:9830")
LOCAL_AGENT_TIMEOUT = 5.0
_local_agent_netloc = urlsplit(LOCAL_AGENT_URL).netloc or "127.0.0.1:9830"
JAYCLOUD_CONFIG_PATH = Path(os.getenv("JAYCLOUD_CONFIG_PATH", "/root/.openclaw/openclaw.json"))
JAYCLOUD_GATEWAY_URL = os.getenv("JAYCLOUD_GATEWAY_URL", "").strip() or None
LOCAL_AGENT_PROXY_HEADER = "x-openclaw-proxy-base-path"
GATEWAY_ROUTE_PREFIX = "/gateway"
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

app = FastAPI(title="OpenClaw Dashboard")


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


def _read_jaycloud_catalog() -> dict:
    return _load_json(JAYCLOUD_CONFIG_PATH.parent / "agents" / "main" / "agent" / "models.json")


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


def _build_jaycloud_metadata() -> dict:
    cfg = _load_json(JAYCLOUD_CONFIG_PATH)
    model_cfg = cfg.get("agents", {}).get("defaults", {}).get("model", {})
    current_model = ""
    fallbacks: list[str] = []
    if isinstance(model_cfg, str):
        current_model = model_cfg.strip()
    elif isinstance(model_cfg, dict):
        current_model = str(model_cfg.get("primary") or "").strip()
        fallbacks = [str(item).strip() for item in model_cfg.get("fallbacks", []) if str(item).strip()]

    base_path = _normalize_base_path(cfg.get("gateway", {}).get("controlUi", {}).get("basePath"))
    gateway_url = JAYCLOUD_GATEWAY_URL
    if not gateway_url and base_path:
        gateway_url = urlunsplit(("https", os.getenv("DASHBOARD_HOST", "localhost:8860"), f"{base_path}/", "", ""))

    return {
        "current_model": current_model or None,
        "fallback_models": fallbacks,
        "available_models": _collect_available_models(cfg, _read_jaycloud_catalog()),
        "gateway_url": gateway_url,
        "gateway_base_path": base_path or "/",
    }


def _dashboard_gateway_path(instance_id: str) -> str:
    return f"{GATEWAY_ROUTE_PREFIX}/{instance_id}"


def _local_agent_public_gateway_url(instance_id: str) -> str:
    parsed = urlsplit(LOCAL_AGENT_URL)
    path = f"/proxy/{instance_id}/"
    return urlunsplit((parsed.scheme or "http", parsed.netloc, path, "", ""))


def _jaycloud_gateway_details() -> dict:
    cfg = _load_json(JAYCLOUD_CONFIG_PATH)
    gateway_cfg = cfg.get("gateway", {})
    auth_cfg = gateway_cfg.get("auth", {})
    base_path = _normalize_base_path(gateway_cfg.get("controlUi", {}).get("basePath")) or "/"
    auth_mode = str(auth_cfg.get("mode") or "").strip()
    token = str(auth_cfg.get("token") or "").strip() or None
    return {
        "base_path": base_path,
        "http_base_url": urlunsplit(("http", "127.0.0.1:18789", base_path, "", "")),
        "ws_url": urlunsplit(("ws", "127.0.0.1:18789", base_path, "", "")),
        "auth_token": token if auth_mode == "token" else None,
    }


def _join_proxy_path(base_path: str, remainder: str) -> str:
    trimmed_base = base_path.rstrip("/")
    trimmed_remainder = remainder.lstrip("/")
    if not trimmed_base:
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
        "const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';"
        f"const scope = `${{proto}}//${{location.host}}{proxy_base_path}`;"
        f"sessionStorage.setItem(`openclaw.control.token.v1:${{scope}}`, {json.dumps(auth_token or '')});"
        # Force-clear stale localStorage settings so the Control UI does not
        # reuse a gatewayUrl saved from a different instance.
        "localStorage.removeItem('openclaw.control.settings.v1:default');"
        "localStorage.removeItem('openclaw.control.settings.v1');"
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


def _local_agent_proxy_url(instance_id: str, path: str, query: str) -> str:
    upstream_path = f"/proxy/{instance_id}"
    if path:
        upstream_path = f"{upstream_path}/{path.lstrip('/')}"
    return urlunsplit(("http", _local_agent_netloc, upstream_path, query, ""))


async def _proxy_local_http(request: Request, instance_id: str, path: str) -> Response:
    proxy_base_path = _dashboard_gateway_path(instance_id)
    upstream_url = _local_agent_proxy_url(instance_id, path, request.url.query)
    body = await request.body()
    headers = _filter_proxy_request_headers(request.headers)
    headers[LOCAL_AGENT_PROXY_HEADER] = proxy_base_path
    async with httpx.AsyncClient(follow_redirects=False, timeout=30.0) as client:
        upstream = await client.request(request.method, upstream_url, content=body, headers=headers)

    response_headers: dict[str, str] = {}
    for key, value in upstream.headers.items():
        if key.lower() in HOP_BY_HOP_HEADERS:
            continue
        response_headers[key] = value
    return Response(content=upstream.content, status_code=upstream.status_code, headers=response_headers)


async def _proxy_jaycloud_http(request: Request, path: str) -> Response:
    proxy_base_path = _dashboard_gateway_path("jaycloud")
    gateway = _jaycloud_gateway_details()
    upstream_path = _join_proxy_path(gateway["base_path"], path)
    upstream_url = urlunsplit(("http", "127.0.0.1:18789", upstream_path, request.url.query, ""))
    body = await request.body()
    async with httpx.AsyncClient(follow_redirects=False, timeout=30.0) as client:
        upstream = await client.request(
            request.method,
            upstream_url,
            content=body,
            headers=_filter_proxy_request_headers(request.headers),
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


async def _proxy_jaycloud_websocket(websocket: WebSocket) -> None:
    gateway = _jaycloud_gateway_details()
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


async def _proxy_local_websocket(websocket: WebSocket, instance_id: str) -> None:
    await websocket.accept()
    ws_url = urlunsplit(("ws", _local_agent_netloc, f"/proxy/{instance_id}", "", ""))
    try:
        async with websockets.connect(
            ws_url,
            open_timeout=10,
            origin=_origin_for_websocket_url(ws_url),
        ) as upstream:
            browser_task = asyncio.create_task(_relay_browser_to_upstream(websocket, upstream, None))
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


def _apply_jaycloud_primary_model(model_key: str) -> dict:
    cfg = _load_json(JAYCLOUD_CONFIG_PATH)
    if not cfg:
        raise HTTPException(500, f"JayCloud config not found: {JAYCLOUD_CONFIG_PATH}")
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
    _write_json(JAYCLOUD_CONFIG_PATH, cfg)
    return _build_jaycloud_metadata()


async def _get_jaycloud_status() -> dict:
    """Get JayCloud status from local systemd service."""
    try:
        result = subprocess.run(
            ["systemctl", "is-active", "openclaw-jay"],
            capture_output=True, text=True, timeout=5,
        )
        is_active = result.stdout.strip() == "active"
    except Exception:
        return {
            "id": "jaycloud",
            "name": "JayCloud",
            "status": "unreachable",
            "port": 18789,
            "location": "cloud",
            **{
                **_build_jaycloud_metadata(),
                "gateway_url": f"{_dashboard_gateway_path('jaycloud')}/",
                "gateway_base_path": _dashboard_gateway_path("jaycloud"),
            },
        }

    info = {
        "id": "jaycloud",
        "name": "JayCloud",
        "status": "running" if is_active else "stopped",
        "port": 18789,
        "location": "cloud",
        **{
            **_build_jaycloud_metadata(),
            "gateway_url": f"{_dashboard_gateway_path('jaycloud')}/",
            "gateway_base_path": _dashboard_gateway_path("jaycloud"),
        },
    }

    if is_active:
        try:
            result = subprocess.run(
                ["systemctl", "show", "openclaw-jay",
                 "--property=ActiveEnterTimestamp,MemoryCurrent,MainPID"],
                capture_output=True, text=True, timeout=5,
            )
            props = {}
            for line in result.stdout.strip().splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    props[k] = v

            if props.get("MainPID") and props["MainPID"] != "0":
                info["pids"] = [int(props["MainPID"])]

            if props.get("MemoryCurrent") and props["MemoryCurrent"] != "[not set]":
                try:
                    info["memory_mb"] = round(int(props["MemoryCurrent"]) / (1024 * 1024), 1)
                except ValueError:
                    pass

            if props.get("ActiveEnterTimestamp"):
                from datetime import datetime, timezone
                try:
                    ts_str = props["ActiveEnterTimestamp"].strip()
                    # systemd format: "Wed 2026-04-01 03:35:55 UTC"
                    ts = datetime.strptime(ts_str, "%a %Y-%m-%d %H:%M:%S %Z")
                    ts = ts.replace(tzinfo=timezone.utc)
                    uptime = (datetime.now(timezone.utc) - ts).total_seconds()
                    info["uptime_seconds"] = round(max(0, uptime))
                except (ValueError, TypeError):
                    pass
        except Exception:
            pass

    return info


async def _get_local_statuses() -> dict:
    """Get status of all local instances from the Windows agent."""
    try:
        async with httpx.AsyncClient(timeout=LOCAL_AGENT_TIMEOUT) as client:
            resp = await client.get(f"{LOCAL_AGENT_URL}/status")
            resp.raise_for_status()
            payload = resp.json()
            for instance_id, info in payload.items():
                if not isinstance(info, dict):
                    continue
                info["gateway_url"] = f"{_dashboard_gateway_path(instance_id)}/"
                info["gateway_base_path"] = _dashboard_gateway_path(instance_id)
            return payload
    except Exception:
        return {
            inst: {
                "id": inst,
                "name": name,
                "status": "unreachable",
                "location": "local",
                "gateway_url": f"{_dashboard_gateway_path(inst)}/",
                "gateway_base_path": _dashboard_gateway_path(inst),
            }
            for inst, name in [("jay", "Jay"), ("jayhova", "JayHova"), ("jarvis", "JARVIS")]
        }


@app.get("/api/status")
async def get_all_status():
    local_task = asyncio.create_task(_get_local_statuses())
    cloud_task = asyncio.create_task(_get_jaycloud_status())
    local, cloud = await asyncio.gather(local_task, cloud_task)
    local["jaycloud"] = cloud
    return local


@app.post("/api/{instance_id}/start")
async def start_instance(instance_id: str):
    if instance_id == "jaycloud":
        try:
            result = subprocess.run(
                ["systemctl", "start", "openclaw-jay"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode != 0:
                raise HTTPException(500, f"systemctl start failed: {result.stderr}")
            return {"message": "JayCloud starting", "status": "starting"}
        except subprocess.TimeoutExpired:
            raise HTTPException(500, "systemctl start timed out")

    try:
        async with httpx.AsyncClient(timeout=LOCAL_AGENT_TIMEOUT) as client:
            resp = await client.post(f"{LOCAL_AGENT_URL}/start/{instance_id}")
            resp.raise_for_status()
            return resp.json()
    except httpx.ConnectError:
        raise HTTPException(502, "Local agent unreachable")
    except httpx.HTTPStatusError as e:
        raise HTTPException(e.response.status_code, e.response.text)


@app.post("/api/{instance_id}/stop")
async def stop_instance(instance_id: str):
    if instance_id == "jaycloud":
        try:
            result = subprocess.run(
                ["systemctl", "stop", "openclaw-jay"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode != 0:
                raise HTTPException(500, f"systemctl stop failed: {result.stderr}")
            return {"message": "JayCloud stopped", "status": "stopped"}
        except subprocess.TimeoutExpired:
            raise HTTPException(500, "systemctl stop timed out")

    try:
        async with httpx.AsyncClient(timeout=LOCAL_AGENT_TIMEOUT) as client:
            resp = await client.post(f"{LOCAL_AGENT_URL}/stop/{instance_id}")
            resp.raise_for_status()
            return resp.json()
    except httpx.ConnectError:
        raise HTTPException(502, "Local agent unreachable")
    except httpx.HTTPStatusError as e:
        raise HTTPException(e.response.status_code, e.response.text)


@app.post("/api/{instance_id}/model")
async def set_instance_model(instance_id: str, payload: ModelUpdateRequest):
    model_key = payload.model.strip()
    if not model_key or "/" not in model_key:
        raise HTTPException(400, "Model must be in provider/model format")

    if instance_id == "jaycloud":
        metadata = _apply_jaycloud_primary_model(model_key)
        return {
            "message": "JayCloud model updated",
            "model": metadata["current_model"],
            "available_models": metadata["available_models"],
        }

    try:
        async with httpx.AsyncClient(timeout=LOCAL_AGENT_TIMEOUT) as client:
            resp = await client.post(f"{LOCAL_AGENT_URL}/model/{instance_id}", json={"model": model_key})
            resp.raise_for_status()
            return resp.json()
    except httpx.ConnectError:
        raise HTTPException(502, "Local agent unreachable")
    except httpx.HTTPStatusError as e:
        raise HTTPException(e.response.status_code, e.response.text)


@app.api_route("/gateway/{instance_id}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
@app.api_route("/gateway/{instance_id}/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def proxy_gateway_http(request: Request, instance_id: str, path: str = ""):
    if instance_id == "jaycloud":
        return await _proxy_jaycloud_http(request, path)
    if instance_id in {"jay", "jayhova", "jarvis"}:
        return await _proxy_local_http(request, instance_id, path)
    raise HTTPException(404, f"Unknown instance: {instance_id}")


@app.websocket("/gateway/{instance_id}")
@app.websocket("/gateway/{instance_id}/")
@app.websocket("/gateway/{instance_id}/{path:path}")
async def proxy_gateway_websocket(websocket: WebSocket, instance_id: str):
    if instance_id == "jaycloud":
        await _proxy_jaycloud_websocket(websocket)
        return
    if instance_id in {"jay", "jayhova", "jarvis"}:
        await _proxy_local_websocket(websocket, instance_id)
        return
    await websocket.close(code=4404)


# Serve static frontend -- must be last so /api routes take priority
_static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
app.mount("/", StaticFiles(directory=_static_dir, html=True), name="static")
