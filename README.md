# openclaw-k

`openclaw-k` manages OpenClaw user containers through a single HTTP API.

OpenClaw image used: `ghcr.io/openclaw/openclaw:latest`

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

OpenAPI:

- `/docs`
- `/openapi.json`
