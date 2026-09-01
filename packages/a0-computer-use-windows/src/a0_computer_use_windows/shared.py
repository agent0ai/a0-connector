from __future__ import annotations

from dataclasses import dataclass
import uuid
from typing import Any

WINDOWS_BACKEND_ID = "windows"
WINDOWS_BACKEND_FAMILY = "windows"
WINDOWS_BACKEND_PRIORITY = 100
WINDOWS_BACKEND_FEATURES = (
    "inline-png-capture",
    "dxcam-screen-capture",
    "normalized-screen-coordinates",
    "virtual-screen-coordinates",
    "multi-monitor-virtual-screen",
    "global-pixel-actions",
    "uia-automation",
    "uia-tree-snapshot",
    "uia-structural-targeting",
    "uia-element-action",
    "uia-window-management",
    "native-window-list",
    "window-state",
    "element-index-targeting",
    "background-dispatch",
    "foreground-dispatch-fallback",
    "mouse-injection",
    "keyboard-injection",
    "real-cursor-may-move",
    "session-reuse-metadata",
    "a0-tag",
)
WINDOWS_TRUST_MODES = ("interactive", "persistent", "allow")
STATE_DIR_ENV = "A0_COMPUTER_USE_WINDOWS_STATE_DIR"
CAPTURE_DEBUG_DIR_ENV = "A0_COMPUTER_USE_WINDOWS_CAPTURE_DIR"


@dataclass(frozen=True)
class TrustModePolicy:
    trust_mode: str
    reuse_allowed: bool
    silent_reuse: bool
    persist_metadata: bool


def normalize_context_id(value: object) -> str:
    context_id = str(value or "").strip()
    if context_id:
        return context_id
    return "default"


def safe_context_segment(value: str) -> str:
    cleaned: list[str] = []
    for char in value:
        if char.isalnum() or char in {"-", "_", "."}:
            cleaned.append(char)
        else:
            cleaned.append("_")
    return "".join(cleaned) or "default"


def clamp_unit_interval(value: object, *, name: str) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number in [0, 1]") from exc
    return min(max(numeric, 0.0), 1.0)


def coerce_int(value: object, *, name: str, default: int | None = None) -> int:
    if value is None and default is not None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc


