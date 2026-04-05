# openclaw-k

`openclaw-k` manages OpenClaw user containers through a single HTTP API.

OpenClaw image used: `ghcr.io/openclaw/openclaw:latest`

## Capabilities

- Provider injection at user creation:
  - default provider from `openclaw-k.yaml` (`providers.default`)
  - optional per-user override: `--provider openclaw-openai|openclaw-gemma4|<profile>`
- Skills injection at user creation:
  - recursive copy from `defaults.skills_dir` into `/app/skills`
  - nested folders/scripts are supported
- SOUL injection at user creation:
  - optional copy from `defaults.soul_file` into `/home/node/.openclaw/workspace/SOUL.md`

## Install

Requirements:

- Docker
- Python 3.10+

```bash
git clone https://github.com/steliosot/openclaw-k.git
cd openclaw-k
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

## Run API

Create `.env` from template and update values:

```bash
cp .env.example .env
```

Set at least:
- `OPENCLAW_K_API_TOKEN`
- `OPENAI_API_KEY` (if using OpenAI provider)

Start the API server:

```bash
openclaw-k api serve --host 0.0.0.0 --port 8787
```

Recommended for VM/server (loads providers from YAML and recreates API container):

```bash
openclaw-k up --config ./openclaw-k.yaml
```

Health check:

```bash
curl http://127.0.0.1:8787/health
```

## Run CLI

CLI CRUD commands call the API and read `.env` automatically.

Create user:

```bash
openclaw-k create user alice --port 19111
```

Create user with provider override (from `openclaw-k.yaml` profiles):

```bash
openclaw-k create user alice --port 19111 --provider openclaw-openai
openclaw-k create user bob --port 19112 --provider openclaw-gemma4
```

Inspect user:

```bash
openclaw-k inspect user alice
```

List users:

```bash
openclaw-k list users
```

Delete user:

```bash
openclaw-k delete user alice
```

Delete user but keep volumes:

```bash
openclaw-k delete user alice --keep-data
```

## API Endpoints

Auth for all `/v1/*` routes:

`Authorization: Bearer <OPENCLAW_K_API_TOKEN>`

- `GET /health`: API liveness
- `POST /v1/users`: create user/container and wait until ready
- `GET /v1/users`: list users
- `GET /v1/users/{username}`: inspect user
- `DELETE /v1/users/{username}?keep_data=false`: delete user

## API Examples

Set variables:

```bash
API="http://34.16.127.240:8787"
TOKEN="REPLACE_ME"
AUTH="Authorization: Bearer $TOKEN"
```

Health:

```bash
curl -s "$API/health"
```

Expected:

```json
{"ok":true,"service":"openclaw-k-api"}
```

Create user:

```bash
curl -s -X POST "$API/v1/users" \
  -H "$AUTH" \
  -H "Content-Type: application/json" \
  -d '{
    "username": "alice",
    "port": 19111,
    "provider": "openclaw-openai",
    "wait_timeout_seconds": 300
  }'
```

Expected (example):

```json
{
  "user": "alice",
  "container": "openclaw-alice",
  "status": "ready",
  "port": 19111,
  "url": "http://34.16.127.240:19111/",
  "connect_link": "http://34.16.127.240:19111/#token=...",
  "token": "...",
  "image": "ghcr.io/openclaw/openclaw:latest",
  "config_ingested": true
}
```

List users:

```bash
curl -s -H "$AUTH" "$API/v1/users"
```

Expected (example):

```json
{
  "items": [
    {
      "user": "alice",
      "container": "openclaw-alice",
      "status": "running",
      "health": "healthy",
      "ready": true,
      "port": 19111
    }
  ]
}
```

Inspect user:

```bash
curl -s -H "$AUTH" "$API/v1/users/alice"
```

Expected (example):

```json
{
  "user": "alice",
  "container": "openclaw-alice",
  "status": "running",
  "ready": true,
  "port": 19111,
  "url": "http://34.16.127.240:19111/",
  "connect_link": "http://34.16.127.240:19111/#token=..."
}
```

Delete user:

```bash
curl -s -X DELETE -H "$AUTH" "$API/v1/users/alice?keep_data=false"
```

Expected (example):

```json
{
  "user": "alice",
  "container_deleted": true,
  "volumes_deleted": [
    "openclaw-config-alice",
    "openclaw-workspace-alice",
    "openclaw-skills-alice"
  ],
  "keep_data": false
}
```

OpenAPI:

- `/docs`
- `/openapi.json`
