# openclaw-k

Minimal Python CLI to manage per-user OpenClaw Docker containers.

Uses the OpenClaw Docker image from docs: `ghcr.io/openclaw/openclaw:latest`.

## Quick Install

Requirements:

- Docker running locally
- Python 3.10+

Install and run:

```bash
cd /Users/stelios/Desktop/openclaw-k
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

# Create one OpenClaw instance for user alice on port 19001
openclaw-k create user alice --port 19001
```

Open the printed convenience link in your browser (`http://127.0.0.1:<port>/#token=...`).

## Commands

```bash
# Create an OpenClaw user/container on port 19001
openclaw-k create user alice --port 19001

# Create using explicit config file and wait up to 5 minutes for readiness
openclaw-k create user alice --port 19001 --config-file ./openclaw.json --wait-timeout 300

# Inspect one user
openclaw-k inspect user alice

# List managed users
openclaw-k list users

# Delete user container + volumes
openclaw-k delete user alice

# Delete container only (keep data volumes)
openclaw-k delete user alice --keep-data

# Run API server (docs at /docs)
export OPENCLAW_K_API_TOKEN='change-me'
openclaw-k api serve --host 127.0.0.1 --port 8787
```

When creating a user, `openclaw-k` prints:

- base URL
- generated token
- convenience URL with `#token=...`
- readiness success only after gateway is live and model initialization is detected

`openclaw-k create user` config behavior:

- If `--config-file` is provided, that file is ingested as `/home/node/.openclaw/openclaw.json`.
- If `OPENCLAW_K_DEFAULT_CONFIG_FILE` is set, that file is ingested automatically.
- If `--config-file` is omitted and `./openclaw.json` exists, it is ingested automatically.
- If neither is available, creation continues with default OpenClaw config.

## Architecture

- There is no always-on `openclaw-k` API server in this project.
- `openclaw-k` is a command-line tool that talks directly to Docker, then exits.
- Each `create user` command creates one OpenClaw container (`openclaw-<user>`).
- If you create only one user, seeing only one container is expected.
- Running `openclaw-k` as a container is also not a persistent server; it is a short-lived helper container that controls Docker via `/var/run/docker.sock`.

## HTTP API (Bearer Token)

Start API:

```bash
export OPENCLAW_K_API_TOKEN='change-me'
openclaw-k api serve --host 127.0.0.1 --port 8787
```

GCP VM style (accept remote API clients):

```bash
export OPENCLAW_K_API_TOKEN='z3hra-1k3r-st3li0s-04-04-2026!'
openclaw-k api serve --host 0.0.0.0 --port 8787
```

Automatic config for every created user:

```bash
export OPENCLAW_K_DEFAULT_CONFIG_FILE='/opt/openclaw-k/openclaw.json'
```

If you run API inside Docker, mount it and set env:

```bash
docker run -d \
  --name openclaw-k-api \
  --restart unless-stopped \
  -p 8787:8787 \
  -e OPENCLAW_K_API_TOKEN='z3hra-1k3r-st3li0s-04-04-2026!' \
  -e OPENCLAW_K_DEFAULT_CONFIG_FILE='/app/openclaw.json' \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v /opt/openclaw-k/openclaw.json:/app/openclaw.json:ro \
  openclaw-k:local \
  api serve --host 0.0.0.0 --port 8787
```

Auth for all `/v1/*` endpoints:

```http
Authorization: Bearer <OPENCLAW_K_API_TOKEN>
```

OpenAPI docs:

- `GET /docs`
- `GET /openapi.json`

Endpoints:

- `GET /health`
  - public liveness check
  - returns: `{ "ok": true, "service": "openclaw-k-api" }`
- `POST /v1/users`
  - create user/container and wait until ready
  - request:
```json
{
  "username": "alice",
  "port": 19001,
  "key": "optional-openclaw-token",
  "image": "ghcr.io/openclaw/openclaw:latest",
  "config_file_path": "/abs/path/openclaw.json",
  "wait_timeout_seconds": 240
}
```
  - response `201`:
```json
{
  "user": "alice",
  "container": "openclaw-alice",
  "status": "ready",
  "port": 19001,
  "url": "http://127.0.0.1:19001/",
  "connect_link": "http://127.0.0.1:19001/#token=...",
  "token": "...",
  "image": "ghcr.io/openclaw/openclaw:latest",
  "config_ingested": true
}
```
- `GET /v1/users`
  - list users
- `GET /v1/users/{username}`
  - inspect a single user
- `DELETE /v1/users/{username}?keep_data=false`
  - delete container, optionally keep volumes

Standard API errors:

- `401` missing/invalid auth header format
- `403` invalid bearer token
- `400` invalid request payload/config path
- `404` user not found
- `409` user exists / port conflict
- `500` internal/docker failures
- `504` readiness timeout

## End-to-End Tutorial (Server + Python Client)

1. Start `openclaw-k` API server on VM

```bash
cd /Users/stelios/Desktop/openclaw-k
source .venv/bin/activate
export OPENCLAW_K_API_TOKEN='z3hra-1k3r-st3li0s-04-04-2026!'
openclaw-k api serve --host 0.0.0.0 --port 8787
```

2. Optional health check

```bash
curl http://127.0.0.1:8787/health
```

3. Python client example (create user and print connect link)

```python
import requests

BASE = "http://127.0.0.1:8787"  # replace with http://<VM_PUBLIC_IP>:8787 if remote
TOKEN = "z3hra-1k3r-st3li0s-04-04-2026!"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}

payload = {
    "username": "demo1",
    "port": 19111,
    "wait_timeout_seconds": 300
}

response = requests.post(f"{BASE}/v1/users", headers=HEADERS, json=payload, timeout=400)
print("status:", response.status_code)
data = response.json()
print(data)

if response.status_code == 201:
    print("Open this URL in browser:")
    print(data["connect_link"])
```

4. Open OpenClaw in browser

- Use the `connect_link` returned by API, for example:  
  `http://127.0.0.1:19111/#token=...`
- From outside VM, use VM public IP in the URL host.

5. List / inspect / delete via API

```bash
# List users
curl -H "Authorization: Bearer z3hra-1k3r-st3li0s-04-04-2026!" \
  http://127.0.0.1:8787/v1/users

# Inspect one user
curl -H "Authorization: Bearer z3hra-1k3r-st3li0s-04-04-2026!" \
  http://127.0.0.1:8787/v1/users/demo1

# Delete user
curl -X DELETE \
  -H "Authorization: Bearer z3hra-1k3r-st3li0s-04-04-2026!" \
  "http://127.0.0.1:8787/v1/users/demo1?keep_data=false"
```

### GCP Notes

- Open firewall for TCP `8787` (API).
- Open firewall for user ports you assign (for example `19111`, `19001`, etc.).
- Keep API protected behind trusted source IPs whenever possible.

## Run as a container (manage Docker from inside Docker)

Build:

```bash
docker build -t openclaw-k:local .
```

Run against host Docker daemon:

```bash
docker run --rm \
  -v /var/run/docker.sock:/var/run/docker.sock \
  openclaw-k:local create user bob --port 19002
```

Delete:

```bash
docker run --rm \
  -v /var/run/docker.sock:/var/run/docker.sock \
  openclaw-k:local delete user bob
```
