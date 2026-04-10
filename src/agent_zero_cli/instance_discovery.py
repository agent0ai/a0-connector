from __future__ import annotations

import asyncio
import json
import shutil
from dataclasses import dataclass
from typing import Any, Literal, Mapping, TypeAlias


DiscoveryStatus: TypeAlias = Literal["loading", "ready", "empty", "unavailable", "error"]

_AGENT_ZERO_COMMAND_MARKERS = ("/exe/initialize.sh", "run_ui.py")
_LOCAL_BINDING_HOSTS = {"", "0.0.0.0", "::", "[::]"}


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


def _find_docker_cli() -> str | None:
    return shutil.which("docker")


async def _run_command(*args: str) -> _CommandResult:
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    return _CommandResult(
        returncode=process.returncode or 0,
        stdout=stdout.decode("utf-8", errors="replace"),
        stderr=stderr.decode("utf-8", errors="replace"),
    )


def _stringify(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _mapping(value: object) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    return {}


def _string_list(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        text = value.strip()
        return (text,) if text else ()
    if isinstance(value, (list, tuple)):
        items: list[str] = []
        for item in value:
            text = _stringify(item)
            if text:
                items.append(text)
        return tuple(items)
    return ()


def _container_name(container: Mapping[str, Any]) -> str:
    name = _stringify(container.get("Name")).lstrip("/")
    if name:
        return name
    config = _mapping(container.get("Config"))
    return _stringify(config.get("Hostname")) or "Agent Zero"


def _container_image(container: Mapping[str, Any]) -> str:
    config = _mapping(container.get("Config"))
    container_config = _mapping(container.get("ContainerConfig"))
    return (
        _stringify(config.get("Image"))
        or _stringify(container_config.get("Image"))
        or _stringify(container.get("Image"))
    )


def _is_running(container: Mapping[str, Any]) -> bool:
    state = _mapping(container.get("State"))
    return bool(state.get("Running"))


def _display_host(host_ip: str) -> str:
    host = host_ip.strip()
    if host in _LOCAL_BINDING_HOSTS:
        return "localhost"
    if ":" in host and not host.startswith("["):
        return f"[{host}]"
    return host


def _published_http_bindings(container: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    network_settings = _mapping(container.get("NetworkSettings"))
    ports = _mapping(network_settings.get("Ports"))
    bindings = ports.get("80/tcp")
    if not isinstance(bindings, list):
        return ()

    urls: list[tuple[str, str]] = []
    for binding in bindings:
        if not isinstance(binding, Mapping):
            continue
        host_port = _stringify(binding.get("HostPort"))
        if not host_port:
            continue
        host = _display_host(_stringify(binding.get("HostIp")))
        urls.append((f"http://{host}:{host_port}", host_port))
    return tuple(urls)


def _command_signal(container: Mapping[str, Any]) -> bool:
    config = _mapping(container.get("Config"))
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
            destination = _stringify(mount.get("Destination")).rstrip("/")
            mount_type = _stringify(mount.get("Type")).lower()
            if destination == "/a0" and (not mount_type or mount_type == "bind"):
                return True

    host_config = _mapping(container.get("HostConfig"))
    for bind in _string_list(host_config.get("Binds")):
        parts = bind.split(":")
        if len(parts) >= 2 and parts[1].rstrip("/") == "/a0":
            return True
    return False


def _image_signal(container: Mapping[str, Any]) -> bool:
    config = _mapping(container.get("Config"))
    container_config = _mapping(container.get("ContainerConfig"))
    image_text = " ".join(
        part
        for part in (
            _stringify(config.get("Image")),
            _stringify(container_config.get("Image")),
            _stringify(container.get("Image")),
        )
        if part
    ).lower()
    return "agent-zero" in image_text


def _looks_like_agent_zero(container: Mapping[str, Any]) -> bool:
    return _image_signal(container) or _command_signal(container) or _mount_targets_a0(container)


def _collect_instances(payload: object) -> tuple[DiscoveredInstance, ...]:
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

        container_id = _stringify(container.get("Id")) or _container_name(container)
        container_name = _container_name(container)
        image_name = _container_image(container)
        status_text = container_name if not image_name or image_name == container_name else f"{container_name} | {image_name}"

        for url, host_port in bindings:
            if url in seen_urls:
                continue
            seen_urls.add(url)
            discovered.append(
                DiscoveredInstance(
                    id=f"{container_id}:{host_port}",
                    name=container_name,
                    url=url,
                    host_port=host_port,
                    source="docker",
                    status_text=status_text,
                )
            )

    return tuple(discovered)


def _command_failure_detail(prefix: str, stderr: str) -> str:
    detail = stderr.strip().splitlines()[0] if stderr.strip() else ""
    return f"{prefix} {detail}".strip()


async def discover_local_instances() -> DiscoveryResult:
    docker_cli = _find_docker_cli()
    if not docker_cli:
        return DiscoveryResult(
            status="unavailable",
            detail="Docker CLI was not found. Enter a URL manually.",
        )

    listed = await _run_command(docker_cli, "ps", "--format", "{{.ID}}")
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

    inspected = await _run_command(docker_cli, "inspect", *container_ids)
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
        instances = _collect_instances(payload)
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


__all__ = [
    "DiscoveredInstance",
    "DiscoveryResult",
    "DiscoveryStatus",
    "discover_local_instances",
]
