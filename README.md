# OpenClaw Dashboard

A web dashboard for managing multiple [OpenClaw](https://github.com/nicholasgasior/openclaw) gateway instances across machines. Built because I got tired of SSH-ing into different boxes to restart things.

The setup assumes you have some OpenClaw gateways running on a local Windows machine and optionally one on a Linux server, and you want a single web UI to start/stop them, switch models, and see their status. It's two components:

- **Dashboard** (runs on your server in Docker) -- serves the web UI and aggregates status from everywhere
- **Local Agent** (runs on your Windows machine) -- a small FastAPI service that can start/stop local OpenClaw processes

They talk to each other over Tailscale (or any private network), and the dashboard is served behind whatever reverse proxy you use.

## Architecture

```
Browser --> Reverse Proxy (Caddy, nginx, etc.) --> Dashboard Container
                                                        |
                                       +----------------+----------------+
                                       |                                 |
                               systemctl (local)              HTTP over Tailscale
                               for cloud instance              to local agent
                                                               |         |
                                                          Instance A   Instance B
                                                           (18789)     (18804)
```

The dashboard backend does two things:
1. Manages a "cloud" instance on the same Linux box via `systemctl`
2. Proxies start/stop/status requests to the local Windows agent over Tailscale

The local agent uses `psutil` to detect running `node.exe` processes that match OpenClaw's port flags, and can launch/kill them. It also proxies the OpenClaw Control UI through the dashboard, so you get embedded gateway access without exposing ports publicly.

## What it looks like

Glassmorphic cards, dark/light theme, auto-refreshes every 5 seconds. Each instance card shows:
- Running/stopped/unreachable status with a toggle switch
- Uptime, memory usage, PIDs
- Current LLM model with a picker to switch models
- Direct link to the gateway's Control UI

The frontend is a single HTML file with inline CSS/JS -- no build step, no dependencies, no node_modules. Deliberately kept simple.

## Prerequisites

- Python 3.12+
- Docker and Docker Compose (for the dashboard)
- A Tailscale network (or any way for the server to reach the Windows machine)
- OpenClaw gateway instances already set up on your machines

## Setup

### 1. Local Agent (Windows)

The local agent runs on whichever machine hosts your local OpenClaw instances.

```bash
cd local-agent
pip install -r requirements.txt
```

Copy the example config and edit it for your setup:

```bash
cp config.example.json config.json
```

Edit `config.json` -- each instance needs:
- `port`: the port the OpenClaw gateway runs on
- `state_dir`: path to that instance's `.openclaw` directory (where `openclaw.json` lives)
- `start_script`: path to the script that launches the gateway
- `start_cmd`: the full command array to execute it
- `display_name`: what shows up in the dashboard UI

Example for a typical setup:

```json
{
  "instances": {
    "main": {
      "port": 18789,
      "state_dir": "C:\\Users\\you\\.openclaw",
      "start_script": "C:\\Users\\you\\.openclaw\\start-hidden.vbs",
      "start_cmd": ["cscript", "//nologo", "C:\\Users\\you\\.openclaw\\start-hidden.vbs"],
      "display_name": "Main"
    }
  }
}
```

Start the agent:

```bash
python agent.py
```

It binds to `0.0.0.0:9830`. For running it in the background, use the included PowerShell/VBS scripts:

```bash
# PowerShell (visible window)
.\start-agent.ps1

# Hidden (no window)
cscript //nologo start-agent-hidden.vbs
```

The PowerShell script has a respawn loop -- if the agent crashes, it restarts after 5 seconds. Set `OPENCLAW_PYTHON` env var if you need a specific Python executable (defaults to `python`).

### 2. Dashboard (Linux server)

```bash
cd dashboard
```

Set environment variables (or create a `.env` file):

```bash
export LOCAL_AGENT_URL=http://100.x.x.x:9830   # Tailscale IP of your Windows machine
export DASHBOARD_HOST=openclaw.yourdomain.com    # Your public hostname
```

Build and run:

```bash
docker compose up -d --build
```

The container exposes port 8860 internally and expects to be on a `web_proxy` Docker network (shared with your reverse proxy). Adjust `docker-compose.yaml` if your setup is different.

For the cloud instance (the one on the same Linux box), the dashboard calls `systemctl start/stop openclaw-jay` directly. The container mounts the D-Bus socket for this. If your systemd service has a different name, grep for `openclaw-jay` in `app/main.py` and change it.

### 3. Reverse proxy

Point your domain at the dashboard container. Caddy example:

```
openclaw.yourdomain.com {
    reverse_proxy openclaw-dashboard:8860
}
```

I'd strongly recommend putting this behind some kind of auth (Cloudflare Access, Authelia, basic auth, etc.) since the dashboard can start and stop processes on your machines.

## Environment variables

### Dashboard

| Variable | Default | Description |
|----------|---------|-------------|
| `LOCAL_AGENT_URL` | `http://127.0.0.1:9830` | URL of the Windows local agent |
| `DASHBOARD_HOST` | `localhost:8860` | Public hostname (used for building gateway URLs) |
| `JAYCLOUD_CONFIG_PATH` | `/root/.openclaw/openclaw.json` | Path to the cloud instance's config |
| `JAYCLOUD_GATEWAY_URL` | _(derived)_ | Override the cloud instance's public gateway URL |

### Local Agent

| Variable | Default | Description |
|----------|---------|-------------|
| `OPENCLAW_PUBLIC_HOST` | _(empty)_ | This machine's address as seen by the dashboard |
| `OPENCLAW_PUBLIC_SCHEME` | `http` | Scheme for gateway URLs |
| `OPENCLAW_AGENT_CONFIG` | `config.json` | Path to the instance definitions file |
| `OPENCLAW_PYTHON` | `python` | Python executable (used by `start-agent.ps1`) |

## Gateway proxy

One of the more useful features: the dashboard proxies each instance's OpenClaw Control UI through itself. So instead of exposing port 18789 on your Windows machine to the internet, you access it at `https://openclaw.yourdomain.com/gateway/main/`. The proxy handles:

- HTTP request forwarding with path rewriting
- WebSocket relay (for the real-time chat interface)
- Auth token injection (reads tokens from each instance's `openclaw.json`)
- CSP header rewriting for injected bootstrap scripts
- Control UI base path overrides

This means you can have one public endpoint with auth, and all your instances are accessible through it.

## Model switching

The dashboard reads each instance's `openclaw.json` and its model catalog (`agents/main/agent/models.json`) to build a list of available models. You can switch the primary model from the UI -- it writes directly to the config file. The gateway picks up the change on the next request.

## Adapting this for your setup

This was built for a specific setup (Windows workstation + Hetzner VPS), so you'll probably need to adjust a few things:

- **Instance names**: The frontend has a hardcoded `INSTANCE_ORDER` array in `index.html` -- update it to match your `config.json` instance IDs and whatever cloud instance ID you use
- **Cloud instance**: If you don't have a cloud instance, strip out the `jaycloud` handling in `main.py` and the `systemctl` calls
- **Linux local instances**: The local agent assumes Windows (`node.exe` process matching, `CREATE_NO_WINDOW` flags). For Linux instances, you'd swap `psutil` process matching for `systemctl` or `pgrep`
- **Docker network**: The compose file expects a `web_proxy` external network. Change or remove this if you don't use one

## License

MIT
