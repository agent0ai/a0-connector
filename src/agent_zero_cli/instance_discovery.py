from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping, TypeAlias
from urllib.parse import unquote

import httpx

from agent_zero_cli.text_utils import as_mapping, strip_text


DiscoveryStatus: TypeAlias = Literal["loading", "ready", "empty", "unavailable", "error"]

_AGENT_ZERO_COMMAND_MARKERS = ("/exe/initialize.sh", "run_ui.py")
_LOCAL_BINDING_HOSTS = {"", "0.0.0.0", "::", "[::]"}
_LAUNCHER_INSTANCE_NAME_LABEL = "a0.launcher.instanceName"
_WINDOWS_WSL_ENGINE_API = "http://127.0.0.1:23750"
_DOCKER_CONTEXTS_META_DIR = Path(".docker/contexts/meta")


@dataclass(frozen=True)
class DiscoveredInstance:
    id: str
    name: str
    url: str
    host_port: str
    source: str = "docker"
    status_text: str = ""


@dataclass(frozen=True)
class DiscoveryResult:
    status: DiscoveryStatus
    instances: tuple[DiscoveredInstance, ...] = ()
    detail: str = ""


@dataclass(frozen=True)
class _CommandResult:
    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class _DockerCommandBackend:
    args: tuple[str, ...]
    source: str = "docker"


def _find_docker_cli() -> str | None:
    return shutil.which("docker")


def _find_wsl_cli() -> str | None:
    return shutil.which("wsl.exe") or shutil.which("wsl")


def _docker_command_backends(*, include_wsl: bool = True) -> tuple[_DockerCommandBackend, ...]:
    backends: list[_DockerCommandBackend] = []
    docker_cli = _find_docker_cli()
    if docker_cli:
        backends.append(_DockerCommandBackend((docker_cli,), source="docker"))

    if include_wsl and sys.platform == "win32":
        wsl_cli = _find_wsl_cli()
        if wsl_cli:
            backends.extend(
                (
                    _DockerCommandBackend((wsl_cli, "--exec", "docker"), source="wsl-docker"),
                    _DockerCommandBackend((wsl_cli, "-d", "Ubuntu", "-u", "root", "--", "docker"), source="wsl-docker"),
                )
            )

    return tuple(backends)


async def _run_command(*args: str, timeout: float = 8.0) -> _CommandResult:
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        process.kill()
        await process.communicate()
        return _CommandResult(
            returncode=124,
            stdout="",
            stderr="Docker discovery timed out.",
        )
    return _CommandResult(
        returncode=process.returncode or 0,
        stdout=stdout.decode("utf-8", errors="replace"),
        stderr=stderr.decode("utf-8", errors="replace"),
    )


