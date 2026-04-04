#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hmac
import io
import os
import secrets
import sys
import tarfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import docker
from docker.errors import APIError, DockerException, NotFound
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field

DEFAULT_OPENCLAW_IMAGE = "ghcr.io/openclaw/openclaw:latest"
OPENCLAW_INTERNAL_PORT = 18789
DEFAULT_API_HOST = "127.0.0.1"
DEFAULT_API_PORT = 8787
DEFAULT_WAIT_TIMEOUT_SECONDS = 240


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


class CreateUserRequest(BaseModel):
    username: str = Field(min_length=1)
    port: int = Field(ge=1, le=65535)
    key: str | None = None
    image: str = DEFAULT_OPENCLAW_IMAGE
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


def resolve_config_file_path(config_file_arg: str | None) -> Path | None:
    if config_file_arg:
        config_path = Path(config_file_arg).expanduser().resolve()
        if not config_path.is_file():
            raise ServiceError(400, "invalid_config_file", f"Config file not found: {config_path}")
        return config_path

    default_path = (Path.cwd() / "openclaw.json").resolve()
    if default_path.is_file():
        return default_path
    return None


def put_file_into_container(container: docker.models.containers.Container, dest_dir: str, name: str, content: bytes) -> None:
    archive_stream = io.BytesIO()
    with tarfile.open(fileobj=archive_stream, mode="w") as tar:
        info = tarfile.TarInfo(name=name)
        info.size = len(content)
        info.mode = 0o600
        tar.addfile(info, io.BytesIO(content))
    archive_stream.seek(0)
    ok = container.put_archive(dest_dir, archive_stream.getvalue())
    if not ok:
        raise ServiceError(500, "container_copy_failed", f"Could not copy '{name}' into container at '{dest_dir}'.")


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
) -> None:
    run_in_seed_container(
        client,
        image,
        volume_mounts,
        ["sh", "-lc", "mkdir -p /home/node/.openclaw/workspace && chown -R 1000:1000 /home/node/.openclaw"],
    )
    if not config_file_path:
        return

    seed = client.containers.create(
        image=image,
        command=["sh", "-lc", "sleep 60"],
        user="0:0",
        volumes=volume_mounts,
    )
    try:
        seed.start()
        put_file_into_container(seed, "/home/node/.openclaw", "openclaw.json", config_file_path.read_bytes())
        chown_result = seed.exec_run(
            ["sh", "-lc", "chown 1000:1000 /home/node/.openclaw/openclaw.json && chmod 600 /home/node/.openclaw/openclaw.json"]
        )
        if chown_result.exit_code != 0:
            raise ServiceError(500, "config_permissions_failed", "Could not set permissions on ingested openclaw.json")
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


def safe_set_config(container: docker.models.containers.Container, path: str, value: str) -> None:
    result = container.exec_run(
        ["node", "dist/index.js", "config", "set", path, value, "--strict-json"],
        demux=False,
    )
    if result.exit_code != 0:
        raise ServiceError(500, "config_set_failed", f"Failed to set config '{path}'.")


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
    config_file_arg: str | None = None,
    wait_timeout_seconds: int = DEFAULT_WAIT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    user = UserContainer(username)
    token = key or secrets.token_urlsafe(24)
    config_file_path = resolve_config_file_path(config_file_arg)
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

        volume_mounts = {
            user.config_volume: {"bind": "/home/node/.openclaw", "mode": "rw"},
            user.workspace_volume: {"bind": "/home/node/.openclaw/workspace", "mode": "rw"},
        }
        seed_openclaw_state(client, image, volume_mounts, config_file_path)

        container = client.containers.run(
            image,
            name=user.container_name,
            detach=True,
            restart_policy={"Name": "unless-stopped"},
            ports={f"{OPENCLAW_INTERNAL_PORT}/tcp": ("127.0.0.1", port)},
            environment={"OPENCLAW_GATEWAY_TOKEN": token},
            command=["node", "openclaw.mjs", "gateway", "--allow-unconfigured", "--bind", "lan"],
            labels={"app": "openclaw", "managed-by": "openclaw-k", "openclaw-k.user": username},
            volumes=volume_mounts,
        )

        safe_set_config(container, "gateway.controlUi.allowedOrigins", f'["http://127.0.0.1:{port}","http://localhost:{port}"]')
        safe_set_config(container, "gateway.controlUi.dangerouslyAllowHostHeaderOriginFallback", "true")
        safe_set_config(container, "gateway.controlUi.dangerouslyDisableDeviceAuth", "true")
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
        "url": f"http://127.0.0.1:{port}/",
        "connect_link": f"http://127.0.0.1:{port}/#token={token}",
        "token": token,
        "image": image,
        "config_ingested": config_file_path is not None,
        "config_file_path": str(config_file_path) if config_file_path else None,
    }


