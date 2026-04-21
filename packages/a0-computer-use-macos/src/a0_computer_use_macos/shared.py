from __future__ import annotations

from dataclasses import dataclass
import uuid
from typing import Any

MACOS_BACKEND_ID = "macos"
MACOS_BACKEND_FAMILY = "macos"
MACOS_BACKEND_PRIORITY = 100
MACOS_BACKEND_FEATURES = (
    "inline-png-capture",
    "screencapture-screen-capture",
    "quartz-input-events",
    "accessibility-trust",
    "session-reuse-metadata",
)
MACOS_TRUST_MODES = ("interactive", "persistent", "free_run")
STATE_DIR_ENV = "A0_COMPUTER_USE_MACOS_STATE_DIR"
CAPTURE_DEBUG_DIR_ENV = "A0_COMPUTER_USE_MACOS_CAPTURE_DIR"


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
    if normalized_mode == "free_run":
        return TrustModePolicy(
            trust_mode="free_run",
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
