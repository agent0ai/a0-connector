from __future__ import annotations

import re
from typing import Any, Mapping

from agent_zero_cli import __version__
from agent_zero_cli.client import PROTOCOL_VERSION, WS_HANDLER, WS_NAMESPACE

_VERSION_PATTERN = re.compile(r"v?(?P<version>\d+(?:\.\d+)*)", re.IGNORECASE)


def validate_capabilities(
    capabilities: dict[str, Any],
    protocol_version: str = PROTOCOL_VERSION,
    ws_namespace: str = WS_NAMESPACE,
    ws_handler: str = WS_HANDLER,
) -> None:
    protocol = capabilities.get("protocol")
    namespace = capabilities.get("websocket_namespace")
    handlers = capabilities.get("websocket_handlers") or []
    auth_modes = capabilities.get("auth")
    auth_required = capabilities.get("auth_required")
    features = capabilities.get("features") or []

    if protocol != protocol_version:
        raise ValueError(f"Unsupported connector protocol: expected {protocol_version}, got {protocol!r}")
    if namespace != ws_namespace:
        raise ValueError(f"Unsupported WebSocket namespace: expected {ws_namespace}, got {namespace!r}")
    if not isinstance(handlers, list) or ws_handler not in handlers:
        raise ValueError(f"Connector handler activation is missing {ws_handler!r} in capabilities")
    if auth_modes != ["session"]:
        raise ValueError(f"Unsupported connector auth contract: expected ['session'], got {auth_modes!r}")
    if not isinstance(auth_required, bool):
        raise ValueError("Connector capabilities must include boolean auth_required")
    if not isinstance(features, list):
        raise ValueError("Connector capabilities features payload is invalid")
    if "connector_login" in features:
        raise ValueError("Connector capabilities still advertise the removed connector_login feature")


def context_identifier(context: Mapping[str, Any]) -> str:
    return str(context.get("id") or context.get("context_id") or context.get("ctxid") or "").strip()


def context_has_messages(context: Mapping[str, Any]) -> bool:
    return bool(context.get("last_message") or context.get("log_entries"))


def context_display_name(context: Mapping[str, Any]) -> str:
    """Prefer the chat name, then the indexed label, then the raw id."""
    name = str(context.get("name") or "").strip()
    if name:
        return name
    try:
        number = int(context.get("no", 0) or 0)
    except (TypeError, ValueError):
        number = 0
    if number > 0:
        return f"Chat #{number}"
    return context_identifier(context)


def _numeric_version(value: object) -> tuple[int, ...] | None:
    match = _VERSION_PATTERN.search(str(value or "").strip())
    if match is None:
        return None
    return tuple(int(part) for part in match.group("version").split("."))


def connector_version_warning(
    capabilities: dict[str, Any],
    *,
    client_version: str = __version__,
) -> str:
    server_label = str(
        capabilities.get("agent_zero_version")
        or capabilities.get("server_version")
        or ""
    ).strip()
    server_version = _numeric_version(server_label)
    installed_version = _numeric_version(client_version)
    if server_version is None or installed_version is None:
        return ""
    if server_version <= installed_version:
        return ""
    return (
        f"Agent Zero {server_label} is newer than a0 CLI {client_version}. "
        "Run `a0 update` after exiting; connector features may misbehave until "
        "the CLI is updated."
    )