def inspect_user_service(*, username: str) -> dict[str, Any]:
    client = get_docker_client()
    container, user = read_user_info(client, username)

    host_port = extract_host_port(container)
    health = container.attrs.get("State", {}).get("Health", {}).get("Status", "n/a")
    token = extract_gateway_token(container)
    config_exists = container.exec_run(["sh", "-lc", "test -f /home/node/.openclaw/openclaw.json"]).exit_code == 0
    ready = is_gateway_live(container) and has_model_synced(container)

    url = f"http://127.0.0.1:{host_port}/" if host_port is not None else None
    link = f"http://127.0.0.1:{host_port}/#token={token}" if host_port is not None and token else None
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
        "volumes": {"config": user.config_volume, "workspace": user.workspace_volume},
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
        for volume_name in (user.config_volume, user.workspace_volume):
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


def create_user_cli(args: argparse.Namespace) -> None:
    result = create_user_service(
        username=args.username,
        port=args.port,
        key=args.key,
        image=args.image,
        config_file_arg=args.config_file,
        wait_timeout_seconds=args.wait_timeout,
    )

    print(f"Created user '{result['user']}' -> container '{result['container']}' (ready)")
    print(f"OpenClaw image: {result['image']}")
    print(f"Port mapping: 127.0.0.1:{result['port']} -> {OPENCLAW_INTERNAL_PORT}")
    if result["config_ingested"]:
        print(f"Config ingested: {result['config_file_path']}")
    else:
        print("Config ingested: none (no openclaw.json found)")
    print("\nConnection details:")
    print(f"- URL: {result['url']}")
    print(f"- Token: {result['token']}")
    print(f"- Convenience link: {result['connect_link']}")
    print("- Note: if token auto-fill is not applied by your UI version, paste the token into Settings once.")


def inspect_user_cli(args: argparse.Namespace) -> None:
    info = inspect_user_service(username=args.username)
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


def list_users_cli(_: argparse.Namespace) -> None:
    items = list_users_service()
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
    result = delete_user_service(username=args.username, keep_data=args.keep_data)
    print(f"Deleted container 'openclaw-{result['user']}'.")
    if args.keep_data:
        print("Kept volumes (--keep-data).")
    else:
        if result["volumes_deleted"]:
            for volume_name in result["volumes_deleted"]:
                print(f"Deleted volume '{volume_name}'.")
        else:
            print("No volumes removed (not found).")


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
    def create_user_endpoint(request: CreateUserRequest) -> dict[str, Any]:
        return create_user_service(
            username=request.username,
            port=request.port,
            key=request.key,
            image=request.image,
            config_file_arg=request.config_file_path,
            wait_timeout_seconds=request.wait_timeout_seconds,
        )

    @app.get("/v1/users", response_model=ListUsersResponse, dependencies=[Depends(require_bearer)])
    def list_users_endpoint() -> dict[str, Any]:
        return {"items": list_users_service()}

    @app.get("/v1/users/{username}", response_model=UserInspectResponse, dependencies=[Depends(require_bearer)])
    def inspect_user_endpoint(username: str) -> dict[str, Any]:
        return inspect_user_service(username=username)

    @app.delete("/v1/users/{username}", response_model=DeleteUserResponse, dependencies=[Depends(require_bearer)])
    def delete_user_endpoint(username: str, keep_data: bool = False) -> dict[str, Any]:
        return delete_user_service(username=username, keep_data=keep_data)

    return app


def api_serve_cli(args: argparse.Namespace) -> None:
    token = args.token or os.getenv("OPENCLAW_K_API_TOKEN")
    if not token:
        raise ServiceError(400, "token_required", "Provide --token or set OPENCLAW_K_API_TOKEN.")

    app = create_api_app(token)
    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port)


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
    delete_user_parser.set_defaults(func=delete_user_cli)

    inspect_parser = subparsers.add_parser("inspect", help="Inspect resources")
    inspect_sub = inspect_parser.add_subparsers(dest="resource", required=True)
    inspect_user_parser = inspect_sub.add_parser("user", help="Inspect an OpenClaw user container")
    inspect_user_parser.add_argument("username", help="User identifier")
    inspect_user_parser.set_defaults(func=inspect_user_cli)

    list_parser = subparsers.add_parser("list", help="List resources")
    list_sub = list_parser.add_subparsers(dest="resource", required=True)
    list_user_parser = list_sub.add_parser("users", help="List openclaw-k-managed OpenClaw users")
    list_user_parser.set_defaults(func=list_users_cli)

    api_parser = subparsers.add_parser("api", help="Run HTTP API server")
    api_sub = api_parser.add_subparsers(dest="resource", required=True)
    api_serve_parser = api_sub.add_parser("serve", help="Serve openclaw-k HTTP API")
    api_serve_parser.add_argument("--host", default=DEFAULT_API_HOST, help=f"Listen host (default: {DEFAULT_API_HOST})")
    api_serve_parser.add_argument("--port", type=int, default=DEFAULT_API_PORT, help=f"Listen port (default: {DEFAULT_API_PORT})")
    api_serve_parser.add_argument(
        "--token",
        help="Admin API Bearer token (fallback: OPENCLAW_K_API_TOKEN env)",
    )
    api_serve_parser.set_defaults(func=api_serve_cli)

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
