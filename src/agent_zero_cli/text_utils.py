"""Small shared helpers for payload coercion and display text."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
import re

_WHITESPACE_RE = re.compile(r"\s+")
_ID_SAFE_RE = re.compile(r"[^A-Za-z0-9_-]+")
_TRUE_WORDS = frozenset({"1", "true", "yes", "on", "enabled"})
_FALSE_WORDS = frozenset({"0", "false", "no", "off", "disabled", ""})


def coerce_bool(value: object, default: bool = False) -> bool:
    """Coerce a connector payload flag, accepting the shared truthy words."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    normalized = str(value).strip().lower()
    if normalized in _TRUE_WORDS:
        return True
    if normalized in _FALSE_WORDS:
        return False
    return default


def strip_text(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def parse_timestamp(value: object) -> float | None:
    """Parse connector timestamps: epoch seconds, numeric strings, or ISO 8601."""
    if isinstance(value, (int, float)):
        return float(value)
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.timestamp()
    return parsed.timestamp()


def collapse_whitespace(value: object) -> str:
    return _WHITESPACE_RE.sub(" ", str(value or "")).strip()


def clip_text(value: str, limit: int) -> str:
    return value if len(value) <= limit else f"{value[: limit - 1].rstrip()}..."


def safe_id_fragment(value: str) -> str:
    return _ID_SAFE_RE.sub("-", value).strip("-")


def as_mapping(value: object) -> Mapping[str, object]:
    """Return a payload mapping as-is, or an empty mapping fallback."""
    return value if isinstance(value, Mapping) else {}
