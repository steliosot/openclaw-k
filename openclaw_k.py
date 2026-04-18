#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hmac
import io
import json
import os
import secrets
import sys
import tarfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
import urllib.request
import urllib.error
import urllib.parse

import docker
from docker.errors import APIError, DockerException, NotFound
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field, ConfigDict, field_validator
import yaml


def load_dotenv_file(path: str = ".env") -> None:
    env_path = Path(path).expanduser().resolve()
    if not env_path.is_file():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if (value.startswith("'") and value.endswith("'")) or (value.startswith('"') and value.endswith('"')):
            value = value[1:-1]
        # Existing shell env vars have priority over .env values.
        os.environ.setdefault(key, value)


load_dotenv_file()

DEFAULT_OPENCLAW_IMAGE = "ghcr.io/openclaw/openclaw:latest"
OPENCLAW_INTERNAL_PORT = 18789
DEFAULT_API_HOST = "127.0.0.1"
DEFAULT_API_PORT = 8787
DEFAULT_WAIT_TIMEOUT_SECONDS = 240
DEFAULT_PUBLISH_BIND_IP = os.getenv("OPENCLAW_K_PUBLISH_BIND_IP", "0.0.0.0")
DEFAULT_CONNECT_HOST = os.getenv("OPENCLAW_K_CONNECT_HOST", "127.0.0.1")
DEFAULT_PROVIDER_FILE_ENV = "OPENCLAW_K_DEFAULT_PROVIDER_FILE"
DEFAULT_CONFIG_FILE_ENV = "OPENCLAW_K_DEFAULT_CONFIG_FILE"  # backwards-compatible alias
DEFAULT_SKILLS_DIR_ENV = "OPENCLAW_K_DEFAULT_SKILLS_DIR"
DEFAULT_SOUL_FILE_ENV = "OPENCLAW_K_DEFAULT_SOUL_FILE"
DEFAULT_WORKSPACE_DIR_ENV = "OPENCLAW_K_DEFAULT_WORKSPACE_DIR"
DEFAULT_UP_CONFIG_FILE = "openclaw-k.yaml"
INTERNAL_DEFAULTS_DIR = "/app/defaults"
DEFAULT_API_BASE_URL = f"http://127.0.0.1:{DEFAULT_API_PORT}"
DEFAULT_API_URL_ENV = "OPENCLAW_K_API_URL"
OPENAI_API_KEY_ENV = "OPENAI_API_KEY"
PROVIDER_PROFILES_ENV = "OPENCLAW_K_PROVIDER_PROFILES_JSON"