def coerce_bool(value: object, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off", ""}:
        return False
    return default


def normalize_restore_token(value: object) -> str:
    token = str(value or "").strip()
    if not token:
        return ""
    try:
        return str(uuid.UUID(token))
    except (ValueError, AttributeError, TypeError):
        return ""


def resolve_trust_mode_policy(trust_mode: str, restore_token: str) -> TrustModePolicy:
    normalized_mode = str(trust_mode or "").strip().lower()
    has_restore_token = bool(normalize_restore_token(restore_token))
    if normalized_mode == "interactive":
        return TrustModePolicy(
            trust_mode="interactive",
            reuse_allowed=False,
            silent_reuse=False,
            persist_metadata=False,
        )
    if normalized_mode == "allow":
        return TrustModePolicy(
            trust_mode="allow",
            reuse_allowed=has_restore_token,
            silent_reuse=has_restore_token,
            persist_metadata=True,
        )
    return TrustModePolicy(
        trust_mode="persistent",
        reuse_allowed=has_restore_token,
        silent_reuse=False,
        persist_metadata=True,
    )


def normalize_action_payload(
    action: str,
    payload: dict[str, Any],
    *,
    context_id: str,
) -> dict[str, Any]:
    normalized_action = str(action or "").strip().lower()
    request: dict[str, Any] = {
        "action": normalized_action,
        "context_id": context_id,
    }
    session_id = str(payload.get("session_id", "")).strip()
    if session_id:
        request["session_id"] = session_id

    if normalized_action == "capture":
        return request

    if normalized_action == "list_windows":
        if payload.get("include_hidden") is not None:
            request["include_hidden"] = coerce_bool(payload.get("include_hidden"))
        if payload.get("include_offscreen") is not None:
            request["include_offscreen"] = coerce_bool(payload.get("include_offscreen"))
        if payload.get("max_windows") is not None:
            request["max_windows"] = coerce_int(payload.get("max_windows"), name="max_windows")
        return request

    if normalized_action == "get_window_state":
        if payload.get("pid") is not None:
            request["pid"] = coerce_int(payload.get("pid"), name="pid")
        for key in ("window_id", "mode"):
            if payload.get(key) is not None:
                request[key] = str(payload.get(key) or "").strip()
        if payload.get("max_depth") is not None:
            request["max_depth"] = coerce_int(payload.get("max_depth"), name="max_depth")
        if payload.get("max_nodes") is not None:
            request["max_nodes"] = coerce_int(payload.get("max_nodes"), name="max_nodes")
        return request

    if normalized_action == "element_action":
        if payload.get("pid") is not None:
            request["pid"] = coerce_int(payload.get("pid"), name="pid")
        if payload.get("window_id") is not None:
            request["window_id"] = str(payload.get("window_id") or "").strip()
        if payload.get("element_index") is not None:
            request["element_index"] = coerce_int(payload.get("element_index"), name="element_index")
        target = payload.get("target")
        normalized_target: dict[str, Any] = {}
        if isinstance(target, dict):
            normalized_target.update(target)
        if payload.get("selector") is not None:
            normalized_target["selector"] = str(payload.get("selector") or "").strip()
        if normalized_target:
            request["target"] = normalized_target
        if payload.get("path") is not None:
            request["path"] = payload.get("path")
        operation = payload.get("operation", payload.get("name"))
        if operation is not None:
            request["operation"] = str(operation or "").strip()
        request["dispatch"] = str(payload.get("dispatch") or "background").strip().lower()
        if payload.get("value") is not None:
            request["value"] = payload.get("value")
        if payload.get("text") is not None:
            request["text"] = str(payload.get("text") or "")
        if coerce_bool(payload.get("submit")):
            request["submit"] = True
        return request

    if normalized_action == "uia_snapshot":
        if payload.get("max_depth") is not None:
            request["max_depth"] = coerce_int(payload.get("max_depth"), name="max_depth")
        if payload.get("max_nodes") is not None:
            request["max_nodes"] = coerce_int(payload.get("max_nodes"), name="max_nodes")
        return request

    if normalized_action == "uia_action":
        target = payload.get("target")
        normalized_target: dict[str, Any] = {}
        if isinstance(target, dict):
            normalized_target.update(target)
        if payload.get("selector") is not None:
            normalized_target["selector"] = str(payload.get("selector") or "").strip()
        if normalized_target:
            request["target"] = normalized_target
        if payload.get("path") is not None:
            request["path"] = payload.get("path")
        operation = payload.get("operation", payload.get("uia_action", payload.get("name")))
        if operation is not None:
            request["operation"] = str(operation or "").strip()
        if payload.get("value") is not None:
            request["value"] = payload.get("value")
        if payload.get("text") is not None:
            request["text"] = str(payload.get("text") or "")
        if coerce_bool(payload.get("submit")):
            request["submit"] = True
        return request

    if normalized_action == "move":
        request["x"] = clamp_unit_interval(payload.get("x"), name="x")
        request["y"] = clamp_unit_interval(payload.get("y"), name="y")
        return request

    if normalized_action == "click":
        if payload.get("x") is not None:
            request["x"] = clamp_unit_interval(payload.get("x"), name="x")
        if payload.get("y") is not None:
            request["y"] = clamp_unit_interval(payload.get("y"), name="y")
        request["button"] = str(payload.get("button", "left") or "left").strip().lower()
        request["count"] = coerce_int(payload.get("count"), name="count", default=1)
        if request["count"] < 1:
            raise ValueError("count must be >= 1")
        return request

    if normalized_action == "scroll":
        delta_x = payload.get("dx", payload.get("delta_x", payload.get("steps_x", 0)))
        delta_y = payload.get("dy", payload.get("delta_y", payload.get("steps_y", 0)))
        request["dx"] = coerce_int(delta_x, name="dx", default=0)
        request["dy"] = coerce_int(delta_y, name="dy", default=0)
        if request["dx"] == 0 and request["dy"] == 0:
            raise ValueError("scroll requires dx or dy")
        return request

    if normalized_action == "key":
        keys_value = payload.get("keys")
        if isinstance(keys_value, (list, tuple)):
            keys = [str(item).strip() for item in keys_value if str(item).strip()]
        else:
            raw = str(payload.get("key", keys_value or "")).strip()
            keys = [part.strip() for part in raw.split("+") if part.strip()]
        if not keys:
            raise ValueError("key requires key or keys")
        request["keys"] = keys
        return request

    if normalized_action == "type":
        text = str(payload.get("text", "") or "")
        if not text:
            raise ValueError("type requires text")
        request["text"] = text
        if coerce_bool(payload.get("submit")):
            request["submit"] = True
        return request

    return request
