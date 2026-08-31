from __future__ import annotations

import argparse
import base64
import io
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    package_dir = Path(__file__).resolve().parent
    parent_dir = package_dir.parent
    if str(parent_dir) not in sys.path:
        sys.path.insert(0, str(parent_dir))

import dbus
from dbus.mainloop.glib import DBusGMainLoop
import gi
from PIL import Image

gi.require_version("Gst", "1.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gst  # noqa: E402

from a0_computer_use_wayland.backend import WAYLAND_BACKEND_SPEC

PORTAL_SERVICE = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
PORTAL_REQUEST_IFACE = "org.freedesktop.portal.Request"
PORTAL_SESSION_IFACE = "org.freedesktop.portal.Session"
PORTAL_REMOTE_DESKTOP_IFACE = "org.freedesktop.portal.RemoteDesktop"
PORTAL_SCREENCAST_IFACE = "org.freedesktop.portal.ScreenCast"
DEVICE_TYPE_KEYBOARD = 1
DEVICE_TYPE_POINTER = 2
SOURCE_TYPE_MONITOR = 1
CURSOR_MODE_EMBEDDED = 2
PERSIST_MODE_NONE = 0
PERSIST_MODE_EXPLICIT = 2
BTN_LEFT = 272
BTN_RIGHT = 273
BTN_MIDDLE = 274
_KEY_ALIASES = {
    "alt": "Alt_L",
    "backspace": "BackSpace",
    "cmd": "Super_L",
    "command": "Super_L",
    "control": "Control_L",
    "ctrl": "Control_L",
    "delete": "Delete",
    "down": "Down",
    "enter": "Return",
    "esc": "Escape",
    "escape": "Escape",
    "left": "Left",
    "pagedown": "Page_Down",
    "pageup": "Page_Up",
    "pgdn": "Page_Down",
    "pgup": "Page_Up",
    "right": "Right",
    "shift": "Shift_L",
    "space": "space",
    "super": "Super_L",
    "win": "Super_L",
    "windows": "Super_L",
    "tab": "Tab",
    "up": "Up",
}
_EVDEV_KEY_ALIASES = {
    "alt": 56,
    "backspace": 14,
    "cmd": 125,
    "command": 125,
    "control": 29,
    "ctrl": 29,
    "delete": 111,
    "del": 111,
    "down": 108,
    "end": 107,
    "enter": 28,
    "esc": 1,
    "escape": 1,
    "home": 102,
    "insert": 110,
    "ins": 110,
    "left": 105,
    "leftalt": 56,
    "leftctrl": 29,
    "leftmeta": 125,
    "leftshift": 42,
    "leftsuper": 125,
    "menu": 139,
    "meta": 125,
    "pagedown": 109,
    "pageup": 104,
    "pgdn": 109,
    "pgup": 104,
    "print": 99,
    "printscreen": 99,
    "prtsc": 99,
    "right": 106,
    "rightalt": 100,
    "rightctrl": 97,
    "rightmeta": 126,
    "rightshift": 54,
    "rightsuper": 126,
    "shift": 42,
    "space": 57,
    "super": 125,
    "tab": 15,
    "up": 103,
    "win": 125,
    "windows": 125,
}


def _backend_contract_metadata() -> dict[str, Any]:
    return {
        "backend_id": WAYLAND_BACKEND_SPEC.backend_id,
        "backend_family": WAYLAND_BACKEND_SPEC.backend_family,
        "features": list(WAYLAND_BACKEND_SPEC.features),
        "contract_version": WAYLAND_BACKEND_SPEC.capabilities()["contract_version"],
        "capabilities": WAYLAND_BACKEND_SPEC.capabilities(),
    }


_EVDEV_CHAR_KEYCODES = {
    "1": 2,
    "2": 3,
    "3": 4,
    "4": 5,
    "5": 6,
    "6": 7,
    "7": 8,
    "8": 9,
    "9": 10,
    "0": 11,
    "-": 12,
    "_": 12,
    "=": 13,
    "+": 13,
    "q": 16,
    "w": 17,
    "e": 18,
    "r": 19,
    "t": 20,
    "y": 21,
    "u": 22,
    "i": 23,
    "o": 24,
    "p": 25,
    "[": 26,
    "{": 26,
    "]": 27,
    "}": 27,
    "a": 30,
    "s": 31,
    "d": 32,
    "f": 33,
    "g": 34,
    "h": 35,
    "j": 36,
    "k": 37,
    "l": 38,
    ";": 39,
    ":": 39,
    "'": 40,
    '"': 40,
    "`": 41,
    "~": 41,
    "\\": 43,
    "|": 43,
    "z": 44,
    "x": 45,
    "c": 46,
    "v": 47,
    "b": 48,
    "n": 49,
    "m": 50,
    ",": 51,
    "<": 51,
    ".": 52,
    ">": 52,
    "/": 53,
    "?": 53,
    " ": 57,
}
_EVDEV_FUNCTION_KEYCODES = {
    **{f"f{number}": 58 + number for number in range(1, 11)},
    "f11": 87,
    "f12": 88,
    **{f"f{number}": 170 + number for number in range(13, 25)},
}
_AX_DEFAULT_MAX_DEPTH = 5
_AX_HARD_MAX_DEPTH = 12
_AX_DEFAULT_MAX_NODES = 120
_AX_HARD_MAX_NODES = 500
_AX_TARGET_SEARCH_MAX_NODES = 800
_AX_TEXT_MAX_CHARS = 240
_TAG_TEXT_WINDOW_CHARS = 4096
_TAG_QUERY_MAX_CHARS = 2048
_TAG_REPLACEMENT_MAX_CHARS = 16384
_TAG_TARGET_TTL_SECONDS = 15 * 60
_TAG_PROFILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_AX_SENTINEL_COORDINATE = -2147483648
_AT_SPI_STATE_NAMES = (
    ("ACTIVE", "active"),
    ("CHECKED", "checked"),
    ("EDITABLE", "editable"),
    ("ENABLED", "enabled"),
    ("EXPANDED", "expanded"),
    ("FOCUSED", "focused"),
    ("FOCUSABLE", "focusable"),
    ("PRESSED", "pressed"),
    ("PROTECTED", "protected"),
    ("SELECTED", "selected"),
    ("SHOWING", "showing"),
    ("VISIBLE", "visible"),
)
_AT_SPI_PRESS_ACTION_NAMES = (
    "press",
    "click",
    "activate",
    "default",
    "toggle",
    "open",
    "invoke",
)
_AT_SPI_WINDOW_ROLES = {"alert", "dialog", "file chooser", "frame", "window"}
_AT_SPI_WINDOW_ACTIVATION_ROLES = {"application", *_AT_SPI_WINDOW_ROLES}
_AT_SPI_FOCUS_SEARCH_MAX_NODES = 500

_DBUS_NATIVE_TYPES = (
    dbus.Boolean,
    dbus.Byte,
    dbus.Int16,
    dbus.Int32,
    dbus.Int64,
    dbus.UInt16,
    dbus.UInt32,
    dbus.UInt64,
    dbus.Double,
    dbus.String,
    dbus.ObjectPath,
    dbus.Signature,
    dbus.Array,
    dbus.Dictionary,
    dbus.Struct,
)


class PortalError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class PortalSession:
    context_id: str
    trust_mode: str
    session_id: str
    session_handle: str
    stream_id: int
    width: int
    height: int
    devices: int
    restore_token: str
    capture_stream: "CaptureStream"


@dataclass
class TagTarget:
    token: str
    path: list[int]
    window_path: list[int]
    window_id: str
    app_name: str
    window_title: str
    start: int
    end: int
    original: str
    editable: bool
    captured_at: float


def _dbus_dict(payload: dict[str, Any]) -> dbus.Dictionary:
    converted = {key: _dbus_value(value) for key, value in payload.items()}
    return dbus.Dictionary(converted, signature="sv")


def _dbus_value(value: Any) -> Any:
    if isinstance(value, _DBUS_NATIVE_TYPES):
        return value
    if isinstance(value, bool):
        return dbus.Boolean(value)
    if isinstance(value, int):
        return dbus.Int32(value)
    if isinstance(value, float):
        return dbus.Double(value)
    if isinstance(value, str):
        return dbus.String(value)
    if isinstance(value, (list, tuple)):
        return dbus.Array([_dbus_value(item) for item in value], signature="v")
    if isinstance(value, dict):
        return _dbus_dict(value)
    return value


def _dbus_u32(value: int) -> dbus.UInt32:
    return dbus.UInt32(int(value))


def _python_value(value: Any) -> Any:
    if isinstance(value, dbus.Boolean):
        return bool(value)
    if isinstance(
        value,
        (
            dbus.Int16,
            dbus.Int32,
            dbus.Int64,
            dbus.UInt16,
            dbus.UInt32,
            dbus.UInt64,
        ),
    ):
        return int(value)
    if isinstance(value, dbus.Double):
        return float(value)
    if isinstance(value, (dbus.String, dbus.ObjectPath, dbus.Signature)):
        return str(value)
    if isinstance(value, dbus.Array):
        return [_python_value(item) for item in value]
    if isinstance(value, dbus.Struct):
        return tuple(_python_value(item) for item in value)
    if isinstance(value, dbus.Dictionary):
        return {str(key): _python_value(item) for key, item in value.items()}
    return value


def _float_param(value: object, *, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int_param(value: object, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return min(max(parsed, minimum), maximum)


def _bool_param(value: object, *, default: bool = False) -> bool:
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


def _normalize_dispatch(value: object, *, default: str = "background") -> str:
    dispatch = str(value or default).strip().lower()
    if not dispatch:
        return default
    if dispatch not in {"background", "foreground", "auto"}:
        raise PortalError(
            "COMPUTER_USE_BAD_DISPATCH",
            "element_action dispatch must be background, foreground, or auto.",
        )
    return dispatch


def _load_atspi_module() -> Any:
    try:
        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi
    except Exception as exc:
        raise PortalError(
            "COMPUTER_USE_AX_UNAVAILABLE",
            f"AT-SPI accessibility is unavailable: {exc}",
        ) from exc
    return Atspi


def _safe_call(obj: object, method_name: str, *args: object) -> Any:
    method = getattr(obj, method_name, None)
    if not callable(method):
        return None
    try:
        return method(*args)
    except Exception:
        return None


def _safe_int(value: object, *, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _clean_text(value: object, *, limit: int = _AX_TEXT_MAX_CHARS) -> str:
    text = str(value or "").replace("\x00", "").strip()
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "..."


def _frame_value(extents: object, field: str, index: int) -> int:
    if hasattr(extents, field):
        return _safe_int(getattr(extents, field), default=_AX_SENTINEL_COORDINATE)
    if isinstance(extents, (list, tuple)) and len(extents) > index:
        return _safe_int(extents[index], default=_AX_SENTINEL_COORDINATE)
    return _AX_SENTINEL_COORDINATE


def _parse_ax_path(value: object) -> list[int] | None:
    if value is None:
        return None
    raw: object = value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError:
            decoded = None
        raw = (
            decoded
            if isinstance(decoded, (list, tuple))
            else [part for part in text.replace("/", ".").split(".") if part.strip()]
        )
    if not isinstance(raw, (list, tuple)):
        return None
    path: list[int] = []
    for item in raw:
        try:
            index = int(item)
        except (TypeError, ValueError):
            return None
        if index < 0:
            return None
        path.append(index)
    return path


def _normalized_match_text(value: object) -> str:
    return _clean_text(value, limit=1000).casefold()


def _list_contains_action(actions: object, candidates: tuple[str, ...]) -> bool:
    if not isinstance(actions, list):
        return False
    names = {str(item.get("name") or "").strip().casefold() for item in actions if isinstance(item, dict)}
    return any(candidate in names for candidate in candidates)


def _atspi_desktop(Atspi: Any) -> object:
    desktop = _safe_call(Atspi, "get_desktop", 0)
    if desktop is None:
        raise PortalError("COMPUTER_USE_AX_UNAVAILABLE", "AT-SPI did not return a desktop root.")
    return desktop


def _atspi_child_count(element: object) -> int:
    count = _safe_call(element, "get_child_count")
    return max(0, _safe_int(count, default=0))


def _atspi_child_at(element: object, index: int) -> object | None:
    return _safe_call(element, "get_child_at_index", index)


def _atspi_role(element: object) -> str:
    role = _clean_text(_safe_call(element, "get_role_name"), limit=80)
    if role:
        return role
    role_obj = _safe_call(element, "get_role")
    return _clean_text(role_obj, limit=80) or "element"


def _atspi_name(element: object) -> str:
    return _clean_text(_safe_call(element, "get_name"), limit=160)


def _atspi_description(element: object) -> str:
    return _clean_text(_safe_call(element, "get_description"), limit=240)


def _atspi_pid(element: object) -> int | None:
    pid = _safe_call(element, "get_process_id")
    if pid is None:
        return None
    return _safe_int(pid, default=0) or None


def _atspi_frame(Atspi: Any, element: object, session: PortalSession) -> dict[str, Any] | None:
    coord_type = getattr(getattr(Atspi, "CoordType", object()), "SCREEN", None)
    extents = _safe_call(element, "get_extents", coord_type)
    if extents is None:
        return None
    x = _frame_value(extents, "x", 0)
    y = _frame_value(extents, "y", 1)
    width = _frame_value(extents, "width", 2)
    height = _frame_value(extents, "height", 3)
    if x <= _AX_SENTINEL_COORDINATE or y <= _AX_SENTINEL_COORDINATE or width <= 0 or height <= 0:
        return None
    frame: dict[str, Any] = {
        "x": x,
        "y": y,
        "width": width,
        "height": height,
    }
    if session.width > 0 and session.height > 0:
        frame.update(
            {
                "normalized_x": x / session.width,
                "normalized_y": y / session.height,
                "normalized_width": width / session.width,
                "normalized_height": height / session.height,
            }
        )
    return frame


def _atspi_states(Atspi: Any, element: object) -> list[str]:
    state_set = _safe_call(element, "get_state_set")
    contains = getattr(state_set, "contains", None)
    state_type = getattr(Atspi, "StateType", None)
    if not callable(contains) or state_type is None:
        return []
    states: list[str] = []
    for attr_name, display_name in _AT_SPI_STATE_NAMES:
        value = getattr(state_type, attr_name, None)
        if value is None:
            continue
        try:
            if contains(value):
                states.append(display_name)
        except Exception:
            continue
    return states


def _atspi_actions(element: object) -> list[dict[str, str]]:
    count = max(0, _safe_int(_safe_call(element, "get_n_actions"), default=0))
    actions: list[dict[str, str]] = []
    for index in range(min(count, 16)):
        name = _clean_text(_safe_call(element, "get_action_name", index), limit=80)
        if not name:
            continue
        action: dict[str, str] = {"name": name}
        description = _clean_text(_safe_call(element, "get_action_description", index), limit=160)
        key_binding = _clean_text(_safe_call(element, "get_key_binding", index), limit=80)
        if description:
            action["description"] = description
        if key_binding:
            action["key_binding"] = key_binding
        actions.append(action)
    return actions


def _atspi_text(element: object) -> str:
    character_count = _safe_call(element, "get_character_count")
    count = _safe_int(character_count, default=0)
    if count <= 0:
        return ""
    text = _safe_call(element, "get_text", 0, min(count, _AX_TEXT_MAX_CHARS + 1))
    return _clean_text(text)


def _atspi_value(element: object) -> object | None:
    value = _safe_call(element, "get_current_value")
    if value is None:
        return None
    if isinstance(value, float):
        return round(value, 4)
    return value


def _atspi_node_metadata(
    Atspi: Any,
    element: object,
    *,
    path: list[int],
    session: PortalSession,
) -> dict[str, Any]:
    node: dict[str, Any] = {
        "path": list(path),
        "role": _atspi_role(element),
    }
    name = _atspi_name(element)
    if name:
        node["title"] = name
        node["name"] = name
    description = _atspi_description(element)
    if description:
        node["description"] = description
    pid = _atspi_pid(element)
    if pid is not None:
        node["pid"] = pid
    frame = _atspi_frame(Atspi, element, session)
    if frame is not None:
        node["frame"] = frame
    states = _atspi_states(Atspi, element)
    if states:
        node["states"] = states
    actions = _atspi_actions(element)
    if actions:
        node["actions"] = actions
    text = _atspi_text(element)
    if text and text != name:
        node["text"] = text
    value = _atspi_value(element)
    if value is not None:
        node["value"] = value
    return node


def _serialize_atspi_element(
    Atspi: Any,
    element: object,
    *,
    path: list[int],
    session: PortalSession,
    depth: int,
    max_depth: int,
    budget: dict[str, Any],
) -> dict[str, Any] | None:
    if int(budget["count"]) >= int(budget["max_nodes"]):
        budget["truncated"] = True
        return None
    budget["count"] = int(budget["count"]) + 1
    node = _atspi_node_metadata(Atspi, element, path=path, session=session)
    if depth >= max_depth:
        child_count = _atspi_child_count(element)
        if child_count > 0:
            budget["truncated"] = True
        return node
    children: list[dict[str, Any]] = []
    for index in range(_atspi_child_count(element)):
        if int(budget["count"]) >= int(budget["max_nodes"]):
            budget["truncated"] = True
            break
        child = _atspi_child_at(element, index)
        if child is None:
            continue
        child_node = _serialize_atspi_element(
            Atspi,
            child,
            path=[*path, index],
            session=session,
            depth=depth + 1,
            max_depth=max_depth,
            budget=budget,
        )
        if child_node is not None:
            children.append(child_node)
    if children:
        node["children"] = children
    return node


def _atspi_element_for_path(desktop: object, path: list[int]) -> object | None:
    element = desktop
    for index in path:
        if index >= _atspi_child_count(element):
            return None
        element = _atspi_child_at(element, index)
        if element is None:
            return None
    return element


def _atspi_interface_call(
    Atspi: Any,
    interface_name: str,
    method_name: str,
    element: object,
    *args: object,
) -> Any:
    interface = getattr(Atspi, interface_name, None)
    method = getattr(interface, method_name, None)
    if callable(method):
        try:
            return method(element, *args)
        except Exception:
            pass
    return _safe_call(element, method_name, *args)


def _atspi_text_count(Atspi: Any, element: object) -> int:
    return max(
        0,
        _safe_int(
            _atspi_interface_call(Atspi, "Text", "get_character_count", element),
            default=0,
        ),
    )


def _atspi_text_range(Atspi: Any, element: object, start: int, end: int) -> str:
    value = _atspi_interface_call(Atspi, "Text", "get_text", element, start, end)
    return str(value or "").replace("\x00", "")


def _atspi_caret_offset(Atspi: Any, element: object) -> int:
    value = _atspi_interface_call(Atspi, "Text", "get_caret_offset", element)
    if value is None:
        value = _safe_call(element, "get_caret_offset")
    return _safe_int(value, default=-1)


def _atspi_find_focused(
    Atspi: Any,
    root: object,
    *,
    max_nodes: int = _AX_TARGET_SEARCH_MAX_NODES,
) -> tuple[object, list[int]] | None:
    active_roots: list[tuple[object, list[int]]] = []
    for app_index in range(_atspi_child_count(root)):
        app = _atspi_child_at(root, app_index)
        if app is None:
            continue
        app_active = "active" in _atspi_states(Atspi, app)
        active_window_found = False
        for window_index in range(_atspi_child_count(app)):
            window = _atspi_child_at(app, window_index)
            if window is not None and "active" in _atspi_states(Atspi, window):
                active_roots.append((window, [app_index, window_index]))
                active_window_found = True
        if app_active and not active_window_found:
            active_roots.append((app, [app_index]))

    search_roots = active_roots or [(root, [])]
    candidates: list[tuple[object, list[int]]] = []
    for search_root, search_path in search_roots:
        stack: list[tuple[object, list[int]]] = [(search_root, search_path)]
        visited = 0
        while stack and visited < max_nodes:
            element, path = stack.pop()
            visited += 1
            if "focused" in _atspi_states(Atspi, element):
                candidates.append((element, path))
            for index in range(_atspi_child_count(element)):
                child = _atspi_child_at(element, index)
                if child is not None:
                    stack.append((child, [*path, index]))
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda item: (
            _atspi_text_count(Atspi, item[0]) > 0 and _atspi_caret_offset(Atspi, item[0]) >= 0,
            len(item[1]),
        ),
    )


def _atspi_window_path(Atspi: Any, desktop: object, path: list[int]) -> list[int]:
    for length in range(len(path), 0, -1):
        candidate_path = path[:length]
        candidate = _atspi_element_for_path(desktop, candidate_path)
        if candidate is not None and _atspi_role(candidate).casefold() in _AT_SPI_WINDOW_ROLES:
            return candidate_path
    raise PortalError(
        "A0_TAG_WINDOW_UNAVAILABLE",
        "The focused field is not inside an accessible application window.",
    )


def _atspi_window_is_active(Atspi: Any, desktop: object, window_path: list[int]) -> bool:
    for candidate_path in (window_path, window_path[:1]):
        candidate = _atspi_element_for_path(desktop, candidate_path)
        if candidate is not None and {"active", "focused"} & set(_atspi_states(Atspi, candidate)):
            return True
    return False


def _parse_tag_invocation(
    Atspi: Any,
    element: object,
) -> tuple[int, int, str, str, str, str]:
    character_count = _atspi_text_count(Atspi, element)
    caret = _atspi_caret_offset(Atspi, element)
    if character_count <= 0 or caret < 0 or caret > character_count:
        raise PortalError("A0_TAG_TEXT_UNAVAILABLE", "The focused field has no readable caret text.")

    before_start = max(0, caret - _TAG_TEXT_WINDOW_CHARS)
    before = _atspi_text_range(Atspi, element, before_start, caret)
    newline = max(before.rfind("\n"), before.rfind("\r"))
    if newline < 0 and before_start > 0:
        raise PortalError("A0_TAG_QUERY_TOO_LONG", "The A0 Tag line is too long.")
    line_start = before_start + newline + 1
    line = before[newline + 1 :]

    after_end = min(character_count, caret + _TAG_TEXT_WINDOW_CHARS)
    after = _atspi_text_range(Atspi, element, caret, after_end)
    after_line = re.split(r"[\r\n]", after, maxsplit=1)[0]
    after_line_bounded = "\n" in after or "\r" in after or after_end == character_count
    if not after_line_bounded or after_line.strip():
        raise PortalError("A0_TAG_CARET_POSITION", "Place the caret at the end of the A0 Tag request.")

    match = re.fullmatch(
        r"(?P<indent>[ \t]*)(?P<tag>@a0(?:\.(?P<profile>[A-Za-z0-9][A-Za-z0-9_-]{0,63}))?[ \t]+(?P<query>.*?))(?P<trailing>[ \t]*)",
        line,
        flags=re.IGNORECASE,
    )
    if match is None:
        raise PortalError("A0_TAG_NOT_FOUND", "The focused line does not contain a valid @a0 request.")
    query = str(match.group("query") or "").strip()
    if not query:
        raise PortalError("A0_TAG_EMPTY_QUERY", "A0 Tag requires a request after the tag.")
    if len(query) > _TAG_QUERY_MAX_CHARS:
        raise PortalError("A0_TAG_QUERY_TOO_LONG", "A0 Tag requests are limited to 2048 characters.")
    profile = str(match.group("profile") or "")
    if profile and not _TAG_PROFILE_RE.fullmatch(profile):
        raise PortalError("A0_TAG_INVALID_PROFILE", "The A0 Tag profile key is invalid.")
    original = str(match.group("tag") or "")
    start = line_start + len(str(match.group("indent") or ""))
    end = start + len(original)
    focused_context = _atspi_text_range(
        Atspi,
        element,
        max(0, start - _TAG_TEXT_WINDOW_CHARS),
        min(character_count, end + _TAG_TEXT_WINDOW_CHARS),
    )
    return start, end, original, query, profile, focused_context


def _replace_atspi_text(
    Atspi: Any,
    element: object,
    *,
    start: int,
    end: int,
    original: str,
    replacement: str,
) -> None:
    original_count = _atspi_text_count(Atspi, element)
    replacement_bytes = len(replacement.encode("utf-8"))
    original_bytes = len(original.encode("utf-8"))
    deleted = _atspi_interface_call(Atspi, "EditableText", "delete_text", element, start, end)
    if deleted is False or deleted is None:
        raise PortalError("A0_TAG_REPLACE_FAILED", "The focused field rejected range deletion.")
    inserted = _atspi_interface_call(
        Atspi,
        "EditableText",
        "insert_text",
        element,
        start,
        replacement,
        replacement_bytes,
    )
    if inserted is False or inserted is None:
        _atspi_interface_call(
            Atspi,
            "EditableText",
            "insert_text",
            element,
            start,
            original,
            original_bytes,
        )
        raise PortalError(
            "A0_TAG_REPLACE_FAILED",
            "The field rejected replacement; the original tag was restored where possible.",
        )
    actual = _atspi_text_range(Atspi, element, start, start + len(replacement))
    inserted_count = _atspi_text_count(Atspi, element) - (original_count - (end - start))
    if actual != replacement or inserted_count != len(replacement):
        if 0 <= inserted_count <= _TAG_REPLACEMENT_MAX_CHARS:
            _atspi_interface_call(
                Atspi,
                "EditableText",
                "delete_text",
                element,
                start,
                start + inserted_count,
            )
            _atspi_interface_call(
                Atspi,
                "EditableText",
                "insert_text",
                element,
                start,
                original,
                original_bytes,
            )
        raise PortalError(
            "A0_TAG_REPLACE_FAILED",
            "The field changed the replacement; the original tag was restored where possible.",
        )


def _atspi_window_id(node: dict[str, Any], *, path: list[int]) -> str:
    path_text = ".".join(str(item) for item in path)
    pid = node.get("pid")
    if pid not in (None, ""):
        return f"atspi-pid:{pid}:path:{path_text}"
    return f"atspi-path:{path_text}"


def _parse_atspi_window_id(window_id: str) -> tuple[int | None, list[int] | None]:
    value = str(window_id or "").strip()
    if not value:
        return None, None
    pid: int | None = None
    path_text = ""
    if value.startswith("atspi-pid:") and ":path:" in value:
        pid_text, path_text = value.removeprefix("atspi-pid:").split(":path:", 1)
        try:
            pid = int(pid_text)
        except ValueError:
            pid = None
    elif value.startswith("atspi-path:"):
        path_text = value.removeprefix("atspi-path:")
    else:
        return None, None
    return pid, _parse_ax_path(path_text) or []


def _atspi_node_is_visible(node: dict[str, Any]) -> bool:
    states = {str(item).strip().casefold() for item in node.get("states", [])}
    if states:
        return "visible" in states or "showing" in states
    return True


def _atspi_node_is_offscreen(node: dict[str, Any]) -> bool:
    frame = node.get("frame")
    if not isinstance(frame, dict):
        return False
    try:
        x = float(frame.get("normalized_x"))
        y = float(frame.get("normalized_y"))
        width = float(frame.get("normalized_width"))
        height = float(frame.get("normalized_height"))
    except (TypeError, ValueError):
        return False
    return x + width <= 0 or y + height <= 0 or x >= 1 or y >= 1


def _atspi_focus_state(Atspi: Any, element: object) -> tuple[bool, bool]:
    active = False
    focused = False
    queue = [element]
    visited = 0
    while queue and visited < _AT_SPI_FOCUS_SEARCH_MAX_NODES:
        current = queue.pop(0)
        visited += 1
        states = set(_atspi_states(Atspi, current))
        active = active or "active" in states
        focused = focused or "focused" in states
        if active or focused:
            return active, focused
        for index in range(_atspi_child_count(current)):
            child = _atspi_child_at(current, index)
            if child is not None:
                queue.append(child)
    return active, focused


def _atspi_wait_for_focus(Atspi: Any, element: object, *, timeout: float = 0.25) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if any(_atspi_focus_state(Atspi, element)):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def _activate_xwayland_window(target: dict[str, Any]) -> bool:
    try:
        pid = int(target.get("pid"))
    except (TypeError, ValueError):
        return False
    try:
        listed = subprocess.run(
            ["wmctrl", "-lp"],
            check=False,
            capture_output=True,
            text=True,
            timeout=1,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if listed.returncode != 0:
        return False

    candidates: list[tuple[str, str]] = []
    for line in listed.stdout.splitlines():
        parts = line.split(maxsplit=4)
        if len(parts) < 3:
            continue
        try:
            window_pid = int(parts[2])
        except ValueError:
            continue
        if window_pid == pid:
            candidates.append((parts[0], parts[4] if len(parts) == 5 else ""))
    title = str(target.get("title") or "").strip()
    exact = [candidate for candidate in candidates if title and candidate[1] == title]
    selected = exact[0] if len(exact) == 1 else candidates[0] if len(candidates) == 1 else None
    if selected is None:
        return False
    try:
        activated = subprocess.run(
            ["wmctrl", "-ia", selected[0]],
            check=False,
            capture_output=True,
            timeout=1,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return activated.returncode == 0


def _focus_atspi_element(Atspi: Any, element: object, *, target: dict[str, Any]) -> None:
    accepted = _safe_call(element, "grab_focus") is not False
    if accepted and _atspi_wait_for_focus(Atspi, element):
        return
    role = str(target.get("role") or "").strip().casefold()
    if role in _AT_SPI_WINDOW_ACTIVATION_ROLES and _activate_xwayland_window(target):
        accepted = True
        if _atspi_wait_for_focus(Atspi, element):
            return
    if accepted:
        raise PortalError(
            "COMPUTER_USE_WINDOW_FOCUS_UNVERIFIED",
            "The focus request was accepted but the target did not report active or focused.",
        )
    raise PortalError("COMPUTER_USE_AX_ACTION_FAILED", "AT-SPI focus action failed.")


def _atspi_window_candidates(
    Atspi: Any,
    application: object,
    *,
    application_path: list[int],
    session: PortalSession,
) -> list[tuple[object, list[int], dict[str, Any]]]:
    application_node = _atspi_node_metadata(
        Atspi,
        application,
        path=application_path,
        session=session,
    )
    candidates: list[tuple[object, list[int], dict[str, Any]]] = []
    queue = [
        (child, [*application_path, index], 1)
        for index in range(_atspi_child_count(application))
        if (child := _atspi_child_at(application, index)) is not None
    ]
    while queue:
        element, path, depth = queue.pop(0)
        node = _atspi_node_metadata(Atspi, element, path=path, session=session)
        if str(node.get("role") or "").casefold() in _AT_SPI_WINDOW_ROLES:
            candidates.append((element, path, node))
            continue
        if depth < 2:
            for index in range(_atspi_child_count(element)):
                child = _atspi_child_at(element, index)
                if child is not None:
                    queue.append((child, [*path, index], depth + 1))
    if candidates:
        return candidates
    if application_node.get("frame"):
        return [(application, application_path, application_node)]
    return []


def _node_text_fields(node: dict[str, Any], keys: tuple[str, ...]) -> list[str]:
    values: list[str] = []
    for key in keys:
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            values.append(value)
    return values


def _atspi_node_matches_target(node: dict[str, Any], target: dict[str, Any]) -> bool:
    comparable = {
        "role": ("role",),
        "title": ("title", "name", "text", "description"),
        "name": ("name", "title", "text", "description"),
        "description": ("description",),
        "text": ("text", "title", "name", "description"),
        "value": ("value",),
    }
    for target_key, node_keys in comparable.items():
        if target.get(target_key) is None:
            continue
        wanted = _normalized_match_text(target.get(target_key))
        if not wanted:
            continue
        haystacks = [_normalized_match_text(value) for value in _node_text_fields(node, node_keys)]
        if target_key == "value" and node.get("value") is not None:
            haystacks.append(_normalized_match_text(node.get("value")))
        if target_key == "role":
            if not any(wanted == haystack or wanted in haystack for haystack in haystacks):
                return False
            continue
        if not any(wanted == haystack or wanted in haystack for haystack in haystacks):
            return False
    target_actions = target.get("actions")
    if isinstance(target_actions, str):
        wanted_actions = {target_actions.strip().casefold()}
    elif isinstance(target_actions, (list, tuple, set)):
        wanted_actions = {str(item).strip().casefold() for item in target_actions if str(item).strip()}
    else:
        wanted_actions = set()
    if wanted_actions:
        node_actions = {
            str(item.get("name") or "").strip().casefold()
            for item in node.get("actions", [])
            if isinstance(item, dict)
        }
        if not wanted_actions & node_actions:
            return False
    target_states = target.get("states")
    if isinstance(target_states, str):
        wanted_states = {target_states.strip().casefold()}
    elif isinstance(target_states, (list, tuple, set)):
        wanted_states = {str(item).strip().casefold() for item in target_states if str(item).strip()}
    else:
        wanted_states = set()
    if wanted_states:
        node_states = {str(item).strip().casefold() for item in node.get("states", [])}
        if not wanted_states <= node_states:
            return False
    return True


def _atspi_match_score(node: dict[str, Any], operation: str) -> int:
    score = 0
    states = {str(item).casefold() for item in node.get("states", [])}
    if "showing" in states:
        score += 4
    if "visible" in states:
        score += 2
    if "enabled" in states:
        score += 2
    if operation == "press" and _list_contains_action(node.get("actions"), _AT_SPI_PRESS_ACTION_NAMES):
        score += 8
    if operation == "focus" and ("focusable" in states or "focused" in states):
        score += 4
    if operation == "set_value" and ("editable" in states or "text" in str(node.get("role", "")).casefold()):
        score += 6
    if node.get("frame"):
        score += 1
    return score


def _find_atspi_matches(
    Atspi: Any,
    desktop: object,
    *,
    session: PortalSession,
    target: dict[str, Any],
    operation: str,
) -> list[tuple[int, object, dict[str, Any]]]:
    matches: list[tuple[int, object, dict[str, Any]]] = []
    queue: list[tuple[object, list[int]]] = [(desktop, [])]
    visited = 0
    while queue and visited < _AX_TARGET_SEARCH_MAX_NODES:
        element, path = queue.pop(0)
        visited += 1
        node = _atspi_node_metadata(Atspi, element, path=path, session=session)
        if _atspi_node_matches_target(node, target):
            matches.append((_atspi_match_score(node, operation), element, node))
        for index in range(_atspi_child_count(element)):
            child = _atspi_child_at(element, index)
            if child is not None:
                queue.append((child, [*path, index]))
    return matches


def _target_summary(targets: list[dict[str, Any]]) -> str:
    summaries: list[str] = []
    for node in targets[:5]:
        role = str(node.get("role") or "element")
        title = str(node.get("title") or node.get("name") or node.get("text") or "").strip()
        path = node.get("path", [])
        if title:
            summaries.append(f"{role} {title!r} path={path}")
        else:
            summaries.append(f"{role} path={path}")
    return "; ".join(summaries)


class CaptureStream:
    def __init__(self, pipewire_fd: int, stream_id: int) -> None:
        self._pipewire_fd = pipewire_fd
        self._stream_id = stream_id
        self._pipeline: Gst.Pipeline | None = None
        self._sink: Gst.Element | None = None
        self._sample_lock = threading.Condition()
        self._sample_bytes = b""
        self._sample_width = 0
        self._sample_height = 0
        self._sample_time = 0.0
        self._start_pipeline()

    def close(self) -> None:
        pipeline = self._pipeline
        self._pipeline = None
        if pipeline is not None:
            pipeline.set_state(Gst.State.NULL)
        self._sink = None
        try:
            os.close(self._pipewire_fd)
        except OSError:
            pass

    def capture_png(
        self,
        output_path: str | None = None,
        *,
        timeout: float = 5.0,
        fresh_after: float = 0.0,
        fresh_timeout: float = 0.0,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + max(timeout, 0.1)
        fresh_deadline = time.monotonic() + max(fresh_timeout, 0.0)
        with self._sample_lock:
            while not self._sample_bytes and time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                self._sample_lock.wait(timeout=max(remaining, 0.05))
            if not self._sample_bytes:
                raise PortalError("COMPUTER_USE_CAPTURE_UNAVAILABLE", "No screen frame is available yet.")

            while (
                fresh_after > 0
                and fresh_timeout > 0
                and self._sample_time < fresh_after
                and time.monotonic() < fresh_deadline
            ):
                remaining = fresh_deadline - time.monotonic()
                self._sample_lock.wait(timeout=max(remaining, 0.01))

            data = self._sample_bytes
            width = self._sample_width
            height = self._sample_height
            frame_time = self._sample_time

        image = Image.frombytes("RGBA", (width, height), data)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        png_bytes = buffer.getvalue()
        result = {
            "width": width,
            "height": height,
            "captured_at": time.time(),
            "frame_captured_at": frame_time,
        }
        if output_path:
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            Path(output_path).write_bytes(png_bytes)
            result["capture_path"] = output_path
        else:
            result["png_base64"] = base64.b64encode(png_bytes).decode("ascii")
        return result

    def _start_pipeline(self) -> None:
        pipeline = Gst.parse_launch(
            "pipewiresrc fd={fd} path={stream} keepalive-time=1000 ! "
            "videoconvert ! video/x-raw,format=RGBA ! "
            "appsink name=sink emit-signals=true sync=false max-buffers=1 drop=true".format(
                fd=self._pipewire_fd,
                stream=self._stream_id,
            )
        )
        sink = pipeline.get_by_name("sink")
        if sink is None:
            raise PortalError("COMPUTER_USE_CAPTURE_INIT_FAILED", "Failed to create PipeWire appsink.")
        sink.connect("new-sample", self._on_new_sample)
        pipeline.set_state(Gst.State.PLAYING)
        self._pipeline = pipeline
        self._sink = sink

    def _on_new_sample(self, sink: Gst.Element) -> Gst.FlowReturn:
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.ERROR

        caps = sample.get_caps()
        structure = caps.get_structure(0) if caps is not None else None
        width = int(structure.get_value("width")) if structure is not None else 0
        height = int(structure.get_value("height")) if structure is not None else 0
        buffer = sample.get_buffer()
        if buffer is None or width <= 0 or height <= 0:
            return Gst.FlowReturn.ERROR

        success, map_info = buffer.map(Gst.MapFlags.READ)
        if not success:
            return Gst.FlowReturn.ERROR
        try:
            payload = bytes(map_info.data)
        finally:
            buffer.unmap(map_info)

        with self._sample_lock:
            self._sample_bytes = payload
            self._sample_width = width
            self._sample_height = height
            self._sample_time = time.time()
            self._sample_lock.notify_all()
        return Gst.FlowReturn.OK


class PortalComputerUseHelper:
    def __init__(self) -> None:
        DBusGMainLoop(set_as_default=True)
        Gst.init(None)
        self._bus = dbus.SessionBus()
        self._bus_name = self._bus.get_unique_name()
        portal = self._bus.get_object(PORTAL_SERVICE, PORTAL_PATH)
        self._remote_desktop = dbus.Interface(portal, PORTAL_REMOTE_DESKTOP_IFACE)
        self._screencast = dbus.Interface(portal, PORTAL_SCREENCAST_IFACE)
        self._session: PortalSession | None = None
        self._element_index_cache: dict[int, dict[str, Any]] = {}
        self._tag_target: TagTarget | None = None

    def dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        handlers = {
            "start_session": self.start_session,
            "status": self.status,
            "capture": self.capture,
            "tag_context": self.tag_context,
            "tag_replace": self.tag_replace,
            "tag_release": self.tag_release,
            "list_windows": self.list_windows,
            "get_window_state": self.get_window_state,
            "element_action": self.element_action,
            "ax_snapshot": self.ax_snapshot,
            "ax_action": self.ax_action,
            "move": self.move,
            "click": self.click,
            "scroll": self.scroll,
            "key": self.key,
            "type": self.type_text,
            "stop_session": self.stop_session,
        }
        handler = handlers.get(method)
        if handler is None:
            raise PortalError("UNKNOWN_METHOD", f"Unknown computer-use helper method: {method}")
        return handler(params)

    def start_session(self, params: dict[str, Any]) -> dict[str, Any]:
        trust_mode = str(params.get("trust_mode") or "persistent").strip().lower()
        context_id = str(params.get("context_id") or "default").strip() or "default"
        restore_token = str(params.get("restore_token") or "").strip()
        allow_prompt = bool(params.get("allow_prompt", trust_mode != "allow"))
        request_timeout = float(params.get("request_timeout_seconds") or 0.0)
        allow = trust_mode == "allow" or not allow_prompt
        if trust_mode == "allow" and not restore_token:
            raise PortalError(
                "COMPUTER_USE_REARM_REQUIRED",
                "Allow requires a stored restore token.",
            )

        self._close_session()
        session_handle = self._create_session()
        timeout = request_timeout if request_timeout > 0 else None
        try:
            self._select_devices(
                session_handle,
                trust_mode=trust_mode,
                restore_token=restore_token,
                timeout=timeout,
                allow=allow,
            )
            self._select_sources(session_handle, timeout=timeout, allow=allow)
            start_results = self._start_remote_desktop(
                session_handle,
                timeout=timeout,
                allow=allow,
            )
        except PortalError as exc:
            self._close_portal_session(session_handle)
            raise exc

        streams = start_results.get("streams")
        if not isinstance(streams, list) or not streams:
            self._close_portal_session(session_handle)
            raise PortalError("COMPUTER_USE_NO_STREAM", "The portal session did not return a screen stream.")

        stream_node, properties = streams[0]
        if not isinstance(properties, dict):
            properties = {}
        size = properties.get("size") or properties.get("logical_size") or (0, 0)
        width = int(size[0]) if isinstance(size, (list, tuple)) and len(size) >= 2 else 0
        height = int(size[1]) if isinstance(size, (list, tuple)) and len(size) >= 2 else 0
        pipewire_fd = self._open_pipewire_remote(session_handle)
        capture_stream = CaptureStream(pipewire_fd, int(stream_node))
        session = PortalSession(
            context_id=context_id,
            trust_mode=trust_mode,
            session_id=uuid.uuid4().hex,
            session_handle=session_handle,
            stream_id=int(stream_node),
            width=width,
            height=height,
            devices=int(start_results.get("devices") or 0),
            restore_token=str(start_results.get("restore_token") or "").strip(),
            capture_stream=capture_stream,
        )
        self._session = session
        return self._session_payload(session)

    def status(self, params: dict[str, Any]) -> dict[str, Any]:
        del params
        if self._session is None:
            return {"active": False, **_backend_contract_metadata()}
        payload = self._session_payload(self._session)
        payload["active"] = True
        return payload

    def capture(self, params: dict[str, Any]) -> dict[str, Any]:
        session = self._require_session(params)
        capture_path = str(params.get("capture_path") or "").strip()
        result = session.capture_stream.capture_png(
            capture_path or None,
            fresh_after=_float_param(params.get("fresh_after"), default=0.0),
            fresh_timeout=_float_param(params.get("fresh_timeout_seconds"), default=0.0),
        )
        result["stream_id"] = session.stream_id
        result["session_id"] = session.session_id
        return result

    def tag_context(self, params: dict[str, Any]) -> dict[str, Any]:
        session = self._require_session(params)
        Atspi = _load_atspi_module()
        desktop = _atspi_desktop(Atspi)
        focused = _atspi_find_focused(Atspi, desktop)
        if focused is None:
            raise PortalError("A0_TAG_FOCUS_UNAVAILABLE", "No accessible focused field was found.")
        element, path = focused
        role = _atspi_role(element).casefold()
        states = set(_atspi_states(Atspi, element))
        if "protected" in states or "password" in role:
            raise PortalError("A0_TAG_PROTECTED_FIELD", "A0 Tag is unavailable in protected fields.")

        start, end, original, query, profile, focused_context = _parse_tag_invocation(
            Atspi,
            element,
        )
        window_path = _atspi_window_path(Atspi, desktop, path)
        window_element = _atspi_element_for_path(desktop, window_path)
        if window_element is None:
            raise PortalError("A0_TAG_WINDOW_UNAVAILABLE", "The active window is no longer available.")
        if not _atspi_window_is_active(Atspi, desktop, window_path):
            raise PortalError("A0_TAG_WINDOW_INACTIVE", "The tagged window is no longer active.")
        window = _atspi_node_metadata(
            Atspi,
            window_element,
            path=window_path,
            session=session,
        )
        window_id = _atspi_window_id(window, path=window_path)
        app_element = _atspi_element_for_path(desktop, path[:1]) if path else None
        app_name = _atspi_name(app_element) if app_element is not None else "Linux app"
        window_title = str(window.get("title") or window.get("name") or app_name or "Linux app")
        budget: dict[str, Any] = {"count": 0, "max_nodes": 120, "truncated": False}
        tree = _serialize_atspi_element(
            Atspi,
            window_element,
            path=window_path,
            session=session,
            depth=0,
            max_depth=5,
            budget=budget,
        ) or {}
        editable = "editable" in states and (
            callable(getattr(getattr(Atspi, "EditableText", None), "delete_text", None))
            or callable(getattr(element, "delete_text", None))
        )
        target = TagTarget(
            token=uuid.uuid4().hex,
            path=list(path),
            window_path=list(window_path),
            window_id=window_id,
            app_name=app_name or "Linux app",
            window_title=window_title,
            start=start,
            end=end,
            original=original,
            editable=editable,
            captured_at=time.time(),
        )
        self._tag_target = target

        screenshot_status = "unavailable"
        screenshot_error = (
            "The Wayland compositor did not expose verified active-window bounds; "
            "A0 Tag continued with text and accessibility context only."
        )

        return {
            "session_id": session.session_id,
            "context_id": session.context_id,
            "target_token": target.token,
            "tag_text": original,
            "query": query,
            "profile_override": profile,
            "app_name": target.app_name,
            "window_title": target.window_title,
            "window_id": target.window_id,
            "focused_text": focused_context,
            "tree": tree,
            "tree_truncated": bool(budget["truncated"]),
            "replace_supported": editable,
            "screenshot_status": screenshot_status,
            **({"screenshot_error": screenshot_error} if screenshot_error else {}),
        }

    def tag_replace(self, params: dict[str, Any]) -> dict[str, Any]:
        session = self._require_session(params)
        token = str(params.get("target_token") or "").strip()
        replacement = str(params.get("replacement") or "")
        target = self._tag_target
        if target is None or not token or token != target.token:
            raise PortalError("A0_TAG_TARGET_EXPIRED", "The original A0 Tag field is no longer available.")
        if time.time() - target.captured_at > _TAG_TARGET_TTL_SECONDS:
            self._tag_target = None
            raise PortalError("A0_TAG_TARGET_EXPIRED", "The original A0 Tag field expired.")
        if not target.editable:
            raise PortalError("A0_TAG_REPLACE_UNSUPPORTED", "The tagged field does not support safe replacement.")
        if not replacement or len(replacement) > _TAG_REPLACEMENT_MAX_CHARS:
            raise PortalError(
                "A0_TAG_INVALID_REPLACEMENT",
                "A0 Tag replacement must contain 1 to 16384 characters.",
            )

        Atspi = _load_atspi_module()
        desktop = _atspi_desktop(Atspi)
        focused = _atspi_find_focused(Atspi, desktop)
        if focused is None or focused[1] != target.path:
            raise PortalError("A0_TAG_TARGET_CHANGED", "The focused field changed while Agent Zero was working.")
        element, _path = focused
        if _atspi_window_path(Atspi, desktop, target.path) != target.window_path:
            raise PortalError("A0_TAG_TARGET_CHANGED", "The active window changed while Agent Zero was working.")
        window_element = _atspi_element_for_path(desktop, target.window_path)
        if window_element is None or not _atspi_window_is_active(Atspi, desktop, target.window_path):
            raise PortalError("A0_TAG_TARGET_CHANGED", "The active window changed while Agent Zero was working.")
        window = _atspi_node_metadata(
            Atspi,
            window_element,
            path=target.window_path,
            session=session,
        )
        current_title = str(window.get("title") or window.get("name") or target.app_name or "Linux app")
        if (
            _atspi_window_id(window, path=target.window_path) != target.window_id
            or current_title != target.window_title
        ):
            raise PortalError("A0_TAG_TARGET_CHANGED", "The active window changed while Agent Zero was working.")
        current = _atspi_text_range(Atspi, element, target.start, target.end)
        if current != target.original:
            raise PortalError("A0_TAG_TARGET_CHANGED", "The original A0 Tag text changed while Agent Zero was working.")
        _replace_atspi_text(
            Atspi,
            element,
            start=target.start,
            end=target.end,
            original=target.original,
            replacement=replacement,
        )
        self._tag_target = None
        return {
            "session_id": session.session_id,
            "replaced": True,
            "characters": len(replacement),
        }

    def tag_release(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_session(params)
        token = str(params.get("target_token") or "").strip()
        released = self._tag_target is not None and token == self._tag_target.token
        if released:
            self._tag_target = None
        return {"released": released}

    def list_windows(self, params: dict[str, Any]) -> dict[str, Any]:
        session = self._require_session(params)
        Atspi = _load_atspi_module()
        desktop = _atspi_desktop(Atspi)
        max_windows = _int_param(params.get("max_windows"), default=80, minimum=1, maximum=500)
        include_hidden = _bool_param(params.get("include_hidden"), default=False)
        include_offscreen = _bool_param(params.get("include_offscreen"), default=False)
        windows: list[dict[str, Any]] = []
        for application_index in range(_atspi_child_count(desktop)):
            application = _atspi_child_at(desktop, application_index)
            if application is None:
                continue
            application_path = [application_index]
            application_node = _atspi_node_metadata(
                Atspi,
                application,
                path=application_path,
                session=session,
            )
            app_name = application_node.get("title") or application_node.get("name")
            for element, path, node in _atspi_window_candidates(
                Atspi,
                application,
                application_path=application_path,
                session=session,
            ):
                if not include_hidden and not _atspi_node_is_visible(node):
                    continue
                if not include_offscreen and _atspi_node_is_offscreen(node):
                    continue
                active, focused = _atspi_focus_state(Atspi, element)
                windows.append(
                    {
                        "window_id": _atspi_window_id(node, path=path),
                        "pid": node.get("pid") or application_node.get("pid"),
                        "app_name": app_name,
                        "title": node.get("title") or node.get("name") or app_name,
                        "role": node.get("role", "window"),
                        "frame": node.get("frame"),
                        "active": active,
                        "focused": focused,
                        "visible": _atspi_node_is_visible(node),
                        "path": path,
                    }
                )
                if len(windows) >= max_windows:
                    break
            if len(windows) >= max_windows:
                break
        return {
            "session_id": session.session_id,
            "context_id": session.context_id,
            "backend": "at-spi",
            "count": len(windows),
            "windows": windows,
        }

    def get_window_state(self, params: dict[str, Any]) -> dict[str, Any]:
        session = self._require_session(params)
        Atspi = _load_atspi_module()
        desktop = _atspi_desktop(Atspi)
        max_depth = _int_param(
            params.get("max_depth"),
            default=_AX_DEFAULT_MAX_DEPTH,
            minimum=0,
            maximum=_AX_HARD_MAX_DEPTH,
        )
        max_nodes = _int_param(
            params.get("max_nodes"),
            default=_AX_DEFAULT_MAX_NODES,
            minimum=1,
            maximum=_AX_HARD_MAX_NODES,
        )
        element, path, window = self._resolve_atspi_window_root(Atspi, desktop, session=session, params=params)
        budget: dict[str, Any] = {
            "count": 0,
            "max_nodes": max_nodes,
            "truncated": False,
        }
        tree = _serialize_atspi_element(
            Atspi,
            element,
            path=path,
            session=session,
            depth=0,
            max_depth=max_depth,
            budget=budget,
        ) or {}
        window_id = _atspi_window_id(window, path=path)
        active, focused = _atspi_focus_state(Atspi, element)
        window["active"] = active
        window["focused"] = focused
        self._cache_element_indices(tree, window_id=window_id)
        window["window_id"] = window_id
        return {
            "session_id": session.session_id,
            "context_id": session.context_id,
            "backend": "at-spi",
            "mode": str(params.get("mode") or "at-spi").strip() or "at-spi",
            "window_id": window_id,
            "window": window,
            "app": {"name": window.get("title") or window.get("name") or "Linux app", "backend": "at-spi"},
            "tree": tree,
            "node_count": budget["count"],
            "truncated": bool(budget["truncated"]),
            "max_depth": max_depth,
            "max_nodes": max_nodes,
        }

    def element_action(self, params: dict[str, Any]) -> dict[str, Any]:
        session = self._require_session(params)
        Atspi = _load_atspi_module()
        desktop = _atspi_desktop(Atspi)
        dispatch = _normalize_dispatch(params.get("dispatch"), default="background")
        operation = str(params.get("operation") or params.get("name") or "press").strip().lower()
        operation_aliases = {
            "activate": "press",
            "click": "press",
            "invoke": "press",
            "type": "set_value",
            "type_text": "set_value",
        }
        operation = operation_aliases.get(operation, operation)
        element, target = self._resolve_element_action_target(
            Atspi,
            desktop,
            session=session,
            params=params,
            operation=operation,
        )
        target.setdefault("element_index", params.get("element_index"))

        if dispatch in {"background", "auto"}:
            background_result = self._try_background_atspi_action(
                Atspi,
                element,
                target=target,
                operation=operation,
                params=params,
                session=session,
                requested_dispatch=dispatch,
            )
            if not background_result.get("background_unavailable"):
                return background_result
            if dispatch == "background":
                return background_result

        foreground_result = self._perform_atspi_element_action(
            Atspi,
            element,
            target=target,
            operation=operation,
            params=params,
            session=session,
        )
        foreground_result["requested_dispatch"] = dispatch
        foreground_result["actual_dispatch"] = "foreground"
        foreground_result["foreground_fallback_used"] = dispatch == "auto"
        return foreground_result

    def ax_snapshot(self, params: dict[str, Any]) -> dict[str, Any]:
        session = self._require_session(params)
        Atspi = _load_atspi_module()
        desktop = _atspi_desktop(Atspi)
        max_depth = _int_param(
            params.get("max_depth"),
            default=_AX_DEFAULT_MAX_DEPTH,
            minimum=0,
            maximum=_AX_HARD_MAX_DEPTH,
        )
        max_nodes = _int_param(
            params.get("max_nodes"),
            default=_AX_DEFAULT_MAX_NODES,
            minimum=1,
            maximum=_AX_HARD_MAX_NODES,
        )
        budget: dict[str, Any] = {
            "count": 0,
            "max_nodes": max_nodes,
            "truncated": False,
        }
        if params.get("window_id") or params.get("pid") is not None:
            element, path, window = self._resolve_atspi_window_root(
                Atspi,
                desktop,
                session=session,
                params=params,
            )
            tree = _serialize_atspi_element(
                Atspi,
                element,
                path=path,
                session=session,
                depth=0,
                max_depth=max_depth,
                budget=budget,
            ) or {}
            window_id = _atspi_window_id(window, path=path)
            active, focused = _atspi_focus_state(Atspi, element)
            window.update({"window_id": window_id, "active": active, "focused": focused})
            return {
                "session_id": session.session_id,
                "context_id": session.context_id,
                "app": {
                    "name": window.get("title") or window.get("name") or "Linux window",
                    "backend": "at-spi",
                },
                "window_id": window_id,
                "window": window,
                "tree": tree,
                "node_count": budget["count"],
                "truncated": bool(budget["truncated"]),
                "max_depth": max_depth,
                "max_nodes": max_nodes,
                "scoped": True,
            }
        tree = {
            "path": [],
            "role": "Desktop",
            "title": "Linux desktop",
            "name": "Linux desktop",
            "frame": {
                "x": 0,
                "y": 0,
                "width": session.width,
                "height": session.height,
                "normalized_x": 0.0,
                "normalized_y": 0.0,
                "normalized_width": 1.0,
                "normalized_height": 1.0,
            },
            "children": [],
        }
        budget["count"] = 1
        if max_depth > 0:
            children: list[dict[str, Any]] = []
            for index in range(_atspi_child_count(desktop)):
                if int(budget["count"]) >= int(budget["max_nodes"]):
                    budget["truncated"] = True
                    break
                child = _atspi_child_at(desktop, index)
                if child is None:
                    continue
                child_node = _serialize_atspi_element(
                    Atspi,
                    child,
                    path=[index],
                    session=session,
                    depth=1,
                    max_depth=max_depth,
                    budget=budget,
                )
                if child_node is not None:
                    children.append(child_node)
            tree["children"] = children
        elif _atspi_child_count(desktop) > 0:
            budget["truncated"] = True
        return {
            "session_id": session.session_id,
            "context_id": session.context_id,
            "app": {"name": "Linux desktop", "backend": "at-spi"},
            "tree": tree,
            "node_count": budget["count"],
            "truncated": bool(budget["truncated"]),
            "max_depth": max_depth,
            "max_nodes": max_nodes,
            "scoped": False,
        }

    def ax_action(self, params: dict[str, Any]) -> dict[str, Any]:
        session = self._require_session(params)
        Atspi = _load_atspi_module()
        operation = str(params.get("operation") or params.get("name") or "press").strip().lower()
        operation_aliases = {
            "activate": "press",
            "click": "press",
            "invoke": "press",
            "type": "set_value",
            "type_text": "set_value",
        }
        operation = operation_aliases.get(operation, operation)
        if operation not in {"press", "focus", "set_value"}:
            raise PortalError(
                "COMPUTER_USE_BAD_AX_ACTION",
                "ax_action operation must be press, focus, or set_value.",
            )

        desktop = _atspi_desktop(Atspi)
        element, target = self._resolve_atspi_target(
            Atspi,
            desktop,
            session=session,
            params=params,
            operation=operation,
        )
        if operation == "press":
            self._press_atspi_element(element, target=target, requested=params)
        elif operation == "focus":
            _focus_atspi_element(Atspi, element, target=target)
        else:
            value = params.get("value", params.get("text"))
            if value is None:
                raise PortalError("COMPUTER_USE_TEXT_REQUIRED", "set_value requires value or text.")
            self._set_atspi_value(element, value)

        return {
            "session_id": session.session_id,
            "context_id": session.context_id,
            "operation": operation,
            "target": _atspi_node_metadata(
                Atspi,
                element,
                path=list(target.get("path", [])),
                session=session,
            ),
            **({"focus_verified": True} if operation == "focus" else {}),
        }

    def move(self, params: dict[str, Any]) -> dict[str, Any]:
        session = self._require_session(params)
        x = float(params.get("x"))
        y = float(params.get("y"))
        self._remote_desktop.NotifyPointerMotionAbsolute(
            dbus.ObjectPath(session.session_handle),
            _dbus_dict({}),
            dbus.UInt32(session.stream_id),
            dbus.Double(session.width * x),
            dbus.Double(session.height * y),
        )
        return {
            "stream_id": session.stream_id,
            "x": x,
            "y": y,
            "pixel_x": session.width * x,
            "pixel_y": session.height * y,
        }

    def click(self, params: dict[str, Any]) -> dict[str, Any]:
        session = self._require_session(params)
        button_name = str(params.get("button") or "left").strip().lower()
        count = max(1, int(params.get("count") or 1))
        button_code = {
            "left": BTN_LEFT,
            "right": BTN_RIGHT,
            "middle": BTN_MIDDLE,
        }.get(button_name)
        if button_code is None:
            raise PortalError("COMPUTER_USE_BAD_BUTTON", "button must be left, right, or middle")
        if "x" in params and "y" in params:
            self.move(params)
        for _ in range(count):
            self._remote_desktop.NotifyPointerButton(
                dbus.ObjectPath(session.session_handle),
                _dbus_dict({}),
                dbus.Int32(button_code),
                dbus.UInt32(1),
            )
            self._remote_desktop.NotifyPointerButton(
                dbus.ObjectPath(session.session_handle),
                _dbus_dict({}),
                dbus.Int32(button_code),
                dbus.UInt32(0),
            )
        return {"button": button_name, "count": count, "session_id": session.session_id}

    def scroll(self, params: dict[str, Any]) -> dict[str, Any]:
        session = self._require_session(params)
        dx = int(params.get("dx") or 0)
        dy = int(params.get("dy") or 0)
        if dy:
            self._remote_desktop.NotifyPointerAxisDiscrete(
                dbus.ObjectPath(session.session_handle),
                _dbus_dict({}),
                dbus.UInt32(0),
                dbus.Int32(dy),
            )
        if dx:
            self._remote_desktop.NotifyPointerAxisDiscrete(
                dbus.ObjectPath(session.session_handle),
                _dbus_dict({}),
                dbus.UInt32(1),
                dbus.Int32(dx),
            )
        self._remote_desktop.NotifyPointerAxis(
            dbus.ObjectPath(session.session_handle),
            _dbus_dict({"finish": True}),
            dbus.Double(float(dx)),
            dbus.Double(float(dy)),
        )
        return {"dx": dx, "dy": dy, "session_id": session.session_id}

    def key(self, params: dict[str, Any]) -> dict[str, Any]:
        session = self._require_session(params)
        keys = params.get("keys")
        if not isinstance(keys, list) or not keys:
            raise PortalError("COMPUTER_USE_KEYS_REQUIRED", "key requires a non-empty keys list")
        normalized = [self._keyboard_event(name) for name in keys]
        for kind, code in normalized:
            self._notify_keyboard(session, kind=kind, code=code, state=1)
        for kind, code in reversed(normalized):
            self._notify_keyboard(session, kind=kind, code=code, state=0)
        return {"keys": keys, "session_id": session.session_id}

    def type_text(self, params: dict[str, Any]) -> dict[str, Any]:
        session = self._require_session(params)
        text = str(params.get("text") or "")
        submit = bool(params.get("submit"))
        if not text:
            raise PortalError("COMPUTER_USE_TEXT_REQUIRED", "type requires text")
        window_id = str(params.get("window_id") or "").strip()
        if not window_id:
            raise PortalError(
                "COMPUTER_USE_WINDOW_REQUIRED",
                "Linux type requires window_id from list_windows so focus can be verified before injection.",
            )
        Atspi = _load_atspi_module()
        element, path, window = self._resolve_atspi_window_root(
            Atspi,
            _atspi_desktop(Atspi),
            session=session,
            params={"window_id": window_id},
        )
        active, focused = _atspi_focus_state(Atspi, element)
        if not (active or focused):
            raise PortalError(
                "COMPUTER_USE_TARGET_NOT_FOCUSED",
                "Refusing keyboard injection because the requested Linux window is not active or focused.",
            )
        verified_window_id = _atspi_window_id(window, path=path)
        for character in text:
            keysym = self._keysym(character)
            self._remote_desktop.NotifyKeyboardKeysym(
                dbus.ObjectPath(session.session_handle),
                _dbus_dict({}),
                dbus.Int32(keysym),
                dbus.UInt32(1),
            )
            self._remote_desktop.NotifyKeyboardKeysym(
                dbus.ObjectPath(session.session_handle),
                _dbus_dict({}),
                dbus.Int32(keysym),
                dbus.UInt32(0),
            )
        if submit:
            enter_keysym = self._keysym("enter")
            self._remote_desktop.NotifyKeyboardKeysym(
                dbus.ObjectPath(session.session_handle),
                _dbus_dict({}),
                dbus.Int32(enter_keysym),
                dbus.UInt32(1),
            )
            self._remote_desktop.NotifyKeyboardKeysym(
                dbus.ObjectPath(session.session_handle),
                _dbus_dict({}),
                dbus.Int32(enter_keysym),
                dbus.UInt32(0),
            )
        return {
            "text": text,
            "submitted": submit,
            "session_id": session.session_id,
            "window_id": verified_window_id,
            "focus_verified": True,
            "input_scope": "verified_foreground_window",
        }

    def stop_session(self, params: dict[str, Any]) -> dict[str, Any]:
        del params
        self._close_session()
        return {"active": False, "status": "stopped", "session_id": ""}

    def _create_session(self) -> str:
        response = self._call_request(
            self._remote_desktop.CreateSession,
            {
                "session_handle_token": self._token("session"),
            },
        )
        session_handle = str(response.get("session_handle") or "").strip()
        if not session_handle:
            raise PortalError("COMPUTER_USE_SESSION_HANDLE_MISSING", "Portal session did not return a session handle.")
        return session_handle

    def _select_devices(
        self,
        session_handle: str,
        *,
        trust_mode: str,
        restore_token: str,
        timeout: float | None,
        allow: bool,
    ) -> None:
        options: dict[str, Any] = {
            "types": _dbus_u32(DEVICE_TYPE_KEYBOARD | DEVICE_TYPE_POINTER),
            "persist_mode": _dbus_u32(PERSIST_MODE_NONE),
        }
        if trust_mode in {"persistent", "allow"}:
            options["persist_mode"] = _dbus_u32(PERSIST_MODE_EXPLICIT)
            if restore_token:
                options["restore_token"] = restore_token
        self._call_request(
            self._remote_desktop.SelectDevices,
            options,
            dbus.ObjectPath(session_handle),
            timeout=timeout,
            allow=allow,
        )

    def _select_sources(self, session_handle: str, *, timeout: float | None, allow: bool) -> None:
        self._call_request(
            self._screencast.SelectSources,
            {
                "types": _dbus_u32(SOURCE_TYPE_MONITOR),
                "multiple": False,
                "cursor_mode": _dbus_u32(CURSOR_MODE_EMBEDDED),
            },
            dbus.ObjectPath(session_handle),
            timeout=timeout,
            allow=allow,
        )

    def _start_remote_desktop(
        self,
        session_handle: str,
        *,
        timeout: float | None,
        allow: bool,
    ) -> dict[str, Any]:
        return self._call_request(
            self._remote_desktop.Start,
            {},
            dbus.ObjectPath(session_handle),
            "",
            timeout=timeout,
            allow=allow,
        )

    def _open_pipewire_remote(self, session_handle: str) -> int:
        fd = self._screencast.OpenPipeWireRemote(dbus.ObjectPath(session_handle), _dbus_dict({}))
        if hasattr(fd, "take"):
            return int(fd.take())
        return int(fd)

    def _call_request(
        self,
        method: Any,
        options: dict[str, Any],
        *args: Any,
        timeout: float | None = None,
        allow: bool = False,
    ) -> dict[str, Any]:
        token = self._token("req")
        request_path = self._request_path(token)
        loop = GLib.MainLoop()
        outcome: dict[str, Any] = {}
        timeout_source: int | None = None
        active_request_path = request_path

        def on_response(response: Any, results: Any) -> None:
            outcome["response"] = int(response)
            outcome["results"] = _python_value(results)
            if loop.is_running():
                loop.quit()

        self._bus.add_signal_receiver(
            on_response,
            dbus_interface=PORTAL_REQUEST_IFACE,
            signal_name="Response",
            path=request_path,
        )

        payload = dict(options)
        payload["handle_token"] = token
        handle = method(*args, _dbus_dict(payload))
        active_request_path = str(handle)
        if active_request_path != request_path:
            self._bus.remove_signal_receiver(
                on_response,
                dbus_interface=PORTAL_REQUEST_IFACE,
                signal_name="Response",
                path=request_path,
            )
            self._bus.add_signal_receiver(
                on_response,
                dbus_interface=PORTAL_REQUEST_IFACE,
                signal_name="Response",
                path=active_request_path,
            )

        if timeout is not None and timeout > 0:
            timeout_ms = int(timeout * 1000)

            def on_timeout() -> bool:
                outcome["timeout"] = True
                self._close_request(active_request_path)
                if loop.is_running():
                    loop.quit()
                return False

            timeout_source = GLib.timeout_add(timeout_ms, on_timeout)

        try:
            loop.run()
        finally:
            if timeout_source is not None:
                GLib.source_remove(timeout_source)
            self._bus.remove_signal_receiver(
                on_response,
                dbus_interface=PORTAL_REQUEST_IFACE,
                signal_name="Response",
                path=active_request_path,
            )

        if outcome.get("timeout"):
            if allow:
                raise PortalError(
                    "COMPUTER_USE_REARM_REQUIRED",
                    "Silent restore was not available. Run /computer-use on and approve the platform permission prompt.",
                )
            raise PortalError(
                "COMPUTER_USE_REQUEST_TIMEOUT",
                "Timed out while waiting for the portal request to finish.",
            )

        response = int(outcome.get("response", 2))
        results = outcome.get("results")
        if not isinstance(results, dict):
            results = {}
        if response == 0:
            return results
        if response == 1:
            if allow:
                raise PortalError(
                    "COMPUTER_USE_REARM_REQUIRED",
                    "The stored computer-use permission is no longer valid.",
                )
            raise PortalError(
                "COMPUTER_USE_APPROVAL_REQUIRED",
                "Computer-use approval is required.",
            )
        raise PortalError("COMPUTER_USE_PORTAL_ERROR", "The portal request did not complete successfully.")

    def _require_session(self, params: dict[str, Any]) -> PortalSession:
        session = self._session
        if session is None:
            raise PortalError("COMPUTER_USE_SESSION_REQUIRED", "No computer-use session is active.")
        requested_id = str(params.get("session_id") or "").strip()
        if requested_id and requested_id != session.session_id:
            raise PortalError(
                "COMPUTER_USE_SESSION_MISMATCH",
                "The requested computer-use session is no longer active.",
            )
        return session

    def _resolve_atspi_window_root(
        self,
        Atspi: Any,
        desktop: object,
        *,
        session: PortalSession,
        params: dict[str, Any],
    ) -> tuple[object, list[int], dict[str, Any]]:
        requested_window_id = str(params.get("window_id") or "").strip()
        requested_pid = params.get("pid")
        parsed_pid, parsed_path = _parse_atspi_window_id(requested_window_id)
        if requested_pid is None and parsed_pid is not None:
            requested_pid = parsed_pid
        if parsed_path is not None and requested_window_id:
            element = _atspi_element_for_path(desktop, parsed_path)
            if element is not None:
                node = _atspi_node_metadata(Atspi, element, path=parsed_path, session=session)
                pid_matches = requested_pid is None or str(node.get("pid") or "") == str(requested_pid)
                if pid_matches:
                    return element, parsed_path, node
        for application_index in range(_atspi_child_count(desktop)):
            application = _atspi_child_at(desktop, application_index)
            if application is None:
                continue
            application_path = [application_index]
            for element, path, node in _atspi_window_candidates(
                Atspi,
                application,
                application_path=application_path,
                session=session,
            ):
                if requested_pid is not None and str(node.get("pid") or "") != str(requested_pid):
                    continue
                if requested_window_id and _atspi_window_id(node, path=path) != requested_window_id:
                    continue
                return element, path, node
        raise PortalError(
            "COMPUTER_USE_WINDOW_NOT_FOUND",
            "No matching AT-SPI top-level window was found.",
        )

    def _cache_element_indices(self, tree: dict[str, Any], *, window_id: str) -> None:
        if not hasattr(self, "_element_index_cache"):
            self._element_index_cache = {}
        self._element_index_cache.clear()
        next_index = 0

        def visit(node: dict[str, Any]) -> None:
            nonlocal next_index
            index = next_index
            next_index += 1
            node["element_index"] = index
            self._element_index_cache[index] = {
                "window_id": window_id,
                "path": list(node.get("path") or []),
                "target": dict(node),
            }
            children = node.get("children")
            if isinstance(children, list):
                for child in children:
                    if isinstance(child, dict):
                        visit(child)

        if tree:
            visit(tree)

    def _resolve_element_action_target(
        self,
        Atspi: Any,
        desktop: object,
        *,
        session: PortalSession,
        params: dict[str, Any],
        operation: str,
    ) -> tuple[object, dict[str, Any]]:
        element_index = params.get("element_index")
        if element_index is not None:
            try:
                index = int(element_index)
            except (TypeError, ValueError) as exc:
                raise PortalError(
                    "COMPUTER_USE_BAD_ELEMENT_INDEX",
                    "element_index must be an integer from the latest get_window_state.",
                ) from exc
            if not hasattr(self, "_element_index_cache"):
                self._element_index_cache = {}
            cached = self._element_index_cache.get(index)
            if cached is None:
                raise PortalError(
                    "COMPUTER_USE_ELEMENT_INDEX_STALE",
                    "element_index was not found. Call get_window_state and retry with a fresh index.",
                )
            requested_window_id = str(params.get("window_id") or "").strip()
            cached_window_id = str(cached.get("window_id") or "").strip()
            if requested_window_id and requested_window_id != cached_window_id:
                raise PortalError(
                    "COMPUTER_USE_ELEMENT_WINDOW_MISMATCH",
                    "element_index belongs to a different cached window_id.",
                )
            path = list(cached.get("path") or [])
            element = _atspi_element_for_path(desktop, path)
            if element is None:
                raise PortalError(
                    "COMPUTER_USE_AX_TARGET_NOT_FOUND",
                    "No AT-SPI element exists at the cached path. Call get_window_state and retry.",
                )
            node = _atspi_node_metadata(Atspi, element, path=path, session=session)
            node["element_index"] = index
            return element, node
        if (
            operation == "focus"
            and params.get("path") is None
            and not params.get("target")
            and (params.get("window_id") or params.get("pid") is not None)
        ):
            element, path, window = self._resolve_atspi_window_root(
                Atspi,
                desktop,
                session=session,
                params=params,
            )
            target = _atspi_node_metadata(Atspi, element, path=path, session=session)
            target["window_id"] = _atspi_window_id(window, path=path)
            return element, target
        return self._resolve_atspi_target(
            Atspi,
            desktop,
            session=session,
            params=params,
            operation=operation,
        )

    def _background_unavailable(
        self,
        *,
        session: PortalSession,
        target: dict[str, Any],
        operation: str,
        reason: str,
        requested_dispatch: str = "background",
    ) -> dict[str, Any]:
        return {
            "session_id": session.session_id,
            "context_id": session.context_id,
            "operation": operation,
            "target": target,
            "requested_dispatch": requested_dispatch,
            "actual_dispatch": "none",
            "background_unavailable": True,
            "reason": reason,
        }

    def _try_background_atspi_action(
        self,
        Atspi: Any,
        element: object,
        *,
        target: dict[str, Any],
        operation: str,
        params: dict[str, Any],
        session: PortalSession,
        requested_dispatch: str,
    ) -> dict[str, Any]:
        if operation == "focus":
            return self._background_unavailable(
                session=session,
                target=target,
                operation=operation,
                reason="AT-SPI focus changes active UI state and is treated as foreground dispatch.",
                requested_dispatch=requested_dispatch,
            )
        if operation not in {"press", "set_value"}:
            return self._background_unavailable(
                session=session,
                target=target,
                operation=operation,
                reason=f"operation {operation!r} is not supported for background dispatch.",
                requested_dispatch=requested_dispatch,
            )
        if operation == "set_value" and _bool_param(params.get("submit"), default=False):
            return self._background_unavailable(
                session=session,
                target=target,
                operation=operation,
                reason="submit requires keyboard input and cannot be guaranteed in background.",
                requested_dispatch=requested_dispatch,
            )
        try:
            result = self._perform_atspi_element_action(
                Atspi,
                element,
                target=target,
                operation=operation,
                params=params,
                session=session,
            )
        except PortalError as exc:
            return self._background_unavailable(
                session=session,
                target=target,
                operation=operation,
                reason=str(exc),
                requested_dispatch=requested_dispatch,
            )
        result["requested_dispatch"] = requested_dispatch
        result["actual_dispatch"] = "background"
        result["background_unavailable"] = False
        return result

    def _perform_atspi_element_action(
        self,
        Atspi: Any,
        element: object,
        *,
        target: dict[str, Any],
        operation: str,
        params: dict[str, Any],
        session: PortalSession,
    ) -> dict[str, Any]:
        focus_verified = False
        if operation == "press":
            self._press_atspi_element(element, target=target, requested=params)
        elif operation == "focus":
            _focus_atspi_element(Atspi, element, target=target)
            focus_verified = True
        elif operation == "set_value":
            value = params.get("value", params.get("text"))
            if value is None:
                raise PortalError("COMPUTER_USE_TEXT_REQUIRED", "set_value requires value or text.")
            self._set_atspi_value(element, value)
        else:
            raise PortalError(
                "COMPUTER_USE_BAD_AX_ACTION",
                "element_action operation must be press, focus, or set_value.",
            )
        return {
            "session_id": session.session_id,
            "context_id": session.context_id,
            "operation": operation,
            "target": target,
            **({"focus_verified": True} if focus_verified else {}),
        }

    def _keysym(self, value: str) -> int:
        normalized = _KEY_ALIASES.get(value.strip().lower(), value)
        if len(normalized) == 1:
            keysym = int(Gdk.unicode_to_keyval(ord(normalized)))
        else:
            keysym = int(Gdk.keyval_from_name(normalized))
        if keysym <= 0:
            raise PortalError("COMPUTER_USE_BAD_KEY", f"Unsupported key: {value}")
        return keysym

    def _keyboard_event(self, value: str) -> tuple[str, int]:
        keycode = self._evdev_keycode(value)
        if keycode is not None:
            return "keycode", keycode
        return "keysym", self._keysym(value)

    def _evdev_keycode(self, value: str) -> int | None:
        normalized = value.strip().lower()
        if not normalized:
            return None
        if normalized in _EVDEV_KEY_ALIASES:
            return _EVDEV_KEY_ALIASES[normalized]
        if normalized in _EVDEV_FUNCTION_KEYCODES:
            return _EVDEV_FUNCTION_KEYCODES[normalized]
        if len(normalized) == 1:
            return _EVDEV_CHAR_KEYCODES.get(normalized)
        return None

    def _notify_keyboard(self, session: PortalSession, *, kind: str, code: int, state: int) -> None:
        if kind == "keycode":
            self._remote_desktop.NotifyKeyboardKeycode(
                dbus.ObjectPath(session.session_handle),
                _dbus_dict({}),
                dbus.Int32(code),
                dbus.UInt32(state),
            )
            return
        self._remote_desktop.NotifyKeyboardKeysym(
            dbus.ObjectPath(session.session_handle),
            _dbus_dict({}),
            dbus.Int32(code),
            dbus.UInt32(state),
        )

    def _resolve_atspi_target(
        self,
        Atspi: Any,
        desktop: object,
        *,
        session: PortalSession,
        params: dict[str, Any],
        operation: str,
    ) -> tuple[object, dict[str, Any]]:
        target = params.get("target") if isinstance(params.get("target"), dict) else {}
        assert isinstance(target, dict)
        path = _parse_ax_path(params.get("path"))
        if path is None:
            path = _parse_ax_path(target.get("path"))
        if path is not None:
            element = _atspi_element_for_path(desktop, path)
            if element is None:
                raise PortalError(
                    "COMPUTER_USE_AX_TARGET_NOT_FOUND",
                    f"No AT-SPI element exists at path={path}. Take a fresh ax_snapshot.",
                )
            return element, _atspi_node_metadata(Atspi, element, path=path, session=session)

        semantic_target = {str(key): value for key, value in target.items() if key != "path"}
        if not semantic_target:
            raise PortalError(
                "COMPUTER_USE_AX_TARGET_REQUIRED",
                "ax_action requires path or semantic target fields.",
            )
        matches = _find_atspi_matches(
            Atspi,
            desktop,
            session=session,
            target=semantic_target,
            operation=operation,
        )
        if not matches:
            raise PortalError(
                "COMPUTER_USE_AX_TARGET_NOT_FOUND",
                "No AT-SPI element matched the requested target. Take a fresh ax_snapshot.",
            )
        matches.sort(key=lambda item: item[0], reverse=True)
        top_score = matches[0][0]
        top_matches = [item for item in matches if item[0] == top_score]
        if len(top_matches) > 1:
            raise PortalError(
                "COMPUTER_USE_AX_TARGET_AMBIGUOUS",
                "Multiple AT-SPI elements matched: "
                f"{_target_summary([item[2] for item in top_matches])}. "
                "Narrow the target or use a path from a fresh ax_snapshot.",
            )
        _, element, node = matches[0]
        return element, node

    def _press_atspi_element(
        self,
        element: object,
        *,
        target: dict[str, Any],
        requested: dict[str, Any],
    ) -> None:
        role = str(target.get("role") or "").strip().casefold()
        if role in _AT_SPI_WINDOW_ACTIVATION_ROLES:
            raise PortalError(
                "COMPUTER_USE_WINDOW_ACTIVATION_REQUIRED",
                "Window/application nodes cannot be pressed; use foreground focus and require focus verification.",
            )
        action_name = ""
        target_action = target.get("action")
        if isinstance(target_action, str):
            action_name = target_action
        requested_action = requested.get("action_name") or requested.get("ax_action_name")
        if isinstance(requested_action, str) and requested_action.strip():
            action_name = requested_action
        action_count = max(0, _safe_int(_safe_call(element, "get_n_actions"), default=0))
        if action_count <= 0:
            raise PortalError("COMPUTER_USE_AX_ACTION_UNAVAILABLE", "The target exposes no AT-SPI actions.")

        action_names = [
            _clean_text(_safe_call(element, "get_action_name", index), limit=80).casefold()
            for index in range(action_count)
        ]
        preferred_names: list[str] = []
        if action_name.strip():
            preferred_names.append(action_name.strip().casefold())
        preferred_names.extend(_AT_SPI_PRESS_ACTION_NAMES)
        chosen_index = None
        for preferred in preferred_names:
            if preferred in action_names:
                chosen_index = action_names.index(preferred)
                break
        if chosen_index is None:
            available = ", ".join(name for name in action_names if name) or "none"
            raise PortalError(
                "COMPUTER_USE_AX_ACTION_UNAVAILABLE",
                f"The target exposes no recognized press action; available actions: {available}.",
            )
        if _safe_call(element, "do_action", chosen_index) is False:
            raise PortalError("COMPUTER_USE_AX_ACTION_FAILED", "AT-SPI press action failed.")

    def _set_atspi_value(self, element: object, value: object) -> None:
        text = str(value)
        set_text_result = _safe_call(element, "set_text_contents", text)
        if set_text_result is not None and set_text_result is not False:
            return
        try:
            numeric_value = float(value)
        except (TypeError, ValueError):
            numeric_value = None
        if numeric_value is not None:
            set_value_result = _safe_call(element, "set_current_value", numeric_value)
            if set_value_result is not None and set_value_result is not False:
                return
        raise PortalError("COMPUTER_USE_AX_ACTION_FAILED", "AT-SPI set_value action failed.")

    def _request_path(self, token: str) -> str:
        sender = self._bus_name.lstrip(":").replace(".", "_")
        return f"/org/freedesktop/portal/desktop/request/{sender}/{token}"

    def _token(self, prefix: str) -> str:
        return f"a0_{prefix}_{uuid.uuid4().hex}"

    def _close_request(self, request_path: str) -> None:
        try:
            request = self._bus.get_object(PORTAL_SERVICE, request_path)
            dbus.Interface(request, PORTAL_REQUEST_IFACE).Close()
        except Exception:
            return

    def _close_portal_session(self, session_handle: str) -> None:
        try:
            session = self._bus.get_object(PORTAL_SERVICE, session_handle)
            dbus.Interface(session, PORTAL_SESSION_IFACE).Close()
        except Exception:
            return

    def _close_session(self) -> None:
        self._tag_target = None
        session = self._session
        self._session = None
        if session is None:
            return
        session.capture_stream.close()
        self._close_portal_session(session.session_handle)

    def _session_payload(self, session: PortalSession) -> dict[str, Any]:
        return {
            "active": True,
            "context_id": session.context_id,
            "trust_mode": session.trust_mode,
            "session_id": session.session_id,
            "session_handle": session.session_handle,
            "stream_id": session.stream_id,
            "width": session.width,
            "height": session.height,
            "devices": session.devices,
            "restore_token": session.restore_token,
            **_backend_contract_metadata(),
        }


def serve_stdio() -> int:
    helper = PortalComputerUseHelper()
    try:
        for raw_line in sys.stdin:
            line = raw_line.strip()
            if not line:
                continue
            request_id = ""
            try:
                request = json.loads(line)
                action = str(request.get("action") or "").strip()
                request_id = str(request.get("request_id") or "")
                if action == "shutdown":
                    response = {"request_id": request_id, "ok": True, "result": {"shutdown": True}}
                    sys.stdout.write(json.dumps(response) + "\n")
                    sys.stdout.flush()
                    break
                if not isinstance(request, dict):
                    raise PortalError("COMPUTER_USE_BAD_REQUEST", "Invalid helper request.")
                result = helper.dispatch(action, request)
                response = {"request_id": request_id, "ok": True, "result": result}
            except PortalError as exc:
                response = {
                    "request_id": request_id,
                    "ok": False,
                    "error": str(exc),
                    "code": exc.code,
                }
            except Exception as exc:
                response = {
                    "request_id": request_id,
                    "ok": False,
                    "error": str(exc),
                    "code": "COMPUTER_USE_ERROR",
                }
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()
    finally:
        helper.stop_session({})
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stdio", action="store_true")
    args = parser.parse_args(argv)
    if not args.stdio:
        parser.error("Use --stdio to run the computer-use helper protocol.")
    return serve_stdio()


if __name__ == "__main__":
    raise SystemExit(main())