class ServiceError(Exception):
    def __init__(self, status_code: int, code: str, message: str, details: Any | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details


@dataclass
class UserContainer:
    username: str

    @property
    def container_name(self) -> str:
        return f"openclaw-{self.username}"

    @property
    def config_volume(self) -> str:
        return f"openclaw-config-{self.username}"

    @property
    def workspace_volume(self) -> str:
        return f"openclaw-workspace-{self.username}"

    @property
    def skills_volume(self) -> str:
        return f"openclaw-skills-{self.username}"


class CreateUserRequest(BaseModel):
    username: str = Field(min_length=1)
    port: int = Field(ge=1, le=65535)
    key: str | None = None
    image: str = DEFAULT_OPENCLAW_IMAGE
    provider: str | None = None
    config_file_path: str | None = None
    wait_timeout_seconds: int = Field(default=DEFAULT_WAIT_TIMEOUT_SECONDS, ge=5, le=3600)


class UserSummary(BaseModel):
    user: str
    container: str
    status: str
    health: str
    ready: bool
    port: int | None


class UserInspectResponse(BaseModel):
    user: str
    container: str
    image: str
    status: str
    health: str
    ready: bool
    port: int | None
    url: str | None
    connect_link: str | None
    config_file_present: bool
    volumes: dict[str, str]


class CreateUserResponse(BaseModel):
    user: str
    container: str
    status: str
    port: int
    url: str
    connect_link: str
    token: str
    image: str
    config_ingested: bool


class ListUsersResponse(BaseModel):
    items: list[UserSummary]


class DeleteUserResponse(BaseModel):
    user: str
    container_deleted: bool
    volumes_deleted: list[str]
    keep_data: bool


class WriteFileRequest(BaseModel):
    path: str = Field(min_length=1, max_length=512, description="Relative path inside workspace (e.g. uploads/image.png)")
    content: str = Field(description="Base64-encoded file content")
    model_config = ConfigDict(extra="forbid")


class WriteFileResponse(BaseModel):
    status: str
    path: str
    user: str


class DeviceIdentityResponse(BaseModel):
    """Device identity files read from inside a running openclaw container.

    `identity` is a flat, best-effort projection of common field names that
    Maestro-style clients look for (deviceId, operatorToken, publicKey,
    privateKey). `raw` preserves the original file contents so clients can
    adapt if openclaw's on-disk schema changes.
    """
    user: str
    identity: dict[str, Any]
    raw: dict[str, Any]
    model_config = ConfigDict(extra="allow")


class ChatMessage(BaseModel):
    role: str
    content: Any  # string or array of content parts
    images: list[str] | None = None  # base64 images for Ollama native format
    model_config = ConfigDict(extra="allow")


class ChatRequest(BaseModel):
    model: str = "openclaw"
    messages: list[ChatMessage]
    stream: bool = False
    user: str | None = None  # session identifier
    model_config = ConfigDict(extra="allow")


class ChatResponse(BaseModel):
    model_config = ConfigDict(extra="allow")


class UpdateAllRequest(BaseModel):
    users: list[str] | None = None
    provider: str | None = None
    restart: bool = True
    wait_timeout_seconds: int = Field(default=DEFAULT_WAIT_TIMEOUT_SECONDS, ge=5, le=3600)


class UpdateAllItem(BaseModel):
    user: str
    container: str
    updated: bool
    restarted: bool
    ready: bool
    applied: dict[str, bool]
    errors: list[str]


class UpdateAllResponse(BaseModel):
    ok: bool
    total: int
    updated: int
    failed: int
    items: list[UpdateAllItem]


def resolve_api_base_url(api_url_arg: str | None) -> str:
    return (api_url_arg or os.getenv(DEFAULT_API_URL_ENV) or DEFAULT_API_BASE_URL).rstrip("/")


def resolve_api_token(api_token_arg: str | None) -> str:
    token = api_token_arg or os.getenv("OPENCLAW_K_API_TOKEN")
    if not token:
        raise ServiceError(400, "token_required", "Set OPENCLAW_K_API_TOKEN or pass --api-token.")
    return token


def api_request(
    *,
    method: str,
    path: str,
    api_base_url: str,
    api_token: str,
    json_body: dict[str, Any] | None = None,
    query_params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    url = f"{api_base_url}{path}"
    if query_params:
        url = f"{url}?{urllib.parse.urlencode(query_params)}"

    body: bytes | None = None
    headers = {"Authorization": f"Bearer {api_token}"}
    if json_body is not None:
        body = json.dumps(json_body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url=url, method=method, headers=headers, data=body)
    try:
        with urllib.request.urlopen(req, timeout=DEFAULT_WAIT_TIMEOUT_SECONDS + 30) as response:
            payload = response.read().decode("utf-8")
            return json.loads(payload) if payload else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw)
            err = parsed.get("error", {}) if isinstance(parsed, dict) else {}
            raise ServiceError(
                exc.code,
                err.get("code", "http_error"),
                err.get("message", raw or f"HTTP {exc.code}"),
                parsed,
            ) from exc
        except json.JSONDecodeError:
            raise ServiceError(exc.code, "http_error", raw or f"HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise ServiceError(500, "api_unreachable", f"Could not reach API at {url}: {exc.reason}") from exc


class ProviderProfile(BaseModel):
    file: str


class ApiConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8787


class DockerUpConfig(BaseModel):
    container_name: str = "openclaw-k-api"
    image_tag: str = "openclaw-k:local"


class ProvidersConfig(BaseModel):
    default: str
    profiles: dict[str, ProviderProfile]

    @field_validator("profiles")
    @classmethod
    def validate_profiles(cls, value: dict[str, ProviderProfile]) -> dict[str, ProviderProfile]:
        if not value:
            raise ValueError("providers.profiles must define at least one profile.")
        return value


class DefaultsConfig(BaseModel):
    publish_bind_ip: str = "0.0.0.0"
    connect_host: str = "127.0.0.1"
    skills_dir: str | None = None
    soul_file: str | None = None
    workspace_dir: str | None = None


class UpConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api: ApiConfig = Field(default_factory=ApiConfig)
    docker: DockerUpConfig = Field(default_factory=DockerUpConfig)
    providers: ProvidersConfig
    defaults: DefaultsConfig = Field(default_factory=DefaultsConfig)

    @field_validator("providers")
    @classmethod
    def validate_default_provider(cls, providers: ProvidersConfig) -> ProvidersConfig:
        if providers.default not in providers.profiles:
            raise ValueError("providers.default must exist in providers.profiles")
        return providers


def error_payload(code: str, message: str, details: Any | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"error": {"code": code, "message": message}}
    if details is not None:
        payload["error"]["details"] = details
    return payload


def get_docker_client() -> docker.DockerClient:
    try:
        client = docker.from_env()
        client.ping()
        return client
    except DockerException as exc:
        raise ServiceError(500, "docker_unreachable", f"Docker is not reachable: {exc}") from exc


def load_up_config(config_path_arg: str | None) -> tuple[UpConfig, Path]:
    config_path = Path(config_path_arg or DEFAULT_UP_CONFIG_FILE).expanduser().resolve()
    if not config_path.is_file():
        raise ServiceError(400, "invalid_config", f"Config file not found: {config_path}")
    try:
        raw = yaml.safe_load(config_path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ServiceError(400, "invalid_config", f"Invalid YAML: {exc}") from exc
    try:
        return UpConfig.model_validate(raw), config_path
    except Exception as exc:
        raise ServiceError(400, "invalid_config", f"Config validation failed: {exc}") from exc


def resolve_existing_file(path_str: str, *, config_dir: Path, required: bool, field_name: str) -> Path | None:
    p = Path(path_str).expanduser()
    if not p.is_absolute():
        p = (config_dir / p).resolve()
    else:
        p = p.resolve()
    if not p.is_file():
        if required:
            raise ServiceError(400, "invalid_config", f"{field_name} file not found: {p}")
        return None
    return p


def resolve_existing_dir(path_str: str | None, *, config_dir: Path, field_name: str) -> Path | None:
    if not path_str:
        return None
    p = Path(path_str).expanduser()
    if not p.is_absolute():
        p = (config_dir / p).resolve()
    else:
        p = p.resolve()
    if not p.exists() or not p.is_dir():
        # Optional by design; skip silently if missing.
        return None
    return p


def _resolve_provider_profile_file(provider_arg: str) -> Path:
    def alias_variants(value: str) -> set[str]:
        v = value.strip().lower()
        if v.endswith(".json"):
            v = v[:-5]
        variants = {v}
        for prefix in ("openclaw-", "provider-"):
            if v.startswith(prefix):
                variants.add(v[len(prefix) :])
        return {x for x in variants if x}

    profiles_json = os.getenv(PROVIDER_PROFILES_ENV)
    needle = provider_arg.strip().lower()
    if not needle:
        raise ServiceError(400, "invalid_provider", "Provider cannot be empty.")
    needle_aliases = alias_variants(needle)

    if profiles_json:
        try:
            mapping = json.loads(profiles_json)
        except json.JSONDecodeError as exc:
            raise ServiceError(500, "invalid_provider_map", f"{PROVIDER_PROFILES_ENV} is invalid JSON.") from exc
        if isinstance(mapping, dict):
            for profile_name, mapped_path in mapping.items():
                if not isinstance(mapped_path, str):
                    continue
                p = Path(mapped_path).expanduser().resolve()
                aliases = alias_variants(str(profile_name)) | alias_variants(p.name) | alias_variants(p.stem)
                if needle_aliases & aliases:
                    if not p.is_file():
                        raise ServiceError(400, "invalid_provider", f"Provider file not found: {p}")
                    return p

    up_config_path = (Path.cwd() / DEFAULT_UP_CONFIG_FILE).resolve()
    if not up_config_path.is_file():
        raise ServiceError(
            400,
            "invalid_provider",
            f"--provider requires {DEFAULT_UP_CONFIG_FILE} in current directory.",
        )
    up_config, loaded_config_path = load_up_config(str(up_config_path))

    for profile_name, profile in up_config.providers.profiles.items():
        profile_path = resolve_existing_file(
            profile.file,
            config_dir=loaded_config_path.parent,
            required=True,
            field_name=f"providers.profiles.{profile_name}.file",
        )
        assert profile_path is not None
        aliases = (
            alias_variants(profile_name)
            | alias_variants(Path(profile.file).name)
            | alias_variants(Path(profile.file).stem)
        )
        if needle_aliases & aliases:
            return profile_path

    available = ", ".join(sorted(up_config.providers.profiles.keys()))
    raise ServiceError(
        400,
        "invalid_provider",
        f"Unknown provider '{provider_arg}'. Available profiles: {available}",
    )


def resolve_config_file_path(config_file_arg: str | None, provider_arg: str | None = None) -> Path | None:
    if config_file_arg:
        config_path = Path(config_file_arg).expanduser().resolve()
        if not config_path.is_file():
            raise ServiceError(400, "invalid_config_file", f"Config file not found: {config_path}")
        return config_path

    if provider_arg:
        return _resolve_provider_profile_file(provider_arg)

    env_default = os.getenv(DEFAULT_PROVIDER_FILE_ENV) or os.getenv(DEFAULT_CONFIG_FILE_ENV)
    if env_default:
        env_path = Path(env_default).expanduser().resolve()
        if not env_path.is_file():
            raise ServiceError(
                400,
                "invalid_config_file",
                f"{DEFAULT_CONFIG_FILE_ENV} is set but file not found: {env_path}",
            )
        return env_path

    up_config_path = (Path.cwd() / DEFAULT_UP_CONFIG_FILE).resolve()
    if up_config_path.is_file():
        up_config, loaded_config_path = load_up_config(str(up_config_path))
        default_profile = up_config.providers.profiles[up_config.providers.default]
        provider_file = resolve_existing_file(
            default_profile.file,
            config_dir=loaded_config_path.parent,
            required=True,
            field_name=f"providers.profiles.{up_config.providers.default}.file",
        )
        if provider_file:
            return provider_file

    default_path = (Path.cwd() / "openclaw.json").resolve()
    if default_path.is_file():
        return default_path
    return None


def resolve_optional_defaults() -> tuple[Path | None, Path | None, Path | None]:
    skills_dir_env = os.getenv(DEFAULT_SKILLS_DIR_ENV)
    soul_file_env = os.getenv(DEFAULT_SOUL_FILE_ENV)
    workspace_dir_env = os.getenv(DEFAULT_WORKSPACE_DIR_ENV)

    skills_path: Path | None = None
    soul_path: Path | None = None
    workspace_path: Path | None = None

    if skills_dir_env:
        p = Path(skills_dir_env).expanduser().resolve()
        if p.exists() and p.is_dir():
            skills_path = p
    if soul_file_env:
        p = Path(soul_file_env).expanduser().resolve()
        if p.exists() and p.is_file():
            soul_path = p
    if workspace_dir_env:
        p = Path(workspace_dir_env).expanduser().resolve()
        if p.exists() and p.is_dir():
            workspace_path = p

    return skills_path, soul_path, workspace_path


def put_file_into_container(container: docker.models.containers.Container, dest_dir: str, name: str, content: bytes) -> None:
    archive_stream = io.BytesIO()
    with tarfile.open(fileobj=archive_stream, mode="w") as tar:
        info = tarfile.TarInfo(name=name)
        info.size = len(content)
        info.mode = 0o644
        info.uid = 1000   # node user
        info.gid = 1000   # node group
        tar.addfile(info, io.BytesIO(content))
    archive_stream.seek(0)
    ok = container.put_archive(dest_dir, archive_stream.getvalue())
    if not ok:
        raise ServiceError(500, "container_copy_failed", f"Could not copy '{name}' into container at '{dest_dir}'.")


def with_openai_api_key(config_bytes: bytes) -> bytes:
    api_key = os.getenv(OPENAI_API_KEY_ENV)
    if not api_key:
        return config_bytes
    try:
        payload = json.loads(config_bytes.decode("utf-8"))
    except Exception:
        return config_bytes
    if not isinstance(payload, dict):
        return config_bytes

    models = payload.get("models")
    if not isinstance(models, dict):
        return config_bytes
    providers = models.get("providers")
    if not isinstance(providers, dict):
        return config_bytes
    openai = providers.get("openai")
    if not isinstance(openai, dict):
        return config_bytes

    openai["apiKey"] = api_key
    return json.dumps(payload, indent=2).encode("utf-8")


def put_directory_into_container(container: docker.models.containers.Container, src_dir: Path, dest_dir: str) -> None:
    archive_stream = io.BytesIO()
    with tarfile.open(fileobj=archive_stream, mode="w") as tar:
        for path in src_dir.rglob("*"):
            rel = path.relative_to(src_dir)
            info = tarfile.TarInfo(name=str(rel))
            if path.is_dir():
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                tar.addfile(info)
            elif path.is_file():
                data = path.read_bytes()
                info.size = len(data)
                info.mode = 0o644
                tar.addfile(info, io.BytesIO(data))
    archive_stream.seek(0)
    ok = container.put_archive(dest_dir, archive_stream.getvalue())
    if not ok:
        raise ServiceError(500, "container_copy_failed", f"Could not copy directory '{src_dir}' into '{dest_dir}'.")


def run_in_seed_container(
    client: docker.DockerClient,
    image: str,
    volume_mounts: dict[str, dict[str, str]],
    command: list[str],
) -> None:
    seed = client.containers.create(
        image=image,
        command=command,
        user="0:0",
        volumes=volume_mounts,
    )
    try:
        seed.start()
        result = seed.wait()
        status_code = int(result.get("StatusCode", 1))
        if status_code != 0:
            logs = seed.logs(stdout=True, stderr=True).decode("utf-8", errors="replace")
            raise ServiceError(500, "seed_container_failed", f"Seed container failed (exit={status_code}): {logs.strip()}")
    finally:
        seed.remove(force=True)


def seed_openclaw_state(
    client: docker.DockerClient,
    image: str,
    volume_mounts: dict[str, dict[str, str]],
    config_file_path: Path | None,
    skills_dir_path: Path | None,
    soul_file_path: Path | None,
    workspace_dir_path: Path | None,
) -> None:
    run_in_seed_container(
        client,
        image,
        volume_mounts,
        ["sh", "-lc", "mkdir -p /home/node/.openclaw/workspace && chown -R 1000:1000 /home/node/.openclaw"],
    )
    if not config_file_path and not skills_dir_path and not soul_file_path and not workspace_dir_path:
        return

    seed = client.containers.create(
        image=image,
        command=["sh", "-lc", "sleep 60"],
        user="0:0",
        volumes=volume_mounts,
    )
    try:
        seed.start()
        if config_file_path:
            config_content = with_openai_api_key(config_file_path.read_bytes())
            put_file_into_container(seed, "/home/node/.openclaw", "openclaw.json", config_content)
        if skills_dir_path:
            # Docker's first-mount content-preservation copies the openclaw
            # image's built-in /app/skills/ (~50 general-purpose skills —
            # 1password, apple-notes, slack, taskflow, weather, etc.) into
            # the freshly-created named volume before the bind takes effect.
            # Those skills bloat the system prompt to 34k+ tokens and blow
            # past OpenAI's per-model TPM limits. Wipe them so only our
            # seed (just maestro-comfysql) ends up in the mounted volume.
            wipe = seed.exec_run(["sh", "-lc", "rm -rf /app/skills/* /app/skills/.??*"], user="0:0")
            if wipe.exit_code != 0:
                print(
                    f"[openclaw-k] warning: failed to wipe /app/skills before seed in "
                    f"{user.container_name} (exit={wipe.exit_code}): "
                    f"{(wipe.output or b'').decode('utf-8', errors='replace')[:200]}",
                    flush=True,
                )
            put_directory_into_container(seed, skills_dir_path, "/app/skills")
        if workspace_dir_path:
            put_directory_into_container(seed, workspace_dir_path, "/home/node/.openclaw/workspace")
        # Explicit soul_file overrides workspace_dir/SOUL.md if both are set.
        if soul_file_path:
            put_file_into_container(seed, "/home/node/.openclaw/workspace", "SOUL.md", soul_file_path.read_bytes())

        chown_cmd = (
            "if [ -f /home/node/.openclaw/openclaw.json ]; then chown 1000:1000 /home/node/.openclaw/openclaw.json && chmod 600 /home/node/.openclaw/openclaw.json; fi; "
            "if [ -f /home/node/.openclaw/workspace/SOUL.md ]; then chown 1000:1000 /home/node/.openclaw/workspace/SOUL.md && chmod 644 /home/node/.openclaw/workspace/SOUL.md; fi; "
            "if [ -d /home/node/.openclaw/workspace ]; then chown -R 1000:1000 /home/node/.openclaw/workspace; fi; "
            "if [ -d /app/skills ]; then chown -R 1000:1000 /app/skills; fi"
        )
        chown_result = seed.exec_run(["sh", "-lc", chown_cmd])
        if chown_result.exit_code != 0:
            raise ServiceError(500, "config_permissions_failed", "Could not set permissions on seeded defaults")
    finally:
        seed.remove(force=True)


def read_container_logs(container: docker.models.containers.Container, tail: int = 300) -> str:
    return container.logs(stdout=True, stderr=True, tail=tail).decode("utf-8", errors="replace")


def is_gateway_live(container: docker.models.containers.Container) -> bool:
    try:
        probe = container.exec_run(
            [
                "node",
                "-e",
                "fetch('http://127.0.0.1:18789/healthz').then(r=>r.text()).then(t=>{if(!t.includes('\\\"ok\\\":true'))process.exit(1)}).catch(()=>process.exit(1))",
            ],
            demux=False,
        )
        return probe.exit_code == 0
    except APIError:
        return False


def has_model_synced(container: docker.models.containers.Container) -> bool:
    logs = read_container_logs(container, tail=500)
    return "agent model:" in logs


def wait_until_ready(container: docker.models.containers.Container, timeout_seconds: int) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        container.reload()
        if container.status != "running":
            logs = read_container_logs(container)
            raise ServiceError(500, "container_stopped", f"Container stopped before ready.\n{logs}")

        if is_gateway_live(container) and has_model_synced(container):
            return
        time.sleep(2)

    container.reload()
    health = container.attrs.get("State", {}).get("Health", {}).get("Status", "unknown")
    logs = read_container_logs(container)
    raise ServiceError(
        504,
        "readiness_timeout",
        f"Timed out waiting for OpenClaw readiness (health={health}).",
        {"logs": logs},
    )


def read_user_info(client: docker.DockerClient, username: str) -> tuple[docker.models.containers.Container, UserContainer]:
    user = UserContainer(username)
    try:
        container = client.containers.get(user.container_name)
    except NotFound as exc:
        raise ServiceError(404, "user_not_found", f"User '{username}' not found.") from exc
    container.reload()
    return container, user


def safe_set_config(
    container: docker.models.containers.Container,
    path: str,
    value: str,
    *,
    optional: bool = False,
) -> None:
    result = container.exec_run(
        ["node", "dist/index.js", "config", "set", path, value, "--strict-json"],
        demux=False,
    )
    if result.exit_code != 0:
        if optional:
            # Some config keys are version-dependent; if the container's openclaw
            # build no longer accepts this key, log and continue rather than
            # failing provisioning. The container is healthy; this is cosmetic.
            detail = (result.output or b"").decode("utf-8", errors="replace").strip()
            print(
                f"[openclaw-k] warning: optional config '{path}' not set "
                f"(exit={result.exit_code}): {detail[:200]}",
                flush=True,
            )
            return
        raise ServiceError(500, "config_set_failed", f"Failed to set config '{path}'.")


def get_device_identity_service(username: str) -> dict[str, Any]:
    """Read a container's device identity + pairing files from its config volume.

    Openclaw writes device identity JSON (deviceId, keypair) and pairing info
    (operatorToken) under /home/node/.openclaw shortly after first boot. Paths
    and field names vary between openclaw versions, so we:

      1. Probe a handful of likely paths via `docker exec cat`.
      2. Additionally scan /home/node/.openclaw/identity and .../devices for
         any *.json files we didn't hardcode.
      3. Flatten common field names (deviceId/operatorToken/publicKey/
         privateKey, plus snake_case variants) into `identity`.
      4. Return the full raw contents so Maestro-side code can adapt if
         names drift.

    Returns 404 if the container is missing; 409 if no identity files were
    found (container may still be initializing).
    """
    user = UserContainer(username)
    client = get_docker_client()
    try:
        container = client.containers.get(user.container_name)
    except NotFound as exc:
        raise ServiceError(404, "user_not_found", f"User '{username}' not found.") from exc

    candidate_paths = [
        "/home/node/.openclaw/identity/device.json",
        "/home/node/.openclaw/devices/paired.json",
        "/home/node/.openclaw/device.json",
        "/home/node/.openclaw/identity.json",
    ]

    collected: dict[str, Any] = {}

    def _try_read(path: str) -> None:
        if path in collected:
            return
        result = container.exec_run(["cat", path], demux=False)
        if result.exit_code != 0:
            return
        try:
            raw = (result.output or b"").decode("utf-8")
            parsed = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return
        collected[path] = parsed

    for path in candidate_paths:
        _try_read(path)

    discover = container.exec_run(
        [
            "sh", "-c",
            "find /home/node/.openclaw/identity /home/node/.openclaw/devices "
            "-maxdepth 3 -type f -name '*.json' 2>/dev/null || true",
        ],
        demux=False,
    )
    if discover.exit_code == 0:
        extra_paths = (discover.output or b"").decode("utf-8", errors="replace").strip().splitlines()
        for path in extra_paths:
            path = path.strip()
            if path:
                _try_read(path)

    if not collected:
        raise ServiceError(
            409,
            "device_not_ready",
            f"Device identity files not yet written for '{username}' — try again in a moment.",
        )

    flat: dict[str, Any] = {}
    # Field-name variants observed in the wild: openclaw's identity/device.json
    # uses camelCase with the `Pem` suffix for keys (`privateKeyPem`,
    # `publicKeyPem`). Include those explicitly. `operatorToken` may not be
    # present at all when the container is configured with
    # `gateway.controlUi.dangerouslyDisableDeviceAuth=true` (simple auth mode,
    # no pairing) — that's fine; the caller treats it as optional.
    interesting_fields = (
        "deviceId", "device_id",
        "operatorToken", "operator_token",
        "privateKey", "private_key",
        "devicePrivateKey", "device_private_key",
        "privateKeyPem", "private_key_pem",
        "publicKey", "public_key",
        "devicePublicKey", "device_public_key",
        "publicKeyPem", "public_key_pem",
    )
    for doc in collected.values():
        if not isinstance(doc, dict):
            continue
        for field in interesting_fields:
            if field in doc and field not in flat:
                flat[field] = doc[field]

    return {"user": username, "identity": flat, "raw": collected}


def extract_host_port(container: docker.models.containers.Container) -> int | None:
    ports = container.attrs.get("NetworkSettings", {}).get("Ports", {})
    binding = ports.get(f"{OPENCLAW_INTERNAL_PORT}/tcp")
    if not binding:
        return None
    try:
        return int(binding[0]["HostPort"])
    except (KeyError, ValueError, TypeError):
        return None


def extract_gateway_token(container: docker.models.containers.Container) -> str | None:
    env = container.attrs.get("Config", {}).get("Env", [])
    token_entry = next((item for item in env if item.startswith("OPENCLAW_GATEWAY_TOKEN=")), None)
    if not token_entry:
        return None
    return token_entry.split("=", 1)[1]


def map_create_api_error(exc: APIError) -> ServiceError:
    message = exc.explanation or str(exc)
    lower = message.lower()
    if "already exists" in lower and "container" in lower:
        return ServiceError(409, "user_exists", message)
    if "port is already allocated" in lower or "bind for" in lower:
        return ServiceError(409, "port_conflict", message)
    return ServiceError(500, "docker_api_error", message)


def create_user_service(
    *,
    username: str,
    port: int,
    key: str | None = None,
    image: str = DEFAULT_OPENCLAW_IMAGE,
    provider: str | None = None,
    config_file_arg: str | None = None,
    wait_timeout_seconds: int = DEFAULT_WAIT_TIMEOUT_SECONDS,
    connect_host: str = DEFAULT_CONNECT_HOST,
    publish_bind_ip: str = DEFAULT_PUBLISH_BIND_IP,
) -> dict[str, Any]:
    user = UserContainer(username)
    token = key or secrets.token_urlsafe(24)
    config_file_path = resolve_config_file_path(config_file_arg, provider)
    client = get_docker_client()

    try:
        existing = client.containers.get(user.container_name)
        raise ServiceError(409, "user_exists", f"Container '{user.container_name}' already exists (status={existing.status}).")
    except NotFound:
        pass

    try:
        client.images.pull(image)
        client.volumes.create(name=user.config_volume, labels={"managed-by": "openclaw-k", "openclaw-k.user": username})
        client.volumes.create(name=user.workspace_volume, labels={"managed-by": "openclaw-k", "openclaw-k.user": username})
        client.volumes.create(name=user.skills_volume, labels={"managed-by": "openclaw-k", "openclaw-k.user": username})

        volume_mounts = {
            user.config_volume: {"bind": "/home/node/.openclaw", "mode": "rw"},
            user.workspace_volume: {"bind": "/home/node/.openclaw/workspace", "mode": "rw"},
            user.skills_volume: {"bind": "/app/skills", "mode": "rw"},
        }
        # Bind-mount /opt/comfysql (read-only) if it exists on the host so
        # pip install inside the container can read from a local path.
        # steliosot/comfysql is private so containers can't clone it directly;
        # the VM operator maintains /opt/comfysql via an authenticated clone.
        if Path("/opt/comfysql").is_dir():
            volume_mounts["/opt/comfysql"] = {"bind": "/opt/comfysql", "mode": "ro"}
        skills_dir_path, soul_file_path, workspace_dir_path = resolve_optional_defaults()
        seed_openclaw_state(client, image, volume_mounts, config_file_path, skills_dir_path, soul_file_path, workspace_dir_path)

        container = client.containers.run(
            image,
            name=user.container_name,
            detach=True,
            restart_policy={"Name": "unless-stopped"},
            ports={f"{OPENCLAW_INTERNAL_PORT}/tcp": (publish_bind_ip, port)},
            environment={"OPENCLAW_GATEWAY_TOKEN": token},
            command=["node", "openclaw.mjs", "gateway", "--allow-unconfigured", "--bind", "lan"],
            labels={"app": "openclaw", "managed-by": "openclaw-k", "openclaw-k.user": username},
            volumes=volume_mounts,
        )

        safe_set_config(container, "gateway.controlUi.allowedOrigins", f'["http://127.0.0.1:{port}","http://localhost:{port}"]')
        safe_set_config(container, "gateway.controlUi.dangerouslyAllowHostHeaderOriginFallback", "true")
        # Optional: newer openclaw builds no longer accept this key (MAE-6).
        # Treat as best-effort — the container still boots and is healthy
        # regardless, and device auth is desirable anyway for the WebSocket flow.
        safe_set_config(container, "gateway.controlUi.dangerouslyDisableDeviceAuth", "true", optional=True)

        # Install Maestro-required tooling in the container. The openclaw
        # image ships python3 and git but no pip, no sudo, and no comfysql
        # (the client our maestro-comfysql skill needs to talk to ComfyUI).
        # Do it once here so new users don't have to wait / install manually.
        #
        # Two separate exec_run calls because `sudo` isn't available in the
        # container — we can't drop from root to node inside a single shell.
        apt_result = container.exec_run(
            [
                "sh", "-lc",
                "apt-get update -qq && apt-get install -y -qq python3-pip",
            ],
            user="0:0",
        )
        if apt_result.exit_code != 0:
            print(
                f"[openclaw-k] warning: apt-get python3-pip failed in "
                f"{user.container_name} (exit={apt_result.exit_code}): "
                f"{(apt_result.output or b'').decode('utf-8', errors='replace')[:400]}",
                flush=True,
            )
        elif not Path("/opt/comfysql").is_dir():
            print(
                f"[openclaw-k] warning: /opt/comfysql not found on VM — "
                f"maestro-comfysql skill will not be usable in "
                f"{user.container_name} until an authenticated clone is staged",
                flush=True,
            )
        else:
            # comfysql is bind-mounted at /opt/comfysql (read-only) via
            # volume_mounts above. pip needs to write `src/comfysql.egg-info`
            # to the source directory during the wheel build, so we can't
            # install directly from the read-only mount. Copy to /tmp
            # (~400 KB without examples/output) and install from there.
            pip_result = container.exec_run(
                [
                    "sh", "-lc",
                    # Copy bind-mount to writable /tmp, exclude the heavy
                    # `examples/` + `output/` dirs pip doesn't need.
                    "cp -r /opt/comfysql /tmp/comfysql-src && "
                    "rm -rf /tmp/comfysql-src/examples /tmp/comfysql-src/output && "
                    # --break-system-packages overrides PEP 668
                    # externally-managed marker on Debian's python.
                    "python3 -m pip install --user --break-system-packages --quiet "
                    "/tmp/comfysql-src && "
                    "rm -rf /tmp/comfysql-src && "
                    "echo 'export PATH=$HOME/.local/bin:$PATH' >> /home/node/.bashrc && "
                    # Set up a writable comfysql workdir at /home/node/comfysql:
                    #   - comfy-agent.json copied from the read-only mount
                    #   - input/ symlinked so it picks up workflow templates
                    #   - .state/ populated with the workflow registry
                    #     (copied from the mount so sql_schema_cache.json
                    #     writes land in the writable copy, not the RO mount)
                    # The agent runs comfysql with this as CWD (see SKILL.md).
                    "mkdir -p /home/node/comfysql/output && "
                    "cp /opt/comfysql/comfy-agent.json /home/node/comfysql/comfy-agent.json && "
                    "ln -sf /opt/comfysql/input /home/node/comfysql/input && "
                    "if [ -d /opt/comfysql/.state ]; then "
                    "  cp -r /opt/comfysql/.state /home/node/comfysql/.state; "
                    "else "
                    "  mkdir -p /home/node/comfysql/.state; "
                    "fi && "
                    # Disable the container image's built-in imagegen system
                    # skill. It tells the agent to use Codex's `image_gen`
                    # built-in tool (which requires an ACP agent that isn't
                    # set up for our OpenAI config). Without disabling it,
                    # the agent picks `imagegen` over our `maestro-comfysql`
                    # skill for every image task and then dead-ends.
                    "rm -rf /home/node/.codex/skills/.system/imagegen",
                ],
                user="1000:1000",  # node user → pip --user lands in /home/node/.local/
                workdir="/home/node",
                environment={"HOME": "/home/node"},
            )
            if pip_result.exit_code != 0:
                print(
                    f"[openclaw-k] warning: pip install comfysql failed in "
                    f"{user.container_name} (exit={pip_result.exit_code}): "
                    f"{(pip_result.output or b'').decode('utf-8', errors='replace')[:400]}",
                    flush=True,
                )

        container.restart()
        wait_until_ready(container, timeout_seconds=wait_timeout_seconds)
        container.reload()
    except APIError as exc:
        raise map_create_api_error(exc) from exc

    return {
        "user": username,
        "container": user.container_name,
        "status": "ready",
        "port": port,
        "url": f"http://{connect_host}:{port}/",
        "connect_link": f"http://{connect_host}:{port}/#token={token}",
        "token": token,
        "image": image,
        "config_ingested": config_file_path is not None,
        "config_file_path": str(config_file_path) if config_file_path else None,
    }


def inspect_user_service(*, username: str, connect_host: str = DEFAULT_CONNECT_HOST) -> dict[str, Any]:
    client = get_docker_client()
    container, user = read_user_info(client, username)

    host_port = extract_host_port(container)
    health = container.attrs.get("State", {}).get("Health", {}).get("Status", "n/a")
    token = extract_gateway_token(container)
    config_exists = container.exec_run(["sh", "-lc", "test -f /home/node/.openclaw/openclaw.json"]).exit_code == 0
    ready = is_gateway_live(container) and has_model_synced(container)

    url = f"http://{connect_host}:{host_port}/" if host_port is not None else None
    link = f"http://{connect_host}:{host_port}/#token={token}" if host_port is not None and token else None
    image = container.image.tags[0] if container.image.tags else container.image.id

    return {
        "user": username,
        "container": container.name,
        "image": image,
        "status": container.status,
        "health": health,
        "ready": ready,
        "port": host_port,
        "url": url,
        "connect_link": link,
        "config_file_present": config_exists,
        "volumes": {"config": user.config_volume, "workspace": user.workspace_volume, "skills": user.skills_volume},
    }


def list_users_service() -> list[dict[str, Any]]:
    client = get_docker_client()
    containers = client.containers.list(all=True, filters={"label": "managed-by=openclaw-k"})

    items: list[dict[str, Any]] = []
    for container in containers:
        container.reload()
        user = container.labels.get("openclaw-k.user", "unknown")
        host_port = extract_host_port(container)
        health = container.attrs.get("State", {}).get("Health", {}).get("Status", "n/a")
        ready = is_gateway_live(container) and has_model_synced(container)
        items.append(
            {
                "user": user,
                "container": container.name,
                "status": container.status,
                "health": health,
                "ready": ready,
                "port": host_port,
            }
        )
    return items


def delete_user_service(*, username: str, keep_data: bool = False) -> dict[str, Any]:
    user = UserContainer(username)
    client = get_docker_client()

    try:
        container = client.containers.get(user.container_name)
        container.remove(force=True)
        removed_container = True
    except NotFound as exc:
        raise ServiceError(404, "user_not_found", f"User '{username}' not found.") from exc
    except APIError as exc:
        raise ServiceError(500, "docker_api_error", exc.explanation or str(exc)) from exc

    removed_volumes: list[str] = []
    if not keep_data:
        for volume_name in (user.config_volume, user.workspace_volume, user.skills_volume):
            try:
                volume = client.volumes.get(volume_name)
                volume.remove(force=True)
                removed_volumes.append(volume_name)
            except NotFound:
                continue
            except APIError as exc:
                raise ServiceError(500, "docker_api_error", exc.explanation or str(exc)) from exc

    return {
        "user": username,
        "container_deleted": removed_container,
        "volumes_deleted": removed_volumes,
        "keep_data": keep_data,
    }


def _sync_skills_mirror(container: docker.models.containers.Container, skills_dir_path: Path) -> None:
    wipe = container.exec_run(["sh", "-lc", "mkdir -p /app/skills && rm -rf /app/skills/*"], user="0:0")
    if wipe.exit_code != 0:
        raise ServiceError(500, "skills_sync_failed", "Failed to clean /app/skills before sync.")
    put_directory_into_container(container, skills_dir_path, "/app/skills")


def _sync_workspace_mirror(container: docker.models.containers.Container, workspace_dir_path: Path) -> None:
    wipe = container.exec_run(
        ["sh", "-lc", "mkdir -p /home/node/.openclaw/workspace && find /home/node/.openclaw/workspace -mindepth 1 -delete"],
        user="0:0",
    )
    if wipe.exit_code != 0:
        raise ServiceError(500, "workspace_sync_failed", "Failed to clean workspace before sync.")
    put_directory_into_container(container, workspace_dir_path, "/home/node/.openclaw/workspace")


def update_all_service(
    *,
    users: list[str] | None = None,
    provider: str | None = None,
    restart: bool = True,
    wait_timeout_seconds: int = DEFAULT_WAIT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    client = get_docker_client()
    running = client.containers.list(all=False, filters={"label": "managed-by=openclaw-k"})
    requested_users = set(users or [])
    targets: list[docker.models.containers.Container] = []
    for container in running:
        container_user = container.labels.get("openclaw-k.user")
        if not container_user:
            continue
        if requested_users and container_user not in requested_users:
            continue
        targets.append(container)

    if not targets:
        raise ServiceError(404, "no_running_users", "No running openclaw-k-managed users matched the request.")

    config_file_path = resolve_config_file_path(None, provider)
    skills_dir_path, soul_file_path, workspace_dir_path = resolve_optional_defaults()

    items: list[dict[str, Any]] = []
    updated_count = 0
    failed_count = 0

    for container in targets:
        container.reload()
        user = container.labels.get("openclaw-k.user", "unknown")
        item = {
            "user": user,
            "container": container.name,
            "updated": False,
            "restarted": False,
            "ready": False,
            "applied": {"config": False, "skills": False, "soul": False, "workspace": False},
            "errors": [],
        }
        try:
            if config_file_path:
                config_content = with_openai_api_key(config_file_path.read_bytes())
                put_file_into_container(container, "/home/node/.openclaw", "openclaw.json", config_content)
                item["applied"]["config"] = True

            if skills_dir_path:
                _sync_skills_mirror(container, skills_dir_path)
                item["applied"]["skills"] = True

            if workspace_dir_path:
                _sync_workspace_mirror(container, workspace_dir_path)
                item["applied"]["workspace"] = True

            if soul_file_path:
                put_file_into_container(container, "/home/node/.openclaw/workspace", "SOUL.md", soul_file_path.read_bytes())
                item["applied"]["soul"] = True

            chown_cmd = (
                "if [ -f /home/node/.openclaw/openclaw.json ]; then chown 1000:1000 /home/node/.openclaw/openclaw.json && chmod 600 /home/node/.openclaw/openclaw.json; fi; "
                "if [ -f /home/node/.openclaw/workspace/SOUL.md ]; then chown 1000:1000 /home/node/.openclaw/workspace/SOUL.md && chmod 644 /home/node/.openclaw/workspace/SOUL.md; fi; "
                "if [ -d /home/node/.openclaw/workspace ]; then chown -R 1000:1000 /home/node/.openclaw/workspace; fi; "
                "if [ -d /app/skills ]; then chown -R 1000:1000 /app/skills; fi"
            )
            chown_result = container.exec_run(["sh", "-lc", chown_cmd], user="0:0")
            if chown_result.exit_code != 0:
                raise ServiceError(500, "config_permissions_failed", "Could not set permissions on updated defaults.")

            if restart:
                container.restart()
                item["restarted"] = True
                wait_until_ready(container, timeout_seconds=wait_timeout_seconds)
                item["ready"] = True
            else:
                item["ready"] = is_gateway_live(container) and has_model_synced(container)

            item["updated"] = True
            updated_count += 1
        except ServiceError as exc:
            failed_count += 1
            item["errors"].append(exc.message)
        except APIError as exc:
            failed_count += 1
            item["errors"].append(exc.explanation or str(exc))
        except Exception as exc:
            failed_count += 1
            item["errors"].append(str(exc))
        items.append(item)

    payload = {
        "ok": failed_count == 0,
        "total": len(items),
        "updated": updated_count,
        "failed": failed_count,
        "items": items,
    }
    if updated_count == 0:
        raise ServiceError(500, "all_updates_failed", "Failed to update all targeted containers.", payload)
    return payload


def create_user_cli(args: argparse.Namespace) -> None:
    api_base_url = resolve_api_base_url(args.api_url)
    api_token = resolve_api_token(args.api_token)
    payload = {
        "username": args.username,
        "port": args.port,
        "key": args.key,
        "image": args.image,
        "provider": args.provider,
        "config_file_path": args.config_file,
        "wait_timeout_seconds": args.wait_timeout,
    }
    result = api_request(
        method="POST",
        path="/v1/users",
        api_base_url=api_base_url,
        api_token=api_token,
        json_body={k: v for k, v in payload.items() if v is not None},
    )

    print(f"Created user '{result['user']}' -> container '{result['container']}' (ready)")
    print(f"OpenClaw image: {result['image']}")
    print(f"Port mapping: <managed-by-api>:{result['port']} -> {OPENCLAW_INTERNAL_PORT}")
    if result["config_ingested"]:
        config_path = result.get("config_file_path", "<default-from-api>")
        print(f"Config ingested: {config_path}")
    else:
        print("Config ingested: none (no openclaw.json found)")
    print("\nConnection details:")
    print(f"- URL: {result['url']}")
    print(f"- Token: {result['token']}")
    print(f"- Convenience link: {result['connect_link']}")
    print("- Note: if token auto-fill is not applied by your UI version, paste the token into Settings once.")


def inspect_user_cli(args: argparse.Namespace) -> None:
    api_base_url = resolve_api_base_url(args.api_url)
    api_token = resolve_api_token(args.api_token)
    info = api_request(
        method="GET",
        path=f"/v1/users/{args.username}",
        api_base_url=api_base_url,
        api_token=api_token,
    )
    print(f"User: {info['user']}")
    print(f"Container: {info['container']}")
    print(f"Image: {info['image']}")
    print(f"Status: {info['status']}")
    print(f"Health: {info['health']}")
    print(f"Ready: {'yes' if info['ready'] else 'no'}")
    print(f"Port: {info['port'] if info['port'] is not None else 'n/a'}")
    print(f"URL: {info['url'] or 'n/a'}")
    print(f"Link: {info['connect_link'] or 'n/a'}")
    print(f"Config file present: {'yes' if info['config_file_present'] else 'no'}")
    print(f"Volumes: {info['volumes']['config']}, {info['volumes']['workspace']}")


def list_users_cli(args: argparse.Namespace) -> None:
    api_base_url = resolve_api_base_url(args.api_url)
    api_token = resolve_api_token(args.api_token)
    payload = api_request(
        method="GET",
        path="/v1/users",
        api_base_url=api_base_url,
        api_token=api_token,
    )
    items = payload.get("items", [])
    if not items:
        print("No openclaw-k-managed OpenClaw users found.")
        return

    print("USER\tCONTAINER\tSTATUS\tHEALTH\tREADY\tPORT")
    for item in items:
        port_value = item["port"] if item["port"] is not None else "-"
        print(
            f"{item['user']}\t{item['container']}\t{item['status']}\t{item['health']}\t"
            f"{'yes' if item['ready'] else 'no'}\t{port_value}"
        )


def delete_user_cli(args: argparse.Namespace) -> None:
    api_base_url = resolve_api_base_url(args.api_url)
    api_token = resolve_api_token(args.api_token)
    result = api_request(
        method="DELETE",
        path=f"/v1/users/{args.username}",
        api_base_url=api_base_url,
        api_token=api_token,
        query_params={"keep_data": str(args.keep_data).lower()},
    )
    print(f"Deleted container 'openclaw-{result['user']}'.")
    if args.keep_data:
        print("Kept volumes (--keep-data).")
    else:
        if result["volumes_deleted"]:
            for volume_name in result["volumes_deleted"]:
                print(f"Deleted volume '{volume_name}'.")
        else:
            print("No volumes removed (not found).")


def update_all_cli(args: argparse.Namespace) -> None:
    api_base_url = resolve_api_base_url(args.api_url)
    api_token = resolve_api_token(args.api_token)
    payload = {
        "users": args.users or None,
        "provider": args.provider,
        "restart": not args.no_restart,
        "wait_timeout_seconds": args.wait_timeout,
    }
    result = api_request(
        method="POST",
        path="/v1/update/all",
        api_base_url=api_base_url,
        api_token=api_token,
        json_body={k: v for k, v in payload.items() if v is not None},
    )

    print(
        f"Update complete: total={result['total']} updated={result['updated']} failed={result['failed']}"
    )
    for item in result.get("items", []):
        status = "ok" if item.get("updated") else "failed"
        print(
            f"- {item.get('user')} ({item.get('container')}): {status}, "
            f"applied={item.get('applied')}, restarted={item.get('restarted')}, ready={item.get('ready')}"
        )
        for err in item.get("errors", []):
            print(f"  error: {err}")


def build_auth_dependency(admin_token: str):
    def require_bearer(authorization: str | None = Header(default=None, alias="Authorization")) -> None:
        if not authorization:
            raise HTTPException(status_code=401, detail=error_payload("auth_missing", "Missing Authorization header."))
        if not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail=error_payload("auth_invalid", "Expected Bearer token."))

        presented = authorization[len("Bearer ") :].strip()
        if not hmac.compare_digest(presented, admin_token):
            raise HTTPException(status_code=403, detail=error_payload("auth_forbidden", "Invalid API token."))

    return require_bearer


def create_api_app(admin_token: str) -> FastAPI:
    app = FastAPI(title="openclaw-k API", version="1.0.0")
    require_bearer = build_auth_dependency(admin_token)

    @app.exception_handler(HTTPException)
    async def http_exception_handler(_, exc: HTTPException) -> JSONResponse:
        if isinstance(exc.detail, dict) and "error" in exc.detail:
            payload = exc.detail
        else:
            payload = error_payload("http_error", str(exc.detail))
        return JSONResponse(status_code=exc.status_code, content=payload)

    @app.exception_handler(ServiceError)
    async def service_exception_handler(_, exc: ServiceError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=error_payload(exc.code, exc.message, exc.details),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(_, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content=error_payload("invalid_request", "Request validation failed.", exc.errors()),
        )

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "service": "openclaw-k-api"}

    @app.post("/v1/users", response_model=CreateUserResponse, status_code=201, dependencies=[Depends(require_bearer)])
    def create_user_endpoint(request: CreateUserRequest, http_request: Request) -> dict[str, Any]:
        connect_host = os.getenv("OPENCLAW_K_CONNECT_HOST", http_request.url.hostname or DEFAULT_CONNECT_HOST)
        return create_user_service(
            username=request.username,
            port=request.port,
            key=request.key,
            image=request.image,
            provider=request.provider,
            config_file_arg=request.config_file_path,
            wait_timeout_seconds=request.wait_timeout_seconds,
            connect_host=connect_host,
            publish_bind_ip=DEFAULT_PUBLISH_BIND_IP,
        )

    @app.get("/v1/users", response_model=ListUsersResponse, dependencies=[Depends(require_bearer)])
    def list_users_endpoint() -> dict[str, Any]:
        return {"items": list_users_service()}

    @app.get("/v1/users/{username}", response_model=UserInspectResponse, dependencies=[Depends(require_bearer)])
    def inspect_user_endpoint(username: str, http_request: Request) -> dict[str, Any]:
        connect_host = os.getenv("OPENCLAW_K_CONNECT_HOST", http_request.url.hostname or DEFAULT_CONNECT_HOST)
        return inspect_user_service(username=username, connect_host=connect_host)

    @app.delete("/v1/users/{username}", response_model=DeleteUserResponse, dependencies=[Depends(require_bearer)])
    def delete_user_endpoint(username: str, keep_data: bool = False) -> dict[str, Any]:
        return delete_user_service(username=username, keep_data=keep_data)

    @app.post("/v1/update/all", response_model=UpdateAllResponse, dependencies=[Depends(require_bearer)])
    def update_all_endpoint(request: UpdateAllRequest) -> dict[str, Any]:
        return update_all_service(
            users=request.users,
            provider=request.provider,
            restart=request.restart,
            wait_timeout_seconds=request.wait_timeout_seconds,
        )

    @app.post("/v1/users/{username}/chat", dependencies=[Depends(require_bearer)])
    def chat_endpoint(username: str, request: ChatRequest) -> Any:
        """Send a chat message to a user's container, with optional image support.

        Images in messages are extracted and forwarded to Ollama's native /api/chat
        endpoint which supports vision. Text-only messages go through openclaw's
        /v1/chat/completions gateway.
        """
        import httpx

        user = UserContainer(username)
        try:
            container = docker.from_env().containers.get(user.container_name)
        except NotFound:
            raise ServiceError(404, "user_not_found", f"User '{username}' not found.")

        # Get container port
        ports = container.ports
        port = None
        for key, bindings in ports.items():
            if bindings:
                port = int(bindings[0]["HostPort"])
                break
        if not port:
            raise ServiceError(500, "no_port", f"Container '{username}' has no mapped port.")

        # Get token from container env
        env_list = container.attrs.get("Config", {}).get("Env", [])
        token = ""
        for item in env_list:
            if item.startswith("OPENCLAW_GATEWAY_TOKEN="):
                token = item.split("=", 1)[1]
                break

        # Check if any message has images (inline base64 or image_url content parts)
        has_images = False
        ollama_messages = []

        for msg in request.messages:
            ollama_msg: dict[str, Any] = {"role": msg.role}

            if isinstance(msg.content, list):
                # OpenAI multi-content format — extract text and images
                text_parts = []
                image_parts = []
                for part in msg.content:
                    if isinstance(part, dict):
                        if part.get("type") == "text":
                            text_parts.append(part.get("text", ""))
                        elif part.get("type") == "image_url":
                            url = part.get("image_url", {}).get("url", "")
                            if url.startswith("data:"):
                                # Extract base64 from data URL
                                b64 = url.split(",", 1)[1] if "," in url else ""
                                image_parts.append(b64)
                                has_images = True
                ollama_msg["content"] = "\n".join(text_parts) if text_parts else ""
                if image_parts:
                    ollama_msg["images"] = image_parts
            elif msg.images:
                # Direct images array
                ollama_msg["content"] = msg.content if isinstance(msg.content, str) else str(msg.content)
                ollama_msg["images"] = msg.images
                has_images = True
            else:
                ollama_msg["content"] = msg.content if isinstance(msg.content, str) else str(msg.content)

            ollama_messages.append(ollama_msg)

        if has_images:
            # Route through Ollama native /api/chat for vision support
            ollama_url = os.getenv("OPENCLAW_K_OLLAMA_URL", "http://172.17.0.1:11434")
            ollama_model = os.getenv("OPENCLAW_K_OLLAMA_MODEL", "gemma4:e4b")
            ollama_headers = {}
            ollama_auth_token = os.getenv("OPENCLAW_K_OLLAMA_AUTH_TOKEN", "")
            if ollama_auth_token:
                ollama_headers["Authorization"] = f"Bearer {ollama_auth_token}"

            payload = {
                "model": request.model if request.model != "openclaw" else ollama_model,
                "messages": ollama_messages,
                "stream": request.stream,
            }

            try:
                resp = httpx.post(
                    f"{ollama_url}/api/chat",
                    json=payload,
                    headers=ollama_headers if ollama_headers else None,
                    timeout=120.0,
                )
                return resp.json()
            except Exception as exc:
                raise ServiceError(502, "ollama_error", f"Ollama request failed: {exc}")
        else:
            # Text-only: route through openclaw gateway's /v1/chat/completions
            payload = {
                "model": request.model,
                "messages": [{"role": m.role, "content": m.content} for m in request.messages],
                "stream": request.stream,
            }
            if request.user:
                payload["user"] = request.user

            chat_host = os.getenv("OPENCLAW_K_CONNECT_HOST", "127.0.0.1")
            try:
                resp = httpx.post(
                    f"http://{chat_host}:{port}/v1/chat/completions",
                    json=payload,
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=120.0,
                )
                return resp.json()
            except Exception as exc:
                raise ServiceError(502, "openclaw_error", f"OpenClaw request failed: {exc}")

    @app.get("/v1/users/{username}/device", response_model=DeviceIdentityResponse, dependencies=[Depends(require_bearer)])
    def get_device_identity_endpoint(username: str) -> dict[str, Any]:
        """Return the device identity and pairing info for a running container.

        Intended to be called by Maestro immediately after successful provisioning
        so it can populate its `openclaw_instances` table (deviceId, operatorToken,
        device keypair) and unlock WebSocket-based chat without a manual SSH step.

        Returns 404 if the container does not exist, 409 if identity files have
        not yet been written (container still initializing — caller should retry).
        The response body contains secrets; do not log it.
        """
        return get_device_identity_service(username=username)

    @app.put("/v1/users/{username}/files", response_model=WriteFileResponse, dependencies=[Depends(require_bearer)])
    def write_file_endpoint(username: str, request: WriteFileRequest) -> dict[str, Any]:
        """Write a file into a running container's workspace directory."""
        import base64
        import posixpath

        user = UserContainer(username)
        try:
            container = docker.from_env().containers.get(user.container_name)
        except NotFound:
            raise ServiceError(404, "user_not_found", f"User '{username}' not found.")

        # Decode base64 content
        try:
            content_bytes = base64.b64decode(request.content)
        except Exception:
            raise ServiceError(400, "invalid_content", "Content must be valid base64.")

        # Ensure parent directories exist via the file path
        workspace_base = "/home/node/.openclaw/workspace"
        dest_path = posixpath.join(workspace_base, request.path)
        dest_dir = posixpath.dirname(dest_path)
        file_name = posixpath.basename(dest_path)

        # Create parent dirs if needed
        if dest_dir != workspace_base:
            container.exec_run(["mkdir", "-p", dest_dir])

        # Write file using Docker's put_archive (same approach as SOUL injection)
        put_file_into_container(container, dest_dir, file_name, content_bytes)

        return {"status": "ok", "path": request.path, "user": username}

    return app


def api_serve_cli(args: argparse.Namespace) -> None:
    if getattr(args, "config", None):
        config_path = Path(args.config).expanduser().resolve()
        if config_path.is_file():
            config, loaded_path = load_up_config(str(config_path))
            config_dir = loaded_path.parent
            default_provider = config.providers.profiles[config.providers.default]
            provider_host_file = resolve_existing_file(
                default_provider.file,
                config_dir=config_dir,
                required=True,
                field_name=f"providers.profiles.{config.providers.default}.file",
            )
            assert provider_host_file is not None
            os.environ[DEFAULT_PROVIDER_FILE_ENV] = str(provider_host_file)
            profile_paths: dict[str, str] = {}
            for profile_name, profile in config.providers.profiles.items():
                resolved = resolve_existing_file(
                    profile.file,
                    config_dir=config_dir,
                    required=True,
                    field_name=f"providers.profiles.{profile_name}.file",
                )
                assert resolved is not None
                profile_paths[profile_name] = str(resolved)
                profile_paths[Path(profile.file).stem] = str(resolved)
                profile_paths[Path(profile.file).name] = str(resolved)
            os.environ[PROVIDER_PROFILES_ENV] = json.dumps(profile_paths)
            os.environ["OPENCLAW_K_PUBLISH_BIND_IP"] = config.defaults.publish_bind_ip
            os.environ["OPENCLAW_K_CONNECT_HOST"] = config.defaults.connect_host

            skills_host_dir = resolve_existing_dir(
                config.defaults.skills_dir,
                config_dir=config_dir,
                field_name="defaults.skills_dir",
            )
            soul_host_file = (
                resolve_existing_file(
                    config.defaults.soul_file,
                    config_dir=config_dir,
                    required=False,
                    field_name="defaults.soul_file",
                )
                if config.defaults.soul_file
                else None
            )
            workspace_host_dir = resolve_existing_dir(
                config.defaults.workspace_dir,
                config_dir=config_dir,
                field_name="defaults.workspace_dir",
            )
            if skills_host_dir:
                os.environ[DEFAULT_SKILLS_DIR_ENV] = str(skills_host_dir)
            else:
                os.environ.pop(DEFAULT_SKILLS_DIR_ENV, None)
            if soul_host_file:
                os.environ[DEFAULT_SOUL_FILE_ENV] = str(soul_host_file)
            else:
                os.environ.pop(DEFAULT_SOUL_FILE_ENV, None)
            if workspace_host_dir:
                os.environ[DEFAULT_WORKSPACE_DIR_ENV] = str(workspace_host_dir)
            else:
                os.environ.pop(DEFAULT_WORKSPACE_DIR_ENV, None)

    token = args.token or os.getenv("OPENCLAW_K_API_TOKEN")
    if not token:
        raise ServiceError(400, "token_required", "Provide --token or set OPENCLAW_K_API_TOKEN.")

    app = create_api_app(token)
    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port)


def wait_for_api_health(host: str, port: int, timeout_seconds: int = 45) -> None:
    url = f"http://127.0.0.1:{port}/health" if host in ("0.0.0.0", "::") else f"http://{host}:{port}/health"
    deadline = time.time() + timeout_seconds
    last_error = ""
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, ConnectionResetError, OSError) as exc:
            last_error = str(exc)
        time.sleep(1)
    raise ServiceError(500, "api_not_ready", f"API did not become healthy at {url}. Last error: {last_error}")


def up_cli(args: argparse.Namespace) -> None:
    config, config_path = load_up_config(args.config)
    config_dir = config_path.parent

    api_token = os.getenv("OPENCLAW_K_API_TOKEN")
    if not api_token:
        raise ServiceError(400, "token_required", "OPENCLAW_K_API_TOKEN is required for 'openclaw-k up'.")

    provider_host_files: dict[str, Path] = {}
    for profile_name, profile in config.providers.profiles.items():
        resolved = resolve_existing_file(
            profile.file,
            config_dir=config_dir,
            required=True,
            field_name=f"providers.profiles.{profile_name}.file",
        )
        assert resolved is not None
        provider_host_files[profile_name] = resolved
    provider_host_file = provider_host_files[config.providers.default]

    skills_host_dir = resolve_existing_dir(config.defaults.skills_dir, config_dir=config_dir, field_name="defaults.skills_dir")
    soul_host_file = (
        resolve_existing_file(config.defaults.soul_file, config_dir=config_dir, required=False, field_name="defaults.soul_file")
        if config.defaults.soul_file
        else None
    )
    workspace_host_dir = resolve_existing_dir(
        config.defaults.workspace_dir,
        config_dir=config_dir,
        field_name="defaults.workspace_dir",
    )

    docker_client = get_docker_client()

    if not args.no_build:
        try:
            docker_client.images.build(path=str(config_dir), tag=config.docker.image_tag)
        except APIError as exc:
            raise ServiceError(500, "docker_build_failed", exc.explanation or str(exc)) from exc

    # Idempotent replace of API container.
    try:
        existing = docker_client.containers.get(config.docker.container_name)
        existing.remove(force=True)
    except NotFound:
        pass
    except APIError as exc:
        raise ServiceError(500, "docker_api_error", exc.explanation or str(exc)) from exc

    provider_container_file = f"{INTERNAL_DEFAULTS_DIR}/provider-{config.providers.default}.json"
    provider_container_paths: dict[str, str] = {}
    volumes: dict[str, dict[str, str]] = {
        "/var/run/docker.sock": {"bind": "/var/run/docker.sock", "mode": "rw"},
    }
    for profile_name, host_file in provider_host_files.items():
        container_file = f"{INTERNAL_DEFAULTS_DIR}/provider-{profile_name}.json"
        volumes[str(host_file)] = {"bind": container_file, "mode": "ro"}
        provider_container_paths[profile_name] = container_file
        source_profile = config.providers.profiles[profile_name]
        provider_container_paths[Path(source_profile.file).stem] = container_file
        provider_container_paths[Path(source_profile.file).name] = container_file
    if skills_host_dir:
        volumes[str(skills_host_dir)] = {"bind": f"{INTERNAL_DEFAULTS_DIR}/skills", "mode": "ro"}
    if soul_host_file:
        volumes[str(soul_host_file)] = {"bind": f"{INTERNAL_DEFAULTS_DIR}/SOUL.md", "mode": "ro"}
    if workspace_host_dir:
        volumes[str(workspace_host_dir)] = {"bind": f"{INTERNAL_DEFAULTS_DIR}/workspace", "mode": "ro"}

    try:
        docker_client.containers.run(
            config.docker.image_tag,
            name=config.docker.container_name,
            detach=True,
            restart_policy={"Name": "unless-stopped"},
            ports={f"{config.api.port}/tcp": ("0.0.0.0", config.api.port)},
            environment={
                "OPENCLAW_K_API_TOKEN": api_token,
                "OPENCLAW_K_DEFAULT_PROVIDER_FILE": provider_container_file,
                PROVIDER_PROFILES_ENV: json.dumps(provider_container_paths),
                "OPENCLAW_K_PUBLISH_BIND_IP": config.defaults.publish_bind_ip,
                "OPENCLAW_K_CONNECT_HOST": config.defaults.connect_host,
                **(
                    {"OPENCLAW_K_DEFAULT_SKILLS_DIR": f"{INTERNAL_DEFAULTS_DIR}/skills"}
                    if skills_host_dir
                    else {}
                ),
                **(
                    {"OPENCLAW_K_DEFAULT_SOUL_FILE": f"{INTERNAL_DEFAULTS_DIR}/SOUL.md"}
                    if soul_host_file
                    else {}
                ),
                **(
                    {"OPENCLAW_K_DEFAULT_WORKSPACE_DIR": f"{INTERNAL_DEFAULTS_DIR}/workspace"}
                    if workspace_host_dir
                    else {}
                ),
            },
            volumes=volumes,
            command=["api", "serve", "--host", config.api.host, "--port", str(config.api.port)],
        )
    except APIError as exc:
        raise ServiceError(500, "docker_run_failed", exc.explanation or str(exc)) from exc

    wait_for_api_health(config.api.host, config.api.port)

    print("openclaw-k API is up.")
    print(f"- Config: {config_path}")
    print(f"- Container: {config.docker.container_name}")
    print(f"- Image: {config.docker.image_tag}")
    print(f"- API: http://{config.defaults.connect_host}:{config.api.port}/health")
    print(f"- Default provider profile: {config.providers.default}")
    print(f"- Provider file: {provider_host_file}")
    print(f"- Skills dir: {skills_host_dir if skills_host_dir else 'not set or missing (skipped)'}")
    print(f"- SOUL file: {soul_host_file if soul_host_file else 'not set or missing (skipped)'}")
    print(f"- Workspace dir: {workspace_host_dir if workspace_host_dir else 'not set or missing (skipped)'}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="openclaw-k",
        description="Manage per-user OpenClaw Docker containers.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    create_parser = subparsers.add_parser("create", help="Create resources")
    create_sub = create_parser.add_subparsers(dest="resource", required=True)
    create_user_parser = create_sub.add_parser("user", help="Create an OpenClaw user container")
    create_user_parser.add_argument("username", help="User identifier (used in container/volume names)")
    create_user_parser.add_argument("--port", type=int, required=True, help="Host port for this user")
    create_user_parser.add_argument("--key", help="Optional OpenClaw gateway token (auto-generated if omitted)")
    create_user_parser.add_argument(
        "--provider",
        help="Provider profile override from openclaw-k.yaml (supports profile name or provider file stem, e.g. openai or openclaw-openai).",
    )
    create_user_parser.add_argument(
        "--config-file",
        help="Path to openclaw.json to ingest. If omitted, './openclaw.json' is used when present.",
    )
    create_user_parser.add_argument(
        "--wait-timeout",
        type=int,
        default=DEFAULT_WAIT_TIMEOUT_SECONDS,
        help=f"Seconds to wait for OpenClaw readiness before failing (default: {DEFAULT_WAIT_TIMEOUT_SECONDS}).",
    )
    create_user_parser.add_argument(
        "--image",
        default=DEFAULT_OPENCLAW_IMAGE,
        help=f"OpenClaw image to run (default: {DEFAULT_OPENCLAW_IMAGE})",
    )
    create_user_parser.add_argument(
        "--api-url",
        help=f"openclaw-k API base URL (default: {DEFAULT_API_URL_ENV} or {DEFAULT_API_BASE_URL})",
    )
    create_user_parser.add_argument(
        "--api-token",
        help="API Bearer token (fallback: OPENCLAW_K_API_TOKEN env)",
    )
    create_user_parser.set_defaults(func=create_user_cli)

    delete_parser = subparsers.add_parser("delete", help="Delete resources")
    delete_sub = delete_parser.add_subparsers(dest="resource", required=True)
    delete_user_parser = delete_sub.add_parser("user", help="Delete an OpenClaw user container")
    delete_user_parser.add_argument("username", help="User identifier")
    delete_user_parser.add_argument(
        "--keep-data",
        action="store_true",
        help="Delete only the container and keep persistent volumes",
    )
    delete_user_parser.add_argument(
        "--api-url",
        help=f"openclaw-k API base URL (default: {DEFAULT_API_URL_ENV} or {DEFAULT_API_BASE_URL})",
    )
    delete_user_parser.add_argument(
        "--api-token",
        help="API Bearer token (fallback: OPENCLAW_K_API_TOKEN env)",
    )
    delete_user_parser.set_defaults(func=delete_user_cli)

    inspect_parser = subparsers.add_parser("inspect", help="Inspect resources")
    inspect_sub = inspect_parser.add_subparsers(dest="resource", required=True)
    inspect_user_parser = inspect_sub.add_parser("user", help="Inspect an OpenClaw user container")
    inspect_user_parser.add_argument("username", help="User identifier")
    inspect_user_parser.add_argument(
        "--api-url",
        help=f"openclaw-k API base URL (default: {DEFAULT_API_URL_ENV} or {DEFAULT_API_BASE_URL})",
    )
    inspect_user_parser.add_argument(
        "--api-token",
        help="API Bearer token (fallback: OPENCLAW_K_API_TOKEN env)",
    )
    inspect_user_parser.set_defaults(func=inspect_user_cli)

    list_parser = subparsers.add_parser("list", help="List resources")
    list_sub = list_parser.add_subparsers(dest="resource", required=True)
    list_user_parser = list_sub.add_parser("users", help="List openclaw-k-managed OpenClaw users")
    list_user_parser.add_argument(
        "--api-url",
        help=f"openclaw-k API base URL (default: {DEFAULT_API_URL_ENV} or {DEFAULT_API_BASE_URL})",
    )
    list_user_parser.add_argument(
        "--api-token",
        help="API Bearer token (fallback: OPENCLAW_K_API_TOKEN env)",
    )
    list_user_parser.set_defaults(func=list_users_cli)

    update_parser = subparsers.add_parser("update", help="Update running resources")
    update_sub = update_parser.add_subparsers(dest="resource", required=True)
    update_all_parser = update_sub.add_parser("all", help="Update all running managed users with latest config/skills/SOUL")
    update_all_parser.add_argument(
        "--user",
        dest="users",
        action="append",
        default=[],
        help="Optional user filter. Repeat to target multiple users.",
    )
    update_all_parser.add_argument(
        "--provider",
        help="Optional provider override alias (same resolution as create --provider).",
    )
    update_all_parser.add_argument(
        "--no-restart",
        action="store_true",
        help="Apply files without restarting containers.",
    )
    update_all_parser.add_argument(
        "--wait-timeout",
        type=int,
        default=DEFAULT_WAIT_TIMEOUT_SECONDS,
        help=f"Seconds to wait for readiness after restart (default: {DEFAULT_WAIT_TIMEOUT_SECONDS}).",
    )
    update_all_parser.add_argument(
        "--api-url",
        help=f"openclaw-k API base URL (default: {DEFAULT_API_URL_ENV} or {DEFAULT_API_BASE_URL})",
    )
    update_all_parser.add_argument(
        "--api-token",
        help="API Bearer token (fallback: OPENCLAW_K_API_TOKEN env)",
    )
    update_all_parser.set_defaults(func=update_all_cli)

    api_parser = subparsers.add_parser("api", help="Run HTTP API server")
    api_sub = api_parser.add_subparsers(dest="resource", required=True)
    api_serve_parser = api_sub.add_parser("serve", help="Serve openclaw-k HTTP API")
    api_serve_parser.add_argument("--host", default=DEFAULT_API_HOST, help=f"Listen host (default: {DEFAULT_API_HOST})")
    api_serve_parser.add_argument("--port", type=int, default=DEFAULT_API_PORT, help=f"Listen port (default: {DEFAULT_API_PORT})")
    api_serve_parser.add_argument(
        "--token",
        help="Admin API Bearer token (fallback: OPENCLAW_K_API_TOKEN env)",
    )
    api_serve_parser.add_argument(
        "--config",
        default=DEFAULT_UP_CONFIG_FILE,
        help=f"Optional YAML defaults file for provider/skills/soul (default: {DEFAULT_UP_CONFIG_FILE})",
    )
    api_serve_parser.set_defaults(func=api_serve_cli)

    up_parser = subparsers.add_parser("up", help="Bootstrap and run API container from config.yaml")
    up_parser.add_argument(
        "--config",
        default=DEFAULT_UP_CONFIG_FILE,
        help=f"Path to YAML config (default: {DEFAULT_UP_CONFIG_FILE})",
    )
    up_parser.add_argument(
        "--no-build",
        action="store_true",
        help="Skip docker image build and reuse existing image tag from config",
    )
    up_parser.set_defaults(func=up_cli)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.func(args)
    except ServiceError as exc:
        print(exc.message, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