def _string_list(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        text = value.strip()
        return (text,) if text else ()
    if isinstance(value, (list, tuple)):
        items: list[str] = []
        for item in value:
            text = strip_text(item)
            if text:
                items.append(text)
        return tuple(items)
    return ()


def _container_name(container: Mapping[str, Any]) -> str:
    name = strip_text(container.get("Name")).lstrip("/")
    if name:
        return name
    config = as_mapping(container.get("Config"))
    return strip_text(config.get("Hostname")) or "Agent Zero"


def _container_config_labels(container: Mapping[str, Any]) -> Mapping[str, Any]:
    config = as_mapping(container.get("Config"))
    return as_mapping(config.get("Labels"))


def _launcher_instance_name(container: Mapping[str, Any]) -> str:
    config_labels = _container_config_labels(container)
    top_level_labels = as_mapping(container.get("Labels"))
    return strip_text(
        config_labels.get(_LAUNCHER_INSTANCE_NAME_LABEL)
        or top_level_labels.get(_LAUNCHER_INSTANCE_NAME_LABEL)
    )


def _container_image(container: Mapping[str, Any]) -> str:
    config = as_mapping(container.get("Config"))
    container_config = as_mapping(container.get("ContainerConfig"))
    return (
        strip_text(config.get("Image"))
        or strip_text(container_config.get("Image"))
        or strip_text(container.get("Image"))
    )


def _is_running(container: Mapping[str, Any]) -> bool:
    state = as_mapping(container.get("State"))
    return bool(state.get("Running"))


def _display_host(host_ip: str) -> str:
    host = host_ip.strip()
    if host in _LOCAL_BINDING_HOSTS:
        return "localhost"
    if ":" in host and not host.startswith("["):
        return f"[{host}]"
    return host


def _published_http_bindings(container: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    network_settings = as_mapping(container.get("NetworkSettings"))
    ports = as_mapping(network_settings.get("Ports"))
    bindings = ports.get("80/tcp")
    if not isinstance(bindings, list):
        return ()

    urls: list[tuple[str, str]] = []
    for binding in bindings:
        if not isinstance(binding, Mapping):
            continue
        host_port = strip_text(binding.get("HostPort"))
        if not host_port:
            continue
        host = _display_host(strip_text(binding.get("HostIp")))
        urls.append((f"http://{host}:{host_port}", host_port))
    return tuple(urls)


def _command_signal(container: Mapping[str, Any]) -> bool:
    config = as_mapping(container.get("Config"))
    parts: list[str] = []
    parts.extend(_string_list(container.get("Path")))
    parts.extend(_string_list(container.get("Args")))
    parts.extend(_string_list(config.get("Entrypoint")))
    parts.extend(_string_list(config.get("Cmd")))
    command_text = " ".join(parts).lower()
    return any(marker in command_text for marker in _AGENT_ZERO_COMMAND_MARKERS)


def _mount_targets_a0(container: Mapping[str, Any]) -> bool:
    mounts = container.get("Mounts")
    if isinstance(mounts, list):
        for mount in mounts:
            if not isinstance(mount, Mapping):
                continue
            destination = strip_text(mount.get("Destination")).rstrip("/")
            mount_type = strip_text(mount.get("Type")).lower()
            if destination == "/a0" and (not mount_type or mount_type == "bind"):
                return True

    host_config = as_mapping(container.get("HostConfig"))
    for bind in _string_list(host_config.get("Binds")):
        parts = bind.split(":")
        if len(parts) >= 2 and parts[1].rstrip("/") == "/a0":
            return True
    return False


def _image_signal(container: Mapping[str, Any]) -> bool:
    config = as_mapping(container.get("Config"))
    container_config = as_mapping(container.get("ContainerConfig"))
    image_text = " ".join(
        part
        for part in (
            strip_text(config.get("Image")),
            strip_text(container_config.get("Image")),
            strip_text(container.get("Image")),
        )
        if part
    ).lower()
    return "agent-zero" in image_text


def _looks_like_agent_zero(container: Mapping[str, Any]) -> bool:
    return _image_signal(container) or _command_signal(container) or _mount_targets_a0(container)


def _collect_instances(payload: object, *, source: str = "docker") -> tuple[DiscoveredInstance, ...]:
    if not isinstance(payload, list):
        raise ValueError("docker inspect payload must be a list")

    discovered: list[DiscoveredInstance] = []
    seen_urls: set[str] = set()
    for container in payload:
        if not isinstance(container, Mapping):
            raise ValueError("docker inspect container payload must be a mapping")
        if not _is_running(container):
            continue
        bindings = _published_http_bindings(container)
        if not bindings or not _looks_like_agent_zero(container):
            continue

        container_id = strip_text(container.get("Id")) or _container_name(container)
        container_name = _container_name(container)
        friendly_name = _launcher_instance_name(container)
        display_name = friendly_name or container_name
        image_name = _container_image(container)
        status_text = display_name if friendly_name or not image_name or image_name == container_name else f"{container_name} | {image_name}"

        for url, host_port in bindings:
            if url in seen_urls:
                continue
            seen_urls.add(url)
            discovered.append(
                DiscoveredInstance(
                    id=f"{container_id}:{host_port}",
                    name=display_name,
                    url=url,
                    host_port=host_port,
                    source=source,
                    status_text=status_text,
                )
            )

    return tuple(discovered)


def _command_failure_detail(prefix: str, stderr: str) -> str:
    detail = stderr.strip().splitlines()[0] if stderr.strip() else ""
    return f"{prefix} {detail}".strip()


def _docker_host_api_base_url(value: object) -> str:
    text = strip_text(value)
    if not text:
        return ""
    if text.startswith("tcp://"):
        return f"http://{text[6:]}"
    if text.startswith("http://") or text.startswith("https://"):
        return text.rstrip("/")
    return ""


def _docker_host_socket_path(value: object) -> str:
    text = strip_text(value)
    if not text.startswith("unix://"):
        return ""
    return unquote(text[7:]).strip()


def _socket_path_exists(path: str) -> bool:
    try:
        candidate = Path(path).expanduser()
        return candidate.exists()
    except OSError:
        return False


def _docker_context_socket_paths() -> tuple[str, ...]:
    meta_root = Path.home() / _DOCKER_CONTEXTS_META_DIR
    try:
        meta_files = tuple(meta_root.glob("*/meta.json"))
    except OSError:
        return ()

    paths: list[str] = []
    for meta_file in meta_files:
        try:
            payload = json.loads(meta_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        endpoints = as_mapping(as_mapping(payload).get("Endpoints"))
        docker_endpoint = as_mapping(endpoints.get("docker"))
        socket_path = _docker_host_socket_path(docker_endpoint.get("Host"))
        if socket_path:
            paths.append(socket_path)
    return tuple(paths)


def _known_docker_socket_paths() -> tuple[str, ...]:
    if os.name == "nt":
        return ()

    home = Path.home()
    paths: list[str] = [
        str(home / ".docker/run/docker.sock"),
        "/var/run/docker.sock",
    ]

    colima_root = home / ".colima"
    paths.append(str(colima_root / "default/docker.sock"))
    try:
        paths.extend(str(path) for path in colima_root.glob("*/docker.sock"))
    except OSError:
        pass

    return tuple(paths)


def _docker_socket_paths() -> tuple[str, ...]:
    candidates: list[str] = []
    docker_host = _docker_host_socket_path(os.environ.get("DOCKER_HOST"))
    if docker_host:
        candidates.append(docker_host)
    candidates.extend(_docker_context_socket_paths())
    candidates.extend(_known_docker_socket_paths())
    return tuple(
        dict.fromkeys(
            str(Path(path).expanduser())
            for path in candidates
            if path and _socket_path_exists(path)
        )
    )


def _docker_api_base_urls() -> tuple[str, ...]:
    urls: list[str] = []
    docker_host = _docker_host_api_base_url(os.environ.get("DOCKER_HOST"))
    if docker_host:
        urls.append(docker_host)
    if sys.platform == "win32":
        urls.append(_WINDOWS_WSL_ENGINE_API)
    return tuple(dict.fromkeys(url.rstrip("/") for url in urls if url))


async def _discover_with_docker_client(client: httpx.AsyncClient, *, source: str) -> DiscoveryResult:
    listed_response = await client.get("/containers/json", params={"all": "0"})
    listed_response.raise_for_status()
    listed_payload = listed_response.json()
    if not isinstance(listed_payload, list):
        return DiscoveryResult(
            status="error",
            detail="Docker returned unexpected discovery data.",
        )

    container_ids = [
        strip_text(container.get("Id"))
        for container in listed_payload
        if isinstance(container, Mapping) and strip_text(container.get("Id"))
    ]
    if not container_ids:
        return DiscoveryResult(
            status="empty",
            detail="No running Docker containers were found.",
        )

    inspected_payload: list[Mapping[str, Any]] = []
    for container_id in container_ids:
        inspected_response = await client.get(f"/containers/{container_id}/json")
        inspected_response.raise_for_status()
        inspected = inspected_response.json()
        if not isinstance(inspected, Mapping):
            return DiscoveryResult(
                status="error",
                detail="Docker returned unexpected discovery data.",
            )
        inspected_payload.append(inspected)

    try:
        instances = _collect_instances(inspected_payload, source=source)
    except ValueError:
        return DiscoveryResult(
            status="error",
            detail="Docker returned unexpected discovery data.",
        )

    if instances:
        count = len(instances)
        return DiscoveryResult(
            status="ready",
            instances=instances,
            detail=f"Found {count} local Agent Zero endpoint{'s' if count != 1 else ''}.",
        )

    return DiscoveryResult(
        status="empty",
        detail="No running Agent Zero Docker WebUI endpoints were detected.",
    )


async def _discover_with_docker_api(base_url: str) -> DiscoveryResult:
    base = base_url.rstrip("/")
    try:
        async with httpx.AsyncClient(base_url=base, timeout=1.5) as client:
            return await _discover_with_docker_client(client, source="docker-api")
    except (httpx.HTTPError, json.JSONDecodeError, OSError):
        return DiscoveryResult(
            status="unavailable",
            detail="Docker API endpoint is not reachable.",
        )


async def _discover_with_docker_socket(socket_path: str) -> DiscoveryResult:
    try:
        transport = httpx.AsyncHTTPTransport(uds=socket_path)
        async with httpx.AsyncClient(transport=transport, base_url="http://docker", timeout=1.5) as client:
            return await _discover_with_docker_client(client, source="docker-socket")
    except (httpx.HTTPError, json.JSONDecodeError, OSError):
        return DiscoveryResult(
            status="unavailable",
            detail="Docker socket endpoint is not reachable.",
        )


async def _discover_with_command_backend(backend: _DockerCommandBackend) -> DiscoveryResult:
    listed = await _run_command(*backend.args, "ps", "--format", "{{.ID}}")
    if listed.returncode != 0:
        return DiscoveryResult(
            status="unavailable",
            detail=_command_failure_detail("Docker is unavailable.", listed.stderr),
        )

    container_ids = [line.strip() for line in listed.stdout.splitlines() if line.strip()]
    if not container_ids:
        return DiscoveryResult(
            status="empty",
            detail="No running Docker containers were found.",
        )

    inspected = await _run_command(*backend.args, "inspect", *container_ids)
    if inspected.returncode != 0:
        return DiscoveryResult(
            status="unavailable",
            detail=_command_failure_detail("Docker inspection failed.", inspected.stderr),
        )

    try:
        payload = json.loads(inspected.stdout or "[]")
    except json.JSONDecodeError:
        return DiscoveryResult(
            status="error",
            detail="Docker returned invalid discovery data.",
        )

    try:
        instances = _collect_instances(payload, source=backend.source)
    except ValueError:
        return DiscoveryResult(
            status="error",
            detail="Docker returned unexpected discovery data.",
        )

    if instances:
        count = len(instances)
        return DiscoveryResult(
            status="ready",
            instances=instances,
            detail=f"Found {count} local Agent Zero endpoint{'s' if count != 1 else ''}.",
        )

    return DiscoveryResult(
        status="empty",
        detail="No running Agent Zero Docker WebUI endpoints were detected.",
    )


async def discover_local_instances() -> DiscoveryResult:
    unavailable_details: list[str] = []
    empty_result: DiscoveryResult | None = None
    error_result: DiscoveryResult | None = None

    command_backends = _docker_command_backends(include_wsl=False)
    for backend in command_backends:
        result = await _discover_with_command_backend(backend)
        if result.status == "ready":
            return result
        if result.status == "empty" and empty_result is None:
            empty_result = result
        if result.status == "error" and error_result is None:
            error_result = result
        if result.detail:
            unavailable_details.append(result.detail)

    for socket_path in _docker_socket_paths():
        result = await _discover_with_docker_socket(socket_path)
        if result.status == "ready":
            return result
        if result.status == "empty" and empty_result is None:
            empty_result = result
        if result.status == "error" and error_result is None:
            error_result = result
        if result.detail:
            unavailable_details.append(result.detail)

    for base_url in _docker_api_base_urls():
        result = await _discover_with_docker_api(base_url)
        if result.status == "ready":
            return result
        if result.status == "empty" and empty_result is None:
            empty_result = result
        if result.status == "error" and error_result is None:
            error_result = result
        if result.detail:
            unavailable_details.append(result.detail)

    wsl_backends = tuple(
        backend for backend in _docker_command_backends(include_wsl=True)
        if backend.source == "wsl-docker"
    )
    for backend in wsl_backends:
        result = await _discover_with_command_backend(backend)
        if result.status == "ready":
            return result
        if result.status == "empty" and empty_result is None:
            empty_result = result
        if result.status == "error" and error_result is None:
            error_result = result
        if result.detail:
            unavailable_details.append(result.detail)

    if empty_result is not None:
        return empty_result
    if error_result is not None:
        return error_result

    detail = "No local Docker runtime responded. Enter a URL manually."
    if unavailable_details:
        detail = unavailable_details[-1]
        if not command_backends and not wsl_backends and _docker_api_base_urls():
            detail = "No local Docker endpoint responded. Enter a URL manually."
    return DiscoveryResult(status="unavailable", detail=detail)


__all__ = [
    "DiscoveredInstance",
    "DiscoveryResult",
    "DiscoveryStatus",
    "discover_local_instances",
]
