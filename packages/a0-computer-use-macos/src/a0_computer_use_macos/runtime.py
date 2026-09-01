from __future__ import annotations

import argparse
import base64
import contextlib
import json
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from agent_zero_cli.computer_use_backend import (
    COMPUTER_USE_CONTRACT_VERSION,
    computer_use_capabilities_from_features,
)

if __package__ in {None, ""}:
    package_dir = Path(__file__).resolve().parent
    parent_dir = package_dir.parent
    if str(parent_dir) not in sys.path:
        sys.path.insert(0, str(parent_dir))

from a0_computer_use_macos.detection import (
    macos_backend_support_reason,
    macos_backend_supported,
)
from a0_computer_use_macos.shared import (
    CAPTURE_DEBUG_DIR_ENV,
    MACOS_BACKEND_FEATURES,
    MACOS_BACKEND_FAMILY,
    MACOS_BACKEND_ID,
    MACOS_TRUST_MODES,
    STATE_DIR_ENV,
    TrustModePolicy,
    coerce_bool,
    coerce_int,
    normalize_action_payload,
    normalize_context_id,
    normalize_dispatch,
    normalize_restore_token,
    resolve_trust_mode_policy,
)

_DEBUG_ENV = "A0_COMPUTER_USE_DEBUG"
_DEBUG_LOG_ENV = "A0_COMPUTER_USE_DEBUG_LOG"
_AX_DEFAULT_MAX_DEPTH = 4
_AX_DEFAULT_MAX_NODES = 200
_AX_HARD_MAX_DEPTH = 8
_AX_HARD_MAX_NODES = 500
_AX_TEXT_LIMIT = 240
_TAG_TEXT_WINDOW_CHARS = 4096
_TAG_QUERY_MAX_CHARS = 2048
_TAG_REPLACEMENT_MAX_CHARS = 16384
_TAG_SCREENSHOT_MAX_BYTES = 16 * 1024 * 1024
_TAG_TARGET_TTL_SECONDS = 15 * 60
_TAG_PROFILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_MACOS_ACCESSIBILITY_MANUAL_APPROVAL = (
    "If no prompt appears, open System Settings > Privacy & Security > Accessibility "
    "and enable the app running a0, such as Terminal, then run /computer-use on again."
)
_MACOS_SCREEN_RECORDING_MANUAL_APPROVAL = (
    "If no prompt appears, open System Settings > Privacy & Security > Screen Recording "
    "and enable the app running a0, such as Terminal, then run /computer-use on again."
)


def _backend_contract_metadata() -> dict[str, Any]:
    return {
        "contract_version": COMPUTER_USE_CONTRACT_VERSION,
        "capabilities": computer_use_capabilities_from_features(
            backend_id=MACOS_BACKEND_ID,
            backend_family=MACOS_BACKEND_FAMILY,
            features=MACOS_BACKEND_FEATURES,
        ),
    }

_MODIFIER_KEY_SPECS = {
    "cmd": (55, "command", "kCGEventFlagMaskCommand"),
    "command": (55, "command", "kCGEventFlagMaskCommand"),
    "super": (55, "command", "kCGEventFlagMaskCommand"),
    "shift": (56, "shift", "kCGEventFlagMaskShift"),
    "alt": (58, "alternate", "kCGEventFlagMaskAlternate"),
    "option": (58, "alternate", "kCGEventFlagMaskAlternate"),
    "ctrl": (59, "control", "kCGEventFlagMaskControl"),
    "control": (59, "control", "kCGEventFlagMaskControl"),
}

_SPECIAL_KEYCODES = {
    "backspace": 51,
    "delete": 51,
    "down": 125,
    "end": 119,
    "enter": 36,
    "esc": 53,
    "escape": 53,
    "forwarddelete": 117,
    "home": 115,
    "left": 123,
    "pagedown": 121,
    "pageup": 116,
    "pgdn": 121,
    "pgup": 116,
    "return": 36,
    "right": 124,
    "space": 49,
    "tab": 48,
    "up": 126,
}

_CHAR_KEYCODES = {
    "a": 0,
    "b": 11,
    "c": 8,
    "d": 2,
    "e": 14,
    "f": 3,
    "g": 5,
    "h": 4,
    "i": 34,
    "j": 38,
    "k": 40,
    "l": 37,
    "m": 46,
    "n": 45,
    "o": 31,
    "p": 35,
    "q": 12,
    "r": 15,
    "s": 1,
    "t": 17,
    "u": 32,
    "v": 9,
    "w": 13,
    "x": 7,
    "y": 16,
    "z": 6,
    "0": 29,
    "1": 18,
    "2": 19,
    "3": 20,
    "4": 21,
    "5": 23,
    "6": 22,
    "7": 26,
    "8": 28,
    "9": 25,
    "-": 27,
    "=": 24,
    "[": 33,
    "]": 30,
    "\\": 42,
    ";": 41,
    "'": 39,
    ",": 43,
    ".": 47,
    "/": 44,
    "`": 50,
    " ": 49,
}

_SHIFTED_CHAR_ALIASES = {
    "!": "1",
    "@": "2",
    "#": "3",
    "$": "4",
    "%": "5",
    "^": "6",
    "&": "7",
    "*": "8",
    "(": "9",
    ")": "0",
    "_": "-",
    "+": "=",
    "{": "[",
    "}": "]",
    "|": "\\",
    ":": ";",
    '"': "'",
    "<": ",",
    ">": ".",
    "?": "/",
    "~": "`",
}


def _env_flag(name: str) -> bool:
    value = str(os.environ.get(name, "")).strip().lower()
    return value in {"1", "true", "yes", "on", "debug"}


def _resolve_debug_log_path() -> Path | None:
    configured = str(os.environ.get(_DEBUG_LOG_ENV, "")).strip()
    if not configured:
        return None
    return Path(configured).expanduser()


def _debug_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + f".{int(time.time_ns() % 1_000_000_000):09d}Z"


def _debug_value(value: object) -> object:
    if isinstance(value, str):
        text = value.replace("\n", "\\n")
        if len(text) > 240:
            return text[:237] + "..."
        return text
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (list, tuple)):
        return [_debug_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _debug_value(item) for key, item in value.items()}
    return value


def _emit_debug(event: str, **fields: object) -> None:
    if not _env_flag(_DEBUG_ENV):
        return
    line = f"[a0 macos runtime] {_debug_timestamp()} {event}"
    if fields:
        formatted = " ".join(
            f"{key}={json.dumps(_debug_value(value), ensure_ascii=True, sort_keys=True)}"
            for key, value in sorted(fields.items())
        )
        line = f"{line} {formatted}"
    log_path = _resolve_debug_log_path()
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    sys.stderr.write(line + "\n")
    sys.stderr.flush()


class MacOSComputerUseError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        result: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.result = result


class MacOSDesktopDriver(Protocol):
    def screen_size(self) -> tuple[int, int]:
        ...

    def capture_png(self) -> tuple[bytes, int, int]:
        ...

    def move(self, x: float, y: float) -> None:
        ...

    def click(self, x: float, y: float, *, button: str, count: int) -> None:
        ...

    def scroll(self, dx: int, dy: int) -> None:
        ...

    def key(self, keys: list[str]) -> None:
        ...

    def type_text(self, text: str, *, submit: bool) -> None:
        ...


@dataclass
class MacOSSession:
    context_id: str
    session_id: str
    trust_mode: str
    restore_token: str = ""
    active: bool = False
    width: int = 0
    height: int = 0
    updated_at: float = field(default_factory=time.time)

    def to_payload(self, *, reused: bool = False) -> dict[str, Any]:
        payload = {
            "context_id": self.context_id,
            "session_id": self.session_id,
            "trust_mode": self.trust_mode,
            "active": self.active,
            "status": "active" if self.active else "stopped",
            "width": self.width,
            "height": self.height,
            "backend_id": MACOS_BACKEND_ID,
            "backend_family": MACOS_BACKEND_FAMILY,
            "features": list(MACOS_BACKEND_FEATURES),
            "supported": macos_backend_supported(),
            "support_reason": macos_backend_support_reason(),
        }
        payload.update(_backend_contract_metadata())
        if self.restore_token:
            payload["restore_token"] = self.restore_token
        if reused:
            payload["reused"] = True
        return payload

    def to_record(self) -> dict[str, Any]:
        return {
            "context_id": self.context_id,
            "session_id": self.session_id,
            "trust_mode": self.trust_mode,
            "restore_token": self.restore_token,
            "active": self.active,
            "width": self.width,
            "height": self.height,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_record(cls, payload: dict[str, Any]) -> "MacOSSession":
        return cls(
            context_id=str(payload.get("context_id", "") or "default"),
            session_id=str(payload.get("session_id", "") or ""),
            trust_mode=str(payload.get("trust_mode", "") or "persistent").strip().lower() or "persistent",
            restore_token=normalize_restore_token(payload.get("restore_token", "")),
            active=bool(payload.get("active")),
            width=coerce_int(payload.get("width"), name="width", default=0),
            height=coerce_int(payload.get("height"), name="height", default=0),
            updated_at=float(payload.get("updated_at") or time.time()),
        )


class MacOSSessionStore:
    def __init__(self, state_dir: str | os.PathLike[str] | None = None) -> None:
        configured = str(state_dir or os.environ.get(STATE_DIR_ENV, "")).strip()
        if configured:
            self.state_dir = Path(configured)
        else:
            self.state_dir = Path.home() / ".a0" / "computer-use-macos"
        self.state_file = self.state_dir / "sessions.json"

    def _read_records(self) -> dict[str, dict[str, Any]]:
        try:
            payload = json.loads(self.state_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(payload, dict):
            return {}
        records: dict[str, dict[str, Any]] = {}
        for context_id, record in payload.items():
            if isinstance(context_id, str) and isinstance(record, dict):
                records[context_id] = record
        return records

    def _write_records(self, records: dict[str, dict[str, Any]]) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(records, indent=2, sort_keys=True), encoding="utf-8")

    def get(self, context_id: str) -> MacOSSession | None:
        record = self._read_records().get(context_id)
        if record is None:
            return None
        return MacOSSession.from_record(record)

    def put(self, session: MacOSSession) -> None:
        records = self._read_records()
        records[session.context_id] = session.to_record()
        self._write_records(records)

    def clear(self, context_id: str) -> None:
        records = self._read_records()
        if context_id not in records:
            return
        records.pop(context_id, None)
        if records:
            self._write_records(records)
        else:
            try:
                self.state_file.unlink()
            except OSError:
                pass


def _default_capture_debug_dir() -> Path | None:
    configured = str(os.environ.get(CAPTURE_DEBUG_DIR_ENV, "")).strip()
    if configured:
        return Path(configured)
    return None


def _load_quartz_module() -> Any:
    try:
        import Quartz  # type: ignore
    except Exception as exc:
        raise MacOSComputerUseError(
            "COMPUTER_USE_UNSUPPORTED",
            "PyObjC Quartz bindings are required for macOS computer use.",
        ) from exc
    return Quartz


def _load_appkit_module() -> Any:
    try:
        import AppKit  # type: ignore
    except Exception as exc:
        raise MacOSComputerUseError(
            "COMPUTER_USE_UNSUPPORTED",
            "PyObjC AppKit bindings are required for macOS screen capture.",
        ) from exc
    return AppKit


def _load_accessibility_module() -> Any:
    try:
        import ApplicationServices  # type: ignore
    except Exception as exc:
        raise MacOSComputerUseError(
            "COMPUTER_USE_UNSUPPORTED",
            "PyObjC ApplicationServices bindings are required for macOS Accessibility checks.",
        ) from exc
    return ApplicationServices


def _png_dimensions(png_bytes: bytes) -> tuple[int, int]:
    if len(png_bytes) < 24 or not png_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        raise MacOSComputerUseError(
            "COMPUTER_USE_CAPTURE_UNAVAILABLE",
            "macOS screenshot helper returned invalid PNG data.",
        )
    width, height = struct.unpack(">II", png_bytes[16:24])
    return int(width), int(height)


def _bounded_text(value: Any, *, limit: int = _AX_TEXT_LIMIT) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def _ax_constant(accessibility: Any, name: str, fallback: str) -> Any:
    return getattr(accessibility, name, fallback)


def _ax_result_value(result: Any) -> tuple[int, Any]:
    if isinstance(result, tuple):
        if not result:
            return 1, None
        try:
            error_code = int(result[0])
        except Exception:
            return 0, result
        value = result[1] if len(result) > 1 else None
        return error_code, value
    return 0, result


def _ax_copy_attribute(accessibility: Any, element: Any, name: str, fallback: str) -> Any:
    copy_attribute = getattr(accessibility, "AXUIElementCopyAttributeValue")
    attribute = _ax_constant(accessibility, name, fallback)
    try:
        result = copy_attribute(element, attribute, None)
    except TypeError:
        result = copy_attribute(element, attribute)
    except Exception:
        return None
    error_code, value = _ax_result_value(result)
    if error_code != 0:
        return None
    return value


def _ax_copy_actions(accessibility: Any, element: Any) -> list[str]:
    copy_actions = getattr(accessibility, "AXUIElementCopyActionNames", None)
    if copy_actions is None:
        return []
    try:
        result = copy_actions(element, None)
    except TypeError:
        result = copy_actions(element)
    except Exception:
        return []
    error_code, value = _ax_result_value(result)
    if error_code != 0:
        return []
    return [_bounded_text(item, limit=80) for item in _ax_iterable(value) if str(item or "").strip()]


def _ax_iterable(value: Any) -> list[Any]:
    if value is None or isinstance(value, (str, bytes, dict)):
        return []
    try:
        return list(value)
    except TypeError:
        return []


def _ax_point(value: Any) -> tuple[float, float] | None:
    if value is None:
        return None
    x = getattr(value, "x", None)
    y = getattr(value, "y", None)
    if x is not None and y is not None:
        return float(x), float(y)
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        try:
            return float(value[0]), float(value[1])
        except (TypeError, ValueError):
            return None
    return None


def _ax_size(value: Any) -> tuple[float, float] | None:
    if value is None:
        return None
    width = getattr(value, "width", None)
    height = getattr(value, "height", None)
    if width is not None and height is not None:
        return float(width), float(height)
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        try:
            return float(value[0]), float(value[1])
        except (TypeError, ValueError):
            return None
    return None


def _ax_scalar(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _bounded_text(value)
    if isinstance(value, bytes):
        return _bounded_text(value.decode("utf-8", errors="replace"))
    return _bounded_text(value)


def _normalize_ax_path(value: Any) -> list[int]:
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                loaded = json.loads(text)
            except json.JSONDecodeError:
                loaded = []
            return _normalize_ax_path(loaded)
        parts = [part for part in text.replace(".", "/").split("/") if part.strip()]
        return [int(part) for part in parts]
    if isinstance(value, (list, tuple)):
        return [int(part) for part in value]
    raise ValueError("AX path must be a list of integers or a slash-delimited string.")


def _utf16_length(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def _cg_window_bounds(value: Any) -> tuple[float, float, float, float] | None:
    if not isinstance(value, dict):
        return None
    try:
        bounds = (
            float(value.get("X", value.get("x"))),
            float(value.get("Y", value.get("y"))),
            float(value.get("Width", value.get("width"))),
            float(value.get("Height", value.get("height"))),
        )
    except (TypeError, ValueError):
        return None
    return bounds if all(math.isfinite(item) for item in bounds) else None


@dataclass(frozen=True)
class _ResolvedKey:
    keycode: int
    requires_shift: bool = False
    modifier_flag_name: str = ""


class _MacOSDesktopAutomation:
    def __init__(self) -> None:
        self._quartz = _load_quartz_module()
        self._event_source = None
        self.last_capture_strategy = ""
        self.last_click_strategy = ""

    def screen_size(self) -> tuple[int, int]:
        _png_bytes, width, height = self.capture_png()
        return width, height

    def capture_png(self) -> tuple[bytes, int, int]:
        try:
            return self._capture_png_coregraphics()
        except MacOSComputerUseError as exc:
            _emit_debug(
                "driver.capture_png.coregraphics_failed",
                code=exc.code,
                error=str(exc),
            )
            return self._capture_png_screencapture(fallback_error=exc)

    def _capture_png_coregraphics(self) -> tuple[bytes, int, int]:
        quartz = self._quartz
        display_id = quartz.CGMainDisplayID()
        image = quartz.CGDisplayCreateImage(display_id)
        if image is None:
            raise MacOSComputerUseError(
                "COMPUTER_USE_CAPTURE_UNAVAILABLE",
                "CoreGraphics did not return a display image. macOS Screen Recording permission may be required.",
            )
        png_bytes, width, height = self._encode_cgimage_png(image)
        self.last_capture_strategy = "coregraphics"
        _emit_debug("driver.capture_png.coregraphics_ok", width=width, height=height, bytes=len(png_bytes))
        return png_bytes, width, height

    def capture_window_png(
        self,
        *,
        pid: int,
        bounds: tuple[float, float, float, float],
        title: str,
    ) -> tuple[bytes, int, int, int]:
        quartz = self._quartz
        options = int(getattr(quartz, "kCGWindowListOptionOnScreenOnly", 1)) | int(
            getattr(quartz, "kCGWindowListExcludeDesktopElements", 16)
        )
        window_info = quartz.CGWindowListCopyWindowInfo(
            options,
            getattr(quartz, "kCGNullWindowID", 0),
        ) or []
        matches: list[tuple[int, str]] = []
        owner_pid_key = getattr(quartz, "kCGWindowOwnerPID", "kCGWindowOwnerPID")
        bounds_key = getattr(quartz, "kCGWindowBounds", "kCGWindowBounds")
        number_key = getattr(quartz, "kCGWindowNumber", "kCGWindowNumber")
        title_key = getattr(quartz, "kCGWindowName", "kCGWindowName")
        layer_key = getattr(quartz, "kCGWindowLayer", "kCGWindowLayer")
        for item in window_info:
            if int(item.get(owner_pid_key, -1)) != pid or int(item.get(layer_key, 0)) != 0:
                continue
            native_bounds = _cg_window_bounds(item.get(bounds_key))
            if native_bounds is None or any(
                abs(actual - expected) > 2.0
                for actual, expected in zip(native_bounds, bounds, strict=True)
            ):
                continue
            try:
                window_number = int(item[number_key])
            except (KeyError, TypeError, ValueError):
                continue
            matches.append((window_number, str(item.get(title_key) or "")))
        if len(matches) > 1 and title:
            titled = [match for match in matches if match[1] == title]
            if len(titled) == 1:
                matches = titled
        if len(matches) != 1:
            raise MacOSComputerUseError(
                "A0_TAG_SCREENSHOT_UNAVAILABLE",
                "CoreGraphics did not expose one verified active window with matching native bounds.",
            )

        window_number = matches[0][0]
        image = quartz.CGWindowListCreateImage(
            getattr(quartz, "CGRectNull"),
            getattr(quartz, "kCGWindowListOptionIncludingWindow", 8),
            window_number,
            int(getattr(quartz, "kCGWindowImageBoundsIgnoreFraming", 1))
            | int(getattr(quartz, "kCGWindowImageBestResolution", 8)),
        )
        if image is None:
            raise MacOSComputerUseError(
                "A0_TAG_SCREENSHOT_UNAVAILABLE",
                "CoreGraphics could not capture the verified active window.",
            )
        png_bytes, width, height = self._encode_cgimage_png(image)
        self.last_capture_strategy = "coregraphics-window"
        return png_bytes, width, height, window_number

    def _encode_cgimage_png(self, image: Any) -> tuple[bytes, int, int]:
        appkit = _load_appkit_module()
        image_rep = appkit.NSBitmapImageRep.alloc().initWithCGImage_(image)
        if image_rep is None:
            raise MacOSComputerUseError(
                "COMPUTER_USE_CAPTURE_UNAVAILABLE",
                "Unable to create a macOS bitmap representation for the captured image.",
            )

        png_type = getattr(appkit, "NSBitmapImageFileTypePNG", getattr(appkit, "NSPNGFileType", 4))
        png_data = image_rep.representationUsingType_properties_(png_type, {})
        if png_data is None:
            raise MacOSComputerUseError(
                "COMPUTER_USE_CAPTURE_UNAVAILABLE",
                "Unable to encode the macOS display image as PNG.",
            )

        png_bytes = bytes(png_data)
        width, height = _png_dimensions(png_bytes)
        return png_bytes, width, height

    def _capture_png_screencapture(
        self,
        *,
        fallback_error: MacOSComputerUseError | None = None,
    ) -> tuple[bytes, int, int]:
        screencapture = shutil.which("screencapture")
        if not screencapture:
            if fallback_error is not None:
                raise fallback_error
            raise MacOSComputerUseError(
                "COMPUTER_USE_UNSUPPORTED",
                "macOS screencapture utility is unavailable.",
            )

        temp_file = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        temp_path = Path(temp_file.name)
        temp_file.close()
        try:
            _emit_debug("driver.capture_png.exec", command=[screencapture, "-x", "-t", "png", str(temp_path)])
            completed = subprocess.run(
                [screencapture, "-x", "-t", "png", str(temp_path)],
                check=False,
                capture_output=True,
                text=True,
            )
            _emit_debug(
                "driver.capture_png.return",
                returncode=completed.returncode,
                stdout=(completed.stdout or "").strip(),
                stderr=(completed.stderr or "").strip(),
            )
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout or "").strip()
                message = "Unable to capture the macOS screen."
                if detail:
                    message = f"{message} {detail}"
                raise MacOSComputerUseError("COMPUTER_USE_CAPTURE_UNAVAILABLE", message)
            png_bytes = temp_path.read_bytes()
            width, height = _png_dimensions(png_bytes)
            self.last_capture_strategy = "screencapture-fallback"
            _emit_debug("driver.capture_png.ok", width=width, height=height, bytes=len(png_bytes))
            return png_bytes, width, height
        except FileNotFoundError as exc:
            if fallback_error is not None:
                raise fallback_error from exc
            raise MacOSComputerUseError(
                "COMPUTER_USE_CAPTURE_UNAVAILABLE",
                "macOS screencapture utility is unavailable.",
            ) from exc
        except OSError as exc:
            raise MacOSComputerUseError(
                "COMPUTER_USE_CAPTURE_UNAVAILABLE",
                "Unable to read the macOS screenshot image.",
            ) from exc
        finally:
            with contextlib.suppress(OSError):
                temp_path.unlink()

    def move(self, x: float, y: float) -> None:
        self._post_mouse_move(x, y)

    def _post_mouse_move(self, x: float, y: float) -> None:
        quartz = self._quartz
        point = (float(x), float(y))
        event = quartz.CGEventCreateMouseEvent(
            self._event_source,
            quartz.kCGEventMouseMoved,
            point,
            quartz.kCGMouseButtonLeft,
        )
        if event is None:
            raise MacOSComputerUseError(
                "COMPUTER_USE_INPUT_UNAVAILABLE",
                "Unable to create a macOS mouse-move event.",
            )
        quartz.CGEventPost(quartz.kCGHIDEventTap, event)

    def _cursor_position(self) -> tuple[float, float] | None:
        quartz = self._quartz
        event = quartz.CGEventCreate(self._event_source)
        if event is None:
            return None
        point = quartz.CGEventGetLocation(event)
        x = getattr(point, "x", None)
        y = getattr(point, "y", None)
        if x is None or y is None:
            try:
                x, y = point
            except Exception:
                return None
        return float(x), float(y)

    def _frontmost_application(self) -> Any | None:
        try:
            appkit = _load_appkit_module()
            workspace = appkit.NSWorkspace.sharedWorkspace()
            return workspace.frontmostApplication()
        except Exception:
            return None

    def _restore_frontmost_application(self, application: Any | None) -> None:
        if application is None:
            return
        try:
            appkit = _load_appkit_module()
            activate_ignoring_others = getattr(
                appkit,
                "NSApplicationActivateIgnoringOtherApps",
                1 << 1,
            )
            application.activateWithOptions_(activate_ignoring_others)
        except Exception:
            return

    def click(self, x: float, y: float, *, button: str, count: int) -> None:
        normalized_button = str(button or "left").strip().lower()
        if normalized_button == "left" and max(1, count) == 1:
            if self._press_accessibility_element_at_position(x, y):
                self.last_click_strategy = "accessibility-press"
                _emit_debug("driver.click.accessibility_press_ok", x=x, y=y)
                return

        quartz = self._quartz
        self.last_click_strategy = "quartz-cursor-restore"
        point = (float(x), float(y))
        button_value, down_type, up_type = self._mouse_button_spec(normalized_button)
        original_position = self._cursor_position()
        original_application = self._frontmost_application()
        self._post_mouse_move(x, y)
        try:
            for click_state in range(1, max(1, count) + 1):
                down = quartz.CGEventCreateMouseEvent(self._event_source, down_type, point, button_value)
                up = quartz.CGEventCreateMouseEvent(self._event_source, up_type, point, button_value)
                if down is None or up is None:
                    raise MacOSComputerUseError(
                        "COMPUTER_USE_INPUT_UNAVAILABLE",
                        "Unable to create a macOS mouse-click event.",
                    )
                quartz.CGEventSetIntegerValueField(down, quartz.kCGMouseEventClickState, click_state)
                quartz.CGEventSetIntegerValueField(up, quartz.kCGMouseEventClickState, click_state)
                quartz.CGEventPost(quartz.kCGHIDEventTap, down)
                quartz.CGEventPost(quartz.kCGHIDEventTap, up)
        finally:
            if original_position is not None:
                with contextlib.suppress(Exception):
                    self._post_mouse_move(*original_position)
            self._restore_frontmost_application(original_application)

    def _press_accessibility_element_at_position(self, x: float, y: float) -> bool:
        try:
            accessibility = _load_accessibility_module()
            create_system_wide = getattr(accessibility, "AXUIElementCreateSystemWide")
            element_at_position = getattr(accessibility, "AXUIElementCopyElementAtPosition")
            perform_action = getattr(accessibility, "AXUIElementPerformAction")
        except Exception:
            return False

        try:
            system = create_system_wide()
            result = element_at_position(system, float(x), float(y), None)
        except Exception as exc:
            _emit_debug("driver.click.accessibility_element_failed", error=str(exc))
            return False

        element = None
        error_code = 0
        if isinstance(result, tuple):
            if result:
                try:
                    error_code = int(result[0])
                except Exception:
                    error_code = 1
            if len(result) > 1:
                element = result[1]
        else:
            element = result

        if error_code != 0 or element is None:
            _emit_debug("driver.click.accessibility_element_missing", error_code=error_code)
            return False

        press_action = getattr(accessibility, "kAXPressAction", "AXPress")
        try:
            press_result = perform_action(element, press_action)
        except Exception as exc:
            _emit_debug("driver.click.accessibility_press_failed", error=str(exc))
            return False

        if isinstance(press_result, tuple):
            press_result = press_result[0] if press_result else 1
        try:
            press_error = int(press_result or 0)
        except Exception:
            press_error = 1
        if press_error != 0:
            _emit_debug("driver.click.accessibility_press_rejected", error_code=press_error)
            return False
        return True

    def scroll(self, dx: int, dy: int) -> None:
        quartz = self._quartz
        wheel_count = 2 if dx else 1
        event = quartz.CGEventCreateScrollWheelEvent(
            self._event_source,
            quartz.kCGScrollEventUnitLine,
            wheel_count,
            int(dy),
            int(dx),
        )
        if event is None:
            raise MacOSComputerUseError(
                "COMPUTER_USE_INPUT_UNAVAILABLE",
                "Unable to create a macOS scroll event.",
            )
        quartz.CGEventPost(quartz.kCGHIDEventTap, event)

    def key(self, keys: list[str]) -> None:
        normalized = [str(item).strip() for item in keys if str(item).strip()]
        if not normalized:
            raise MacOSComputerUseError(
                "COMPUTER_USE_KEYS_REQUIRED",
                "key requires a non-empty keys list.",
            )

        modifiers = normalized[:-1]
        body = normalized[-1]
        modifier_specs: list[_ResolvedKey] = []
        for token in modifiers:
            spec = self._resolve_key(token)
            if not spec.modifier_flag_name:
                raise MacOSComputerUseError(
                    "COMPUTER_USE_BAD_KEY",
                    f"Unsupported modifier key: {token}",
                )
            modifier_specs.append(spec)

        body_spec = self._resolve_key(body)
        if body_spec.modifier_flag_name and not modifier_specs:
            modifier_specs = [body_spec]
            body_spec = _ResolvedKey(keycode=body_spec.keycode)

        temporary_shift = False
        if body_spec.requires_shift and not any(spec.modifier_flag_name == "kCGEventFlagMaskShift" for spec in modifier_specs):
            modifier_specs.append(self._resolve_key("shift"))
            temporary_shift = True

        active_flags = 0
        for spec in modifier_specs:
            active_flags = self._post_modifier_event(spec, True, active_flags)

        self._post_keyboard_event(body_spec.keycode, True, active_flags)
        self._post_keyboard_event(body_spec.keycode, False, active_flags)

        for spec in reversed(modifier_specs):
            active_flags = self._post_modifier_event(spec, False, active_flags)

        if temporary_shift:
            active_flags = 0

    def type_text(self, text: str, *, submit: bool) -> None:
        if not text:
            raise MacOSComputerUseError(
                "COMPUTER_USE_TEXT_REQUIRED",
                "type requires text.",
            )
        for char in text:
            if char in {"\r", "\n"}:
                self.key(["enter"])
                continue
            if char == "\t":
                self.key(["tab"])
                continue
            self._type_unicode_char(char)
        if submit:
            self.key(["enter"])

    def _resolve_key(self, token: str) -> _ResolvedKey:
        cleaned = str(token or "").strip()
        lowered = cleaned.lower()
        modifier = _MODIFIER_KEY_SPECS.get(lowered)
        if modifier is not None:
            keycode, _label, flag_name = modifier
            return _ResolvedKey(keycode=keycode, modifier_flag_name=flag_name)

        special = _SPECIAL_KEYCODES.get(lowered)
        if special is not None:
            return _ResolvedKey(keycode=special)

        if len(cleaned) == 1:
            if cleaned in _CHAR_KEYCODES:
                return _ResolvedKey(keycode=_CHAR_KEYCODES[cleaned])
            if cleaned.isalpha() and cleaned.lower() in _CHAR_KEYCODES:
                return _ResolvedKey(
                    keycode=_CHAR_KEYCODES[cleaned.lower()],
                    requires_shift=cleaned.isupper(),
                )
            shifted_base = _SHIFTED_CHAR_ALIASES.get(cleaned)
            if shifted_base is not None:
                return _ResolvedKey(keycode=_CHAR_KEYCODES[shifted_base], requires_shift=True)

        raise MacOSComputerUseError("COMPUTER_USE_BAD_KEY", f"Unsupported key: {token}")

    def _mouse_button_spec(self, button: str) -> tuple[int, int, int]:
        quartz = self._quartz
        normalized = str(button or "left").strip().lower()
        if normalized == "right":
            return quartz.kCGMouseButtonRight, quartz.kCGEventRightMouseDown, quartz.kCGEventRightMouseUp
        if normalized in {"middle", "center"}:
            return quartz.kCGMouseButtonCenter, quartz.kCGEventOtherMouseDown, quartz.kCGEventOtherMouseUp
        return quartz.kCGMouseButtonLeft, quartz.kCGEventLeftMouseDown, quartz.kCGEventLeftMouseUp

    def _post_modifier_event(self, spec: _ResolvedKey, is_down: bool, active_flags: int) -> int:
        quartz = self._quartz
        if not spec.modifier_flag_name:
            return active_flags
        flag_value = getattr(quartz, spec.modifier_flag_name)
        next_flags = active_flags | flag_value if is_down else active_flags & ~flag_value
        self._post_keyboard_event(spec.keycode, is_down, next_flags if is_down else active_flags)
        return next_flags

    def _post_keyboard_event(self, keycode: int, is_down: bool, flags: int) -> None:
        quartz = self._quartz
        event = quartz.CGEventCreateKeyboardEvent(self._event_source, keycode, is_down)
        if event is None:
            raise MacOSComputerUseError(
                "COMPUTER_USE_INPUT_UNAVAILABLE",
                "Unable to create a macOS keyboard event.",
            )
        quartz.CGEventSetFlags(event, flags)
        quartz.CGEventPost(quartz.kCGHIDEventTap, event)

    def _type_unicode_char(self, char: str) -> None:
        quartz = self._quartz
        down = quartz.CGEventCreateKeyboardEvent(self._event_source, 0, True)
        up = quartz.CGEventCreateKeyboardEvent(self._event_source, 0, False)
        if down is None or up is None:
            raise MacOSComputerUseError(
                "COMPUTER_USE_INPUT_UNAVAILABLE",
                "Unable to create a macOS Unicode keyboard event.",
            )
        quartz.CGEventKeyboardSetUnicodeString(down, len(char), char)
        quartz.CGEventKeyboardSetUnicodeString(up, len(char), char)
        quartz.CGEventPost(quartz.kCGHIDEventTap, down)
        quartz.CGEventPost(quartz.kCGHIDEventTap, up)


def _load_default_driver() -> _MacOSDesktopAutomation:
    if not macos_backend_supported():
        raise MacOSComputerUseError("COMPUTER_USE_UNSUPPORTED", macos_backend_support_reason())
    return _MacOSDesktopAutomation()


@dataclass
class _RuntimeSession:
    session: MacOSSession
    policy: TrustModePolicy


@dataclass
class _TagTarget:
    token: str
    pid: int
    bundle_id: str
    app_name: str
    window_title: str
    window_id: str
    window_bounds: tuple[float, float, float, float]
    window: Any = field(repr=False)
    element: Any = field(repr=False)
    start: int
    end: int
    caret: int
    original: str
    editable: bool
    captured_at: float


class MacOSComputerUseRuntime:
    def __init__(
        self,
        *,
        driver: Any | None = None,
        store: MacOSSessionStore | None = None,
        state_dir: str | os.PathLike[str] | None = None,
        capture_debug_dir: str | os.PathLike[str] | None = None,
    ) -> None:
        self._driver = driver or _load_default_driver()
        self._store = store or MacOSSessionStore(state_dir=state_dir)
        self._capture_debug_dir = (
            Path(capture_debug_dir)
            if capture_debug_dir is not None
            else _default_capture_debug_dir()
        )
        self._session: _RuntimeSession | None = None
        self._element_index_cache: dict[int, dict[str, Any]] = {}
        self._tag_target: _TagTarget | None = None

    @property
    def supported(self) -> bool:
        return macos_backend_supported()

    def hello_metadata(self) -> dict[str, Any]:
        return {
            "supported": self.supported,
            "backend_id": MACOS_BACKEND_ID,
            "backend_family": MACOS_BACKEND_FAMILY,
            "features": list(MACOS_BACKEND_FEATURES),
            **_backend_contract_metadata(),
            "support_reason": macos_backend_support_reason(),
        }

    def status(self, params: dict[str, Any]) -> dict[str, Any]:
        context_id = normalize_context_id(params.get("context_id"))
        if self._session is not None and self._session.session.context_id == context_id:
            payload = self._session.session.to_payload(reused=False)
            payload["active"] = True
            payload["status"] = "active"
            return payload

        stored = self._store.get(context_id)
        if stored is not None:
            payload = stored.to_payload(reused=False)
            payload["active"] = bool(stored.active)
            payload["status"] = "active" if stored.active else "stopped"
            return payload

        return {
            "active": False,
            "context_id": context_id,
            "backend_id": MACOS_BACKEND_ID,
            "backend_family": MACOS_BACKEND_FAMILY,
            "features": list(MACOS_BACKEND_FEATURES),
            **_backend_contract_metadata(),
            "support_reason": macos_backend_support_reason(),
        }

    def dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        handlers = {
            "permission_status": self.permission_status,
            "request_accessibility": self.request_accessibility,
            "request_screen_recording": self.request_screen_recording,
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
        handler = handlers.get(str(method or "").strip().lower())
        if handler is None:
            raise MacOSComputerUseError(
                "UNKNOWN_METHOD",
                f"Unknown computer-use helper method: {method}",
            )
        normalized_method = str(method or "").strip().lower()
        normalized_params = dict(params)
        if normalized_method in {
            "capture",
            "list_windows",
            "get_window_state",
            "element_action",
            "ax_snapshot",
            "ax_action",
            "move",
            "click",
            "scroll",
            "key",
            "type",
        }:
            normalized_params = normalize_action_payload(
                normalized_method,
                normalized_params,
                context_id=normalize_context_id(normalized_params.get("context_id")),
            )
        return handler(normalized_params)

    def permission_status(self, _params: dict[str, Any] | None = None) -> dict[str, Any]:
        accessibility = _load_accessibility_module()
        quartz = _load_quartz_module()
        accessibility_granted = self._accessibility_trusted(accessibility, prompt=False)
        screen_recording_granted = self._screen_recording_granted(quartz)
        state = (
            "accessibility_required"
            if not accessibility_granted
            else "screen_recording_required"
            if not screen_recording_granted
            else "ready"
        )
        return {
            "state": state,
            "accessibility": "granted" if accessibility_granted else "required",
            "screen_recording": "granted" if screen_recording_granted else "required",
            "restart_required": False,
        }

    def request_accessibility(self, _params: dict[str, Any] | None = None) -> dict[str, Any]:
        accessibility = _load_accessibility_module()
        granted = self._accessibility_trusted(accessibility, prompt=True)
        return {
            "state": "ready" if granted else "accessibility_required",
            "accessibility": "granted" if granted else "required",
            "screen_recording": "unknown",
            "restart_required": False,
        }

    def request_screen_recording(self, _params: dict[str, Any] | None = None) -> dict[str, Any]:
        quartz = _load_quartz_module()
        if self._screen_recording_granted(quartz):
            return {
                "state": "ready",
                "accessibility": "granted",
                "screen_recording": "granted",
                "restart_required": False,
            }
        request = getattr(quartz, "CGRequestScreenCaptureAccess", None)
        if not callable(request):
            raise MacOSComputerUseError(
                "COMPUTER_USE_CAPTURE_UNAVAILABLE",
                "This macOS version cannot request Screen Recording permission automatically.",
            )
        requested = bool(request())
        granted = self._screen_recording_granted(quartz)
        return {
            "state": "ready" if granted else "screen_recording_required",
            "accessibility": "granted",
            "screen_recording": "granted" if granted else "required",
            "permission_granted": requested,
            "restart_required": requested and not granted,
        }

    def start_session(self, params: dict[str, Any]) -> dict[str, Any]:
        trust_mode = str(params.get("trust_mode") or "persistent").strip().lower()
        context_id = normalize_context_id(params.get("context_id"))
        restore_token = normalize_restore_token(params.get("restore_token"))
        allow_prompt = bool(params.get("allow_prompt", trust_mode != "allow"))
        request_timeout = float(params.get("request_timeout_seconds") or 0.0)
        allow = trust_mode == "allow" or not allow_prompt
        policy = resolve_trust_mode_policy(trust_mode, restore_token)
        _emit_debug(
            "start_session.begin",
            context_id=context_id,
            trust_mode=trust_mode,
            allow_prompt=allow_prompt,
            request_timeout_seconds=request_timeout,
            allow=allow,
            restore_token_present=bool(restore_token),
        )

        if policy.trust_mode not in MACOS_TRUST_MODES:
            raise MacOSComputerUseError(
                "COMPUTER_USE_UNSUPPORTED",
                f"Unsupported trust mode: {trust_mode!r}",
            )
        if policy.trust_mode == "allow" and not policy.reuse_allowed:
            raise MacOSComputerUseError(
                "COMPUTER_USE_REARM_REQUIRED",
                "Allow requires a stored restore token.",
            )

        if self._session is not None and self._session.session.context_id == context_id:
            self._session.session.active = True
            _emit_debug(
                "start_session.reuse_active_runtime_session",
                context_id=context_id,
                session_id=self._session.session.session_id,
            )
            return self._session.session.to_payload(reused=False)

        _emit_debug("start_session.accessibility.begin", context_id=context_id)
        self._ensure_accessibility_permission(
            allow_prompt=allow_prompt,
            timeout=request_timeout,
            allow=allow,
        )
        _emit_debug("start_session.accessibility.ok", context_id=context_id)
        if context_id == "launcher-tag":
            width, height = 0, 0
        else:
            _emit_debug("start_session.capture_probe.begin", context_id=context_id)
            width, height = self._probe_capture_dimensions(
                allow_prompt=allow_prompt,
                timeout=request_timeout,
                allow=allow,
            )
            _emit_debug("start_session.capture_probe.ok", context_id=context_id, width=width, height=height)

        reusable = self._store.get(context_id)
        if reusable is not None and policy.reuse_allowed and reusable.restore_token == restore_token:
            reusable = MacOSSession(
                context_id=context_id,
                session_id=reusable.session_id,
                trust_mode=policy.trust_mode,
                restore_token=reusable.restore_token,
                active=True,
                width=width,
                height=height,
            )
            self._session = _RuntimeSession(session=reusable, policy=policy)
            self._store.put(reusable)
            _emit_debug(
                "start_session.reused_persisted_session",
                context_id=context_id,
                session_id=reusable.session_id,
                width=width,
                height=height,
            )
            return reusable.to_payload(reused=True)

        session = MacOSSession(
            context_id=context_id,
            session_id=uuid.uuid4().hex,
            trust_mode=policy.trust_mode,
            restore_token=restore_token if policy.persist_metadata else "",
            active=True,
            width=width,
            height=height,
        )
        if policy.persist_metadata and not session.restore_token:
            session = MacOSSession(
                context_id=session.context_id,
                session_id=session.session_id,
                trust_mode=session.trust_mode,
                restore_token=str(uuid.uuid4()),
                active=session.active,
                width=session.width,
                height=session.height,
            )
        self._session = _RuntimeSession(session=session, policy=policy)
        if policy.persist_metadata:
            self._store.put(session)
        _emit_debug(
            "start_session.created_session",
            context_id=context_id,
            session_id=session.session_id,
            width=width,
            height=height,
            persisted=policy.persist_metadata,
        )
        return session.to_payload(reused=False)

    def capture(self, params: dict[str, Any]) -> dict[str, Any]:
        session = self._require_session(params)
        png_bytes, width, height = self._driver.capture_png()
        capture_backend = str(getattr(self._driver, "last_capture_strategy", "") or "").strip()
        session.session.width = width
        session.session.height = height
        session.session.updated_at = time.time()
        if session.policy.persist_metadata:
            self._store.put(session.session)

        result = {
            "session_id": session.session.session_id,
            "context_id": session.session.context_id,
            "width": width,
            "height": height,
            "captured_at": time.time(),
        }
        if capture_backend:
            result["capture_backend"] = capture_backend
        capture_path_value = str(params.get("capture_path") or "").strip()
        if capture_path_value:
            capture_path = Path(capture_path_value)
            capture_path.parent.mkdir(parents=True, exist_ok=True)
            capture_path.write_bytes(png_bytes)
            result["capture_path"] = str(capture_path)
        else:
            result["png_base64"] = base64.b64encode(png_bytes).decode("ascii")
        return result

    def tag_context(self, params: dict[str, Any]) -> dict[str, Any]:
        session = self._require_session(params)
        self._tag_target = None
        accessibility = _load_accessibility_module()
        app_info, app_root = self._frontmost_ax_root(accessibility)
        try:
            pid = int(app_info.get("pid"))
        except (TypeError, ValueError):
            raise MacOSComputerUseError(
                "A0_TAG_FOCUS_UNAVAILABLE",
                "The frontmost macOS application has no verifiable process identity.",
            ) from None
        window = _ax_copy_attribute(
            accessibility,
            app_root,
            "kAXFocusedWindowAttribute",
            "AXFocusedWindow",
        )
        element = _ax_copy_attribute(
            accessibility,
            app_root,
            "kAXFocusedUIElementAttribute",
            "AXFocusedUIElement",
        )
        if window is None or element is None:
            raise MacOSComputerUseError(
                "A0_TAG_FOCUS_UNAVAILABLE",
                "No accessible focused field was found in the frontmost macOS window.",
            )
        element_window = _ax_copy_attribute(
            accessibility,
            element,
            "kAXWindowAttribute",
            "AXWindow",
        )
        if element_window is not None and not self._ax_elements_equal(
            accessibility,
            element_window,
            window,
        ):
            raise MacOSComputerUseError(
                "A0_TAG_WINDOW_INACTIVE",
                "The focused field is not inside the active macOS window.",
            )
        if self._tag_element_protected(accessibility, element):
            raise MacOSComputerUseError(
                "A0_TAG_PROTECTED_FIELD",
                "A0 Tag is unavailable in protected fields.",
            )

        start, end, caret, original, query, profile, focused_context = self._parse_tag_invocation(
            accessibility,
            element,
        )
        editable = self._tag_element_editable(accessibility, element)
        screen_size = (session.session.width, session.session.height)
        frame = self._ax_frame(accessibility, window, screen_size=screen_size)
        bounds = (
            float(frame.get("x", 0.0)) if frame else 0.0,
            float(frame.get("y", 0.0)) if frame else 0.0,
            float(frame.get("width", 0.0)) if frame else 0.0,
            float(frame.get("height", 0.0)) if frame else 0.0,
        )
        windows = _ax_iterable(
            _ax_copy_attribute(accessibility, app_root, "kAXWindowsAttribute", "AXWindows")
        )
        window_path = next(
            ([index] for index, candidate in enumerate(windows) if self._ax_elements_equal(accessibility, candidate, window)),
            [],
        )
        window_id = self._window_id_for_ax_window(app_info, path=window_path)
        app_name = _bounded_text(app_info.get("name") or "macOS app", limit=128)
        window_title = _bounded_text(
            _ax_copy_attribute(accessibility, window, "kAXTitleAttribute", "AXTitle") or app_name,
            limit=240,
        )
        budget: dict[str, Any] = {"count": 0, "truncated": False}
        tree = self._serialize_ax_element(
            accessibility,
            window,
            path=window_path,
            depth=0,
            max_depth=5,
            max_nodes=120,
            budget=budget,
            screen_size=screen_size,
        ) or {}
        target = _TagTarget(
            token=uuid.uuid4().hex,
            pid=pid,
            bundle_id=_bounded_text(app_info.get("bundle_id"), limit=256),
            app_name=app_name,
            window_title=window_title,
            window_id=window_id,
            window_bounds=bounds,
            window=window,
            element=element,
            start=start,
            end=end,
            caret=caret,
            original=original,
            editable=editable,
            captured_at=time.time(),
        )
        self._tag_target = target

        screenshot_status, screenshot_error, artifact = self._tag_window_screenshot(target)
        return {
            "session_id": session.session.session_id,
            "context_id": session.session.context_id,
            "target_token": target.token,
            "tag_text": original,
            "query": query,
            "profile_override": profile,
            "app_name": app_name,
            "window_title": window_title,
            "window_id": window_id,
            "focused_text": focused_context,
            "tree": tree,
            "tree_truncated": bool(budget["truncated"]),
            "replace_supported": editable,
            "screenshot_status": screenshot_status,
            **({"screenshot_error": screenshot_error} if screenshot_error else {}),
            **({"artifact": artifact} if artifact is not None else {}),
        }

    def tag_replace(self, params: dict[str, Any]) -> dict[str, Any]:
        session = self._require_session(params)
        token = str(params.get("target_token") or "").strip()
        replacement = str(params.get("replacement") or "")
        target = self._tag_target
        if target is None or not token or token != target.token:
            raise MacOSComputerUseError(
                "A0_TAG_TARGET_EXPIRED",
                "The original A0 Tag field is no longer available.",
            )
        if time.time() - target.captured_at > _TAG_TARGET_TTL_SECONDS:
            self._tag_target = None
            raise MacOSComputerUseError("A0_TAG_TARGET_EXPIRED", "The original A0 Tag field expired.")
        if not target.editable:
            raise MacOSComputerUseError(
                "A0_TAG_REPLACE_UNSUPPORTED",
                "The tagged field does not support safe replacement.",
            )
        if not replacement or len(replacement) > _TAG_REPLACEMENT_MAX_CHARS:
            raise MacOSComputerUseError(
                "A0_TAG_INVALID_REPLACEMENT",
                "A0 Tag replacement must contain 1 to 16384 characters.",
            )

        accessibility = _load_accessibility_module()
        app_info, app_root = self._frontmost_ax_root(accessibility)
        if (
            str(app_info.get("pid") or "") != str(target.pid)
            or _bounded_text(app_info.get("bundle_id"), limit=256) != target.bundle_id
        ):
            raise MacOSComputerUseError(
                "A0_TAG_TARGET_CHANGED",
                "The frontmost application changed while Agent Zero was working.",
            )
        window = _ax_copy_attribute(
            accessibility,
            app_root,
            "kAXFocusedWindowAttribute",
            "AXFocusedWindow",
        )
        element = _ax_copy_attribute(
            accessibility,
            app_root,
            "kAXFocusedUIElementAttribute",
            "AXFocusedUIElement",
        )
        if (
            window is None
            or element is None
            or not self._ax_elements_equal(accessibility, window, target.window)
            or not self._ax_elements_equal(accessibility, element, target.element)
        ):
            raise MacOSComputerUseError(
                "A0_TAG_TARGET_CHANGED",
                "The active window or focused field changed while Agent Zero was working.",
            )
        element_window = _ax_copy_attribute(
            accessibility,
            element,
            "kAXWindowAttribute",
            "AXWindow",
        )
        current_title = _bounded_text(
            _ax_copy_attribute(accessibility, window, "kAXTitleAttribute", "AXTitle") or target.app_name,
            limit=240,
        )
        if (
            (element_window is not None and not self._ax_elements_equal(accessibility, element_window, window))
            or current_title != target.window_title
        ):
            raise MacOSComputerUseError(
                "A0_TAG_TARGET_CHANGED",
                "The active window changed while Agent Zero was working.",
            )
        if self._tag_element_protected(accessibility, element):
            raise MacOSComputerUseError(
                "A0_TAG_TARGET_CHANGED",
                "The tagged field became protected while Agent Zero was working.",
            )
        if not self._tag_element_editable(accessibility, element):
            raise MacOSComputerUseError(
                "A0_TAG_TARGET_CHANGED",
                "The tagged field is no longer safely editable.",
            )
        if self._tag_selected_range(accessibility, element) != (target.caret, 0):
            raise MacOSComputerUseError(
                "A0_TAG_TARGET_CHANGED",
                "The caret moved while Agent Zero was working.",
            )
        try:
            current = self._tag_text_range(accessibility, element, target.start, target.end)
        except MacOSComputerUseError:
            current = None
        if current != target.original:
            raise MacOSComputerUseError(
                "A0_TAG_TARGET_CHANGED",
                "The original A0 Tag text changed while Agent Zero was working.",
            )

        self._replace_tag_text(accessibility, target, replacement)
        self._tag_target = None
        return {
            "session_id": session.session.session_id,
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
        accessibility = _load_accessibility_module()
        max_windows = max(1, coerce_int(params.get("max_windows"), name="max_windows", default=80))
        include_hidden = coerce_bool(params.get("include_hidden"), default=False)
        include_offscreen = coerce_bool(params.get("include_offscreen"), default=False)
        screen_size = (session.session.width, session.session.height)
        windows: list[dict[str, Any]] = []
        for app_info, _app_root, window, path in self._ax_window_roots(accessibility):
            if not include_hidden and bool(app_info.get("hidden")):
                continue
            summary = self._ax_target_summary(accessibility, window, path=path, screen_size=screen_size)
            if not include_offscreen and self._ax_summary_is_offscreen(summary):
                continue
            window_id = self._window_id_for_ax_window(app_info, path=path)
            windows.append(
                {
                    "window_id": window_id,
                    "pid": app_info.get("pid"),
                    "app_name": app_info.get("name"),
                    "bundle_id": app_info.get("bundle_id"),
                    "title": summary.get("title"),
                    "role": summary.get("role", "AXWindow"),
                    "frame": summary.get("frame"),
                    "focused": summary.get("focused"),
                    "visible": not bool(app_info.get("hidden")),
                    "path": list(path),
                }
            )
            if len(windows) >= max_windows:
                break
        return {
            "session_id": session.session.session_id,
            "context_id": session.session.context_id,
            "backend": "ax",
            "count": len(windows),
            "windows": windows,
        }

    def get_window_state(self, params: dict[str, Any]) -> dict[str, Any]:
        session = self._require_session(params)
        accessibility = _load_accessibility_module()
        max_depth = min(
            _AX_HARD_MAX_DEPTH,
            max(0, coerce_int(params.get("max_depth"), name="max_depth", default=_AX_DEFAULT_MAX_DEPTH)),
        )
        max_nodes = min(
            _AX_HARD_MAX_NODES,
            max(1, coerce_int(params.get("max_nodes"), name="max_nodes", default=_AX_DEFAULT_MAX_NODES)),
        )
        screen_size = (session.session.width, session.session.height)
        app_info, window, path, window_summary = self._resolve_ax_window_root(
            accessibility,
            params,
            screen_size=screen_size,
        )
        budget = {"count": 0, "truncated": False}
        tree = self._serialize_ax_element(
            accessibility,
            window,
            path=path,
            depth=0,
            max_depth=max_depth,
            max_nodes=max_nodes,
            budget=budget,
            screen_size=screen_size,
        ) or {}
        window_id = self._window_id_for_ax_window(app_info, path=path)
        self._cache_element_indices(tree, window_id=window_id)
        window_summary["window_id"] = window_id
        return {
            "session_id": session.session.session_id,
            "context_id": session.session.context_id,
            "backend": "ax",
            "mode": str(params.get("mode") or "ax").strip() or "ax",
            "window_id": window_id,
            "window": window_summary,
            "app": app_info,
            "tree": tree,
            "node_count": budget["count"],
            "truncated": bool(budget["truncated"]),
            "max_depth": max_depth,
            "max_nodes": max_nodes,
        }

    def element_action(self, params: dict[str, Any]) -> dict[str, Any]:
        session = self._require_session(params)
        accessibility = _load_accessibility_module()
        dispatch = normalize_dispatch(params.get("dispatch"), default="background")
        operation = str(
            params.get("operation")
            or params.get("ax_action")
            or params.get("name")
            or "press"
        ).strip().lower()
        if operation in {"click", "activate", "invoke"}:
            operation = "press"
        if operation in {"type", "type_text"}:
            operation = "set_value"
        element, target = self._resolve_element_action_target(accessibility, params)
        target.setdefault("element_index", params.get("element_index"))

        if dispatch in {"background", "auto"}:
            background_result = self._try_background_ax_action(
                accessibility,
                element,
                target=target,
                operation=operation,
                params=params,
                session_id=session.session.session_id,
                context_id=session.session.context_id,
                requested_dispatch=dispatch,
            )
            if not background_result.get("background_unavailable"):
                return background_result
            if dispatch == "background":
                return background_result

        foreground_result = self._perform_ax_element_action(
            accessibility,
            element,
            target=target,
            operation=operation,
            params=params,
            session_id=session.session.session_id,
            context_id=session.session.context_id,
        )
        foreground_result["requested_dispatch"] = dispatch
        foreground_result["actual_dispatch"] = "foreground"
        foreground_result["foreground_fallback_used"] = dispatch == "auto"
        return foreground_result

    def ax_snapshot(self, params: dict[str, Any]) -> dict[str, Any]:
        session = self._require_session(params)
        max_depth = min(
            _AX_HARD_MAX_DEPTH,
            max(0, coerce_int(params.get("max_depth"), name="max_depth", default=_AX_DEFAULT_MAX_DEPTH)),
        )
        max_nodes = min(
            _AX_HARD_MAX_NODES,
            max(1, coerce_int(params.get("max_nodes"), name="max_nodes", default=_AX_DEFAULT_MAX_NODES)),
        )
        accessibility = _load_accessibility_module()
        app_info, root = self._frontmost_ax_root(accessibility)
        budget = {"count": 0, "truncated": False}
        tree = self._serialize_ax_element(
            accessibility,
            root,
            path=[],
            depth=0,
            max_depth=max_depth,
            max_nodes=max_nodes,
            budget=budget,
            screen_size=(session.session.width, session.session.height),
        )
        return {
            "session_id": session.session.session_id,
            "context_id": session.session.context_id,
            "app": app_info,
            "tree": tree or {},
            "node_count": budget["count"],
            "truncated": bool(budget["truncated"]),
            "max_depth": max_depth,
            "max_nodes": max_nodes,
        }

    def ax_action(self, params: dict[str, Any]) -> dict[str, Any]:
        session = self._require_session(params)
        accessibility = _load_accessibility_module()
        operation = str(
            params.get("operation")
            or params.get("ax_action")
            or params.get("name")
            or "press"
        ).strip().lower()
        if operation in {"click", "activate"}:
            operation = "press"
        if operation not in {"press", "focus", "set_value"}:
            raise MacOSComputerUseError(
                "COMPUTER_USE_BAD_AX_ACTION",
                "ax_action operation must be one of: press, focus, set_value.",
            )

        element, target = self._resolve_ax_target(accessibility, params)
        if operation == "press":
            action_name = _ax_constant(accessibility, "kAXPressAction", "AXPress")
            error_code = self._perform_ax_action(accessibility, element, action_name)
            if error_code != 0:
                raise MacOSComputerUseError(
                    "COMPUTER_USE_AX_ACTION_FAILED",
                    f"AX press failed with error {error_code}.",
                )
        elif operation == "focus":
            focused_attr = _ax_constant(accessibility, "kAXFocusedAttribute", "AXFocused")
            error_code = self._set_ax_attribute(accessibility, element, focused_attr, True)
            if error_code != 0:
                raise MacOSComputerUseError(
                    "COMPUTER_USE_AX_ACTION_FAILED",
                    f"AX focus failed with error {error_code}.",
                )
        else:
            value = params.get("value", params.get("text"))
            if value is None:
                raise MacOSComputerUseError(
                    "COMPUTER_USE_AX_VALUE_REQUIRED",
                    "ax_action set_value requires value or text.",
                )
            value_attr = _ax_constant(accessibility, "kAXValueAttribute", "AXValue")
            error_code = self._set_ax_attribute(accessibility, element, value_attr, str(value))
            if error_code != 0:
                raise MacOSComputerUseError(
                    "COMPUTER_USE_AX_ACTION_FAILED",
                    f"AX set_value failed with error {error_code}.",
                )

        return {
            "session_id": session.session.session_id,
            "context_id": session.session.context_id,
            "operation": operation,
            "target": target,
        }

    def move(self, params: dict[str, Any]) -> dict[str, Any]:
        session = self._require_session(params)
        x = float(params.get("x"))
        y = float(params.get("y"))
        pixel_x = session.session.width * x
        pixel_y = session.session.height * y
        self._driver.move(pixel_x, pixel_y)
        return {
            "session_id": session.session.session_id,
            "x": x,
            "y": y,
            "pixel_x": pixel_x,
            "pixel_y": pixel_y,
        }

    def click(self, params: dict[str, Any]) -> dict[str, Any]:
        session = self._require_session(params)
        x = float(params.get("x", 0.5))
        y = float(params.get("y", 0.5))
        button_name = str(params.get("button") or "left").strip().lower()
        count = max(1, int(params.get("count") or 1))
        pixel_x = session.session.width * x
        pixel_y = session.session.height * y
        self._driver.click(pixel_x, pixel_y, button=button_name, count=count)
        action_backend = str(getattr(self._driver, "last_click_strategy", "") or "").strip()
        return {
            "session_id": session.session.session_id,
            "button": button_name,
            "count": count,
            "x": x,
            "y": y,
            **({"action_backend": action_backend} if action_backend else {}),
        }

    def scroll(self, params: dict[str, Any]) -> dict[str, Any]:
        session = self._require_session(params)
        dx = int(params.get("dx") or 0)
        dy = int(params.get("dy") or 0)
        self._driver.scroll(dx, dy)
        return {
            "session_id": session.session.session_id,
            "dx": dx,
            "dy": dy,
        }

    def key(self, params: dict[str, Any]) -> dict[str, Any]:
        session = self._require_session(params)
        keys = params.get("keys")
        if not isinstance(keys, list) or not keys:
            raise MacOSComputerUseError(
                "COMPUTER_USE_KEYS_REQUIRED",
                "key requires a non-empty keys list.",
            )
        normalized = [str(item).strip() for item in keys if str(item).strip()]
        if not normalized:
            raise MacOSComputerUseError(
                "COMPUTER_USE_KEYS_REQUIRED",
                "key requires a non-empty keys list.",
            )
        self._driver.key(normalized)
        return {
            "session_id": session.session.session_id,
            "keys": normalized,
        }

    def type_text(self, params: dict[str, Any]) -> dict[str, Any]:
        session = self._require_session(params)
        text = str(params.get("text") or "")
        submit = coerce_bool(params.get("submit"))
        if not text:
            raise MacOSComputerUseError(
                "COMPUTER_USE_TEXT_REQUIRED",
                "type requires text.",
            )
        self._driver.type_text(text, submit=submit)
        return {
            "session_id": session.session.session_id,
            "text": text,
            "submitted": submit,
        }

    def stop_session(self, params: dict[str, Any]) -> dict[str, Any]:
        context_id = normalize_context_id(params.get("context_id"))
        self._tag_target = None
        session = self._session
        if session is not None and session.session.context_id == context_id:
            session.session.active = False
            session.session.updated_at = time.time()
            if session.policy.persist_metadata:
                self._store.put(session.session)
            self._session = None
        return {"active": False, "status": "stopped", "session_id": ""}

    def _parse_tag_invocation(
        self,
        accessibility: Any,
        element: Any,
    ) -> tuple[int, int, int, str, str, str, str]:
        character_count = self._tag_text_count(accessibility, element)
        caret, selection_length = self._tag_selected_range(accessibility, element)
        if character_count <= 0 or caret < 0 or caret > character_count or selection_length != 0:
            raise MacOSComputerUseError(
                "A0_TAG_TEXT_UNAVAILABLE",
                "The focused field has no readable caret text.",
            )

        before_start = max(0, caret - _TAG_TEXT_WINDOW_CHARS)
        before_start, _, before = self._tag_bounded_text_range(
            accessibility,
            element,
            before_start,
            caret,
            trim_start=True,
        )
        newline = max(before.rfind("\n"), before.rfind("\r"))
        if newline < 0 and before_start > 0:
            raise MacOSComputerUseError("A0_TAG_QUERY_TOO_LONG", "The A0 Tag line is too long.")
        line_start = before_start + _utf16_length(before[: newline + 1])
        line = before[newline + 1 :]

        after_end = min(character_count, caret + _TAG_TEXT_WINDOW_CHARS)
        _, after_end, after = self._tag_bounded_text_range(
            accessibility,
            element,
            caret,
            after_end,
            trim_start=False,
        )
        after_line = re.split(r"[\r\n]", after, maxsplit=1)[0]
        after_line_bounded = "\n" in after or "\r" in after or after_end == character_count
        if not after_line_bounded or after_line.strip():
            raise MacOSComputerUseError(
                "A0_TAG_CARET_POSITION",
                "Place the caret at the end of the A0 Tag request.",
            )

        match = re.fullmatch(
            r"(?P<indent>[ \t]*)(?P<tag>@a0(?:\.(?P<profile>[A-Za-z0-9][A-Za-z0-9_-]{0,63}))?[ \t]+(?P<query>.*?))(?P<trailing>[ \t]*)",
            line,
            flags=re.IGNORECASE,
        )
        if match is None:
            raise MacOSComputerUseError(
                "A0_TAG_NOT_FOUND",
                "The focused line does not contain a valid @a0 request.",
            )
        query = str(match.group("query") or "").strip()
        if not query:
            raise MacOSComputerUseError("A0_TAG_EMPTY_QUERY", "A0 Tag requires a request after the tag.")
        if len(query) > _TAG_QUERY_MAX_CHARS:
            raise MacOSComputerUseError(
                "A0_TAG_QUERY_TOO_LONG",
                "A0 Tag requests are limited to 2048 characters.",
            )
        profile = str(match.group("profile") or "")
        if profile and not _TAG_PROFILE_RE.fullmatch(profile):
            raise MacOSComputerUseError("A0_TAG_INVALID_PROFILE", "The A0 Tag profile key is invalid.")
        original = str(match.group("tag") or "")
        start = line_start + _utf16_length(str(match.group("indent") or ""))
        end = start + _utf16_length(original)
        context_start = max(0, start - _TAG_TEXT_WINDOW_CHARS)
        context_end = min(character_count, end + _TAG_TEXT_WINDOW_CHARS)
        _, _, context_before = self._tag_bounded_text_range(
            accessibility,
            element,
            context_start,
            start,
            trim_start=True,
        )
        _, _, context_after = self._tag_bounded_text_range(
            accessibility,
            element,
            end,
            context_end,
            trim_start=False,
        )
        focused_context = context_before + original + context_after
        return start, end, caret, original, query, profile, focused_context

    def _tag_text_count(self, accessibility: Any, element: Any) -> int:
        value = _ax_copy_attribute(
            accessibility,
            element,
            "kAXNumberOfCharactersAttribute",
            "AXNumberOfCharacters",
        )
        try:
            return int(value)
        except (TypeError, ValueError):
            raise MacOSComputerUseError(
                "A0_TAG_TEXT_UNAVAILABLE",
                "The focused field does not expose bounded readable text.",
            ) from None

    def _tag_selected_range(self, accessibility: Any, element: Any) -> tuple[int, int]:
        value = _ax_copy_attribute(
            accessibility,
            element,
            "kAXSelectedTextRangeAttribute",
            "AXSelectedTextRange",
        )
        if isinstance(value, (list, tuple)) and len(value) >= 2:
            try:
                return int(value[0]), int(value[1])
            except (TypeError, ValueError):
                pass
        get_value = getattr(accessibility, "AXValueGetValue", None)
        range_type = getattr(accessibility, "kAXValueCFRangeType", 4)
        if callable(get_value) and value is not None:
            try:
                result = get_value(value, range_type, None)
                success, native_range = result if isinstance(result, tuple) and len(result) >= 2 else (False, None)
                if success and isinstance(native_range, (list, tuple)) and len(native_range) >= 2:
                    return int(native_range[0]), int(native_range[1])
            except Exception:
                pass
        raise MacOSComputerUseError(
            "A0_TAG_TEXT_UNAVAILABLE",
            "The focused field does not expose a readable caret range.",
        )

    def _tag_range_value(self, accessibility: Any, start: int, length: int) -> Any:
        create_value = getattr(accessibility, "AXValueCreate", None)
        if callable(create_value):
            try:
                return create_value(getattr(accessibility, "kAXValueCFRangeType", 4), (start, length))
            except Exception:
                pass
        return (start, length)

    def _tag_text_range(self, accessibility: Any, element: Any, start: int, end: int) -> str:
        if start < 0 or end < start:
            raise MacOSComputerUseError("A0_TAG_TEXT_UNAVAILABLE", "The focused text range is invalid.")
        copy_value = getattr(accessibility, "AXUIElementCopyParameterizedAttributeValue", None)
        if not callable(copy_value):
            raise MacOSComputerUseError(
                "A0_TAG_TEXT_UNAVAILABLE",
                "The focused field does not expose bounded readable text.",
            )
        attribute = _ax_constant(
            accessibility,
            "kAXStringForRangeParameterizedAttribute",
            "AXStringForRange",
        )
        native_range = self._tag_range_value(accessibility, start, end - start)
        try:
            result = copy_value(element, attribute, native_range, None)
        except TypeError:
            result = copy_value(element, attribute, native_range)
        except Exception as exc:
            raise MacOSComputerUseError(
                "A0_TAG_TEXT_UNAVAILABLE",
                "The focused field rejected a bounded text read.",
            ) from exc
        error_code, value = _ax_result_value(result)
        if error_code != 0 or value is None:
            raise MacOSComputerUseError(
                "A0_TAG_TEXT_UNAVAILABLE",
                "The focused field rejected a bounded text read.",
            )
        return str(value)

    def _tag_bounded_text_range(
        self,
        accessibility: Any,
        element: Any,
        start: int,
        end: int,
        *,
        trim_start: bool,
    ) -> tuple[int, int, str]:
        try:
            return start, end, self._tag_text_range(accessibility, element, start, end)
        except MacOSComputerUseError:
            if start >= end:
                raise
        if trim_start:
            start += 1
        else:
            end -= 1
        return start, end, self._tag_text_range(accessibility, element, start, end)

    def _tag_element_protected(self, accessibility: Any, element: Any) -> bool:
        role = str(_ax_copy_attribute(accessibility, element, "kAXRoleAttribute", "AXRole") or "")
        subrole = str(_ax_copy_attribute(accessibility, element, "kAXSubroleAttribute", "AXSubrole") or "")
        protected = _ax_copy_attribute(
            accessibility,
            element,
            "kAXProtectedContentAttribute",
            "AXProtectedContent",
        )
        semantic_role = f"{role} {subrole}".casefold()
        return bool(protected) or "password" in semantic_role or "secure" in semantic_role

    def _tag_element_editable(self, accessibility: Any, element: Any) -> bool:
        enabled = _ax_copy_attribute(accessibility, element, "kAXEnabledAttribute", "AXEnabled")
        if enabled is False:
            return False
        return self._ax_attribute_settable(
            accessibility,
            element,
            _ax_constant(accessibility, "kAXSelectedTextRangeAttribute", "AXSelectedTextRange"),
        ) and self._ax_attribute_settable(
            accessibility,
            element,
            _ax_constant(accessibility, "kAXSelectedTextAttribute", "AXSelectedText"),
        )

    def _ax_attribute_settable(self, accessibility: Any, element: Any, attribute: Any) -> bool:
        is_settable = getattr(accessibility, "AXUIElementIsAttributeSettable", None)
        if not callable(is_settable):
            return False
        try:
            result = is_settable(element, attribute, None)
        except TypeError:
            result = is_settable(element, attribute)
        except Exception:
            return False
        error_code, value = _ax_result_value(result)
        return error_code == 0 and bool(value)

    def _ax_elements_equal(self, accessibility: Any, left: Any, right: Any) -> bool:
        if left is right:
            return True
        equal = getattr(accessibility, "CFEqual", None)
        if callable(equal):
            with contextlib.suppress(Exception):
                return bool(equal(left, right))
        with contextlib.suppress(Exception):
            return bool(left == right)
        return False

    def _tag_window_screenshot(
        self,
        target: _TagTarget,
    ) -> tuple[str, str, dict[str, str] | None]:
        if (
            not all(math.isfinite(item) for item in target.window_bounds)
            or target.window_bounds[2] <= 0
            or target.window_bounds[3] <= 0
        ):
            return (
                "unavailable",
                "macOS Accessibility did not expose verified active-window bounds; A0 Tag continued without a screenshot.",
                None,
            )
        try:
            quartz = _load_quartz_module()
        except MacOSComputerUseError as exc:
            return "unavailable", str(exc), None
        try:
            screen_recording_granted = self._screen_recording_granted(quartz)
        except Exception:
            return (
                "unavailable",
                "macOS Screen Recording permission could not be verified; A0 Tag continued without a screenshot.",
                None,
            )
        if not screen_recording_granted:
            return (
                "unavailable",
                "macOS Screen Recording permission is unavailable; A0 Tag continued without a screenshot.",
                None,
            )
        capture_window = getattr(self._driver, "capture_window_png", None)
        if not callable(capture_window):
            return (
                "unavailable",
                "The macOS backend cannot capture a verified active window; A0 Tag continued without a screenshot.",
                None,
            )
        try:
            png_bytes, _width, _height, _window_number = capture_window(
                pid=target.pid,
                bounds=target.window_bounds,
                title=target.window_title,
            )
        except MacOSComputerUseError as exc:
            return "unavailable", str(exc), None
        except Exception:
            return (
                "unavailable",
                "The verified active-window screenshot failed; A0 Tag continued without a screenshot.",
                None,
            )
        if not png_bytes.startswith(b"\x89PNG\r\n\x1a\n") or len(png_bytes) > _TAG_SCREENSHOT_MAX_BYTES:
            return (
                "unavailable",
                "The verified active-window screenshot was invalid or too large; A0 Tag continued without it.",
                None,
            )
        return (
            "attached",
            "",
            {
                "encoding": "base64",
                "mime": "image/png",
                "filename": "a0-tag-window.png",
                "data": base64.b64encode(png_bytes).decode("ascii"),
            },
        )

    def _replace_tag_text(
        self,
        accessibility: Any,
        target: _TagTarget,
        replacement: str,
    ) -> None:
        original_count = self._tag_text_count(accessibility, target.element)
        replacement_length = _utf16_length(replacement)
        replacement_caret = target.start + replacement_length + (target.caret - target.end)
        expected_count = original_count - (target.end - target.start) + replacement_length
        range_attribute = _ax_constant(
            accessibility,
            "kAXSelectedTextRangeAttribute",
            "AXSelectedTextRange",
        )
        text_attribute = _ax_constant(accessibility, "kAXSelectedTextAttribute", "AXSelectedText")
        if self._set_ax_attribute(
            accessibility,
            target.element,
            range_attribute,
            self._tag_range_value(accessibility, target.start, target.end - target.start),
        ) != 0:
            raise MacOSComputerUseError(
                "A0_TAG_REPLACE_FAILED",
                "The focused field rejected exact range selection.",
            )
        write_error = self._set_ax_attribute(accessibility, target.element, text_attribute, replacement)
        try:
            actual_count = self._tag_text_count(accessibility, target.element)
            actual = self._tag_text_range(
                accessibility,
                target.element,
                target.start,
                target.start + replacement_length,
            )
        except MacOSComputerUseError:
            actual_count = expected_count
            actual = ""
        caret_error = self._set_ax_attribute(
            accessibility,
            target.element,
            range_attribute,
            self._tag_range_value(accessibility, replacement_caret, 0),
        )
        caret_ok = False
        if caret_error == 0:
            with contextlib.suppress(MacOSComputerUseError):
                caret_ok = self._tag_selected_range(accessibility, target.element) == (
                    replacement_caret,
                    0,
                )
        if write_error == 0 and actual_count == expected_count and actual == replacement and caret_ok:
            return

        self._restore_tag_text(
            accessibility,
            target,
            current_count=actual_count,
            original_count=original_count,
        )
        raise MacOSComputerUseError(
            "A0_TAG_REPLACE_FAILED",
            "The field changed or rejected the replacement; the original tag was restored where possible.",
        )

    def _restore_tag_text(
        self,
        accessibility: Any,
        target: _TagTarget,
        *,
        current_count: int,
        original_count: int,
    ) -> None:
        inserted_length = current_count - (original_count - (target.end - target.start))
        if inserted_length < 0 or inserted_length > _TAG_REPLACEMENT_MAX_CHARS * 2:
            return
        range_attribute = _ax_constant(
            accessibility,
            "kAXSelectedTextRangeAttribute",
            "AXSelectedTextRange",
        )
        text_attribute = _ax_constant(accessibility, "kAXSelectedTextAttribute", "AXSelectedText")
        if self._set_ax_attribute(
            accessibility,
            target.element,
            range_attribute,
            self._tag_range_value(accessibility, target.start, inserted_length),
        ) != 0:
            return
        if self._set_ax_attribute(accessibility, target.element, text_attribute, target.original) != 0:
            return
        self._set_ax_attribute(
            accessibility,
            target.element,
            range_attribute,
            self._tag_range_value(accessibility, target.caret, 0),
        )

    def _frontmost_ax_root(self, accessibility: Any) -> tuple[dict[str, Any], Any]:
        try:
            appkit = _load_appkit_module()
            application = appkit.NSWorkspace.sharedWorkspace().frontmostApplication()
        except Exception as exc:
            raise MacOSComputerUseError(
                "COMPUTER_USE_AX_UNAVAILABLE",
                "Unable to inspect the frontmost macOS application.",
            ) from exc

        app_info: dict[str, Any] = {}
        pid = None
        if application is not None:
            for key, method_name in (
                ("name", "localizedName"),
                ("bundle_id", "bundleIdentifier"),
                ("pid", "processIdentifier"),
            ):
                method = getattr(application, method_name, None)
                if callable(method):
                    with contextlib.suppress(Exception):
                        value = method()
                        if value is not None:
                            app_info[key] = _ax_scalar(value)
            pid = app_info.get("pid")

        create_application = getattr(accessibility, "AXUIElementCreateApplication", None)
        if create_application is None or pid is None:
            create_system = getattr(accessibility, "AXUIElementCreateSystemWide", None)
            if create_system is None:
                raise MacOSComputerUseError(
                    "COMPUTER_USE_AX_UNAVAILABLE",
                    "macOS Accessibility root element is unavailable.",
                )
            return app_info, create_system()

        try:
            return app_info, create_application(int(pid))
        except Exception as exc:
            raise MacOSComputerUseError(
                "COMPUTER_USE_AX_UNAVAILABLE",
                "Unable to create a macOS Accessibility application element.",
            ) from exc

    def _ax_application_info(self, application: Any) -> dict[str, Any]:
        app_info: dict[str, Any] = {}
        if application is None:
            return app_info
        for key, method_name in (
            ("name", "localizedName"),
            ("bundle_id", "bundleIdentifier"),
            ("pid", "processIdentifier"),
            ("hidden", "isHidden"),
            ("active", "isActive"),
        ):
            method = getattr(application, method_name, None)
            if callable(method):
                with contextlib.suppress(Exception):
                    value = method()
                    if value is not None:
                        app_info[key] = _ax_scalar(value)
        return app_info

    def _ax_application_roots(self, accessibility: Any) -> list[tuple[dict[str, Any], Any]]:
        create_application = getattr(accessibility, "AXUIElementCreateApplication", None)
        if create_application is None:
            app_info, root = self._frontmost_ax_root(accessibility)
            return [(app_info, root)]

        applications: list[Any] = []
        try:
            appkit = _load_appkit_module()
            workspace = appkit.NSWorkspace.sharedWorkspace()
            running = getattr(workspace, "runningApplications", None)
            if callable(running):
                applications = list(running() or [])
            if not applications:
                frontmost = getattr(workspace, "frontmostApplication", None)
                if callable(frontmost):
                    application = frontmost()
                    if application is not None:
                        applications = [application]
        except Exception:
            applications = []

        roots: list[tuple[dict[str, Any], Any]] = []
        for application in applications:
            app_info = self._ax_application_info(application)
            pid = app_info.get("pid")
            if pid is None:
                continue
            with contextlib.suppress(Exception):
                roots.append((app_info, create_application(int(pid))))
        if roots:
            return roots
        app_info, root = self._frontmost_ax_root(accessibility)
        return [(app_info, root)]

    def _ax_window_roots(self, accessibility: Any) -> list[tuple[dict[str, Any], Any, Any, list[int]]]:
        windows: list[tuple[dict[str, Any], Any, Any, list[int]]] = []
        for app_info, app_root in self._ax_application_roots(accessibility):
            app_windows = self._ax_children(accessibility, app_root, root=True)
            if not app_windows:
                windows.append((app_info, app_root, app_root, []))
                continue
            for index, window in enumerate(app_windows):
                windows.append((app_info, app_root, window, [index]))
        return windows

    def _window_id_for_ax_window(self, app_info: dict[str, Any], *, path: list[int]) -> str:
        path_text = ".".join(str(item) for item in path)
        pid = app_info.get("pid")
        if pid not in (None, ""):
            return f"ax-pid:{pid}:path:{path_text}"
        bundle_id = str(app_info.get("bundle_id") or "").strip()
        if bundle_id:
            return f"ax-bundle:{bundle_id}:path:{path_text}"
        return f"ax-path:{path_text}"

    def _parse_ax_window_id(self, window_id: str) -> tuple[int | None, str | None, list[int] | None]:
        value = str(window_id or "").strip()
        if not value:
            return None, None, None
        pid: int | None = None
        bundle_id: str | None = None
        path_text = ""
        if value.startswith("ax-pid:") and ":path:" in value:
            pid_text, path_text = value.removeprefix("ax-pid:").split(":path:", 1)
            with contextlib.suppress(ValueError):
                pid = int(pid_text)
        elif value.startswith("ax-bundle:") and ":path:" in value:
            bundle_id, path_text = value.removeprefix("ax-bundle:").split(":path:", 1)
        elif value.startswith("ax-path:"):
            path_text = value.removeprefix("ax-path:")
        else:
            return None, None, None
        try:
            return pid, bundle_id, _normalize_ax_path(path_text)
        except Exception:
            return pid, bundle_id, None

    def _resolve_ax_window_root(
        self,
        accessibility: Any,
        params: dict[str, Any],
        *,
        screen_size: tuple[int, int],
    ) -> tuple[dict[str, Any], Any, list[int], dict[str, Any]]:
        requested_window_id = str(params.get("window_id") or "").strip()
        requested_pid = params.get("pid")
        parsed_pid, parsed_bundle, parsed_path = self._parse_ax_window_id(requested_window_id)
        if requested_pid is None and parsed_pid is not None:
            requested_pid = parsed_pid

        candidates = self._ax_window_roots(accessibility)
        for app_info, app_root, window, path in candidates:
            pid_matches = requested_pid is None or str(app_info.get("pid") or "") == str(requested_pid)
            bundle_matches = not parsed_bundle or str(app_info.get("bundle_id") or "") == parsed_bundle
            path_matches = parsed_path is None or path == parsed_path
            if pid_matches and bundle_matches and path_matches:
                summary = self._ax_target_summary(accessibility, window, path=path, screen_size=screen_size)
                return app_info, window, path, summary

        if not requested_window_id and requested_pid is None and candidates:
            app_info, _app_root, window, path = candidates[0]
            summary = self._ax_target_summary(accessibility, window, path=path, screen_size=screen_size)
            return app_info, window, path, summary

        raise MacOSComputerUseError(
            "COMPUTER_USE_WINDOW_NOT_FOUND",
            "No matching macOS Accessibility window was found.",
        )

    def _serialize_ax_element(
        self,
        accessibility: Any,
        element: Any,
        *,
        path: list[int],
        depth: int,
        max_depth: int,
        max_nodes: int,
        budget: dict[str, Any],
        screen_size: tuple[int, int],
    ) -> dict[str, Any] | None:
        if int(budget.get("count") or 0) >= max_nodes:
            budget["truncated"] = True
            return None
        budget["count"] = int(budget.get("count") or 0) + 1

        node: dict[str, Any] = {"path": list(path)}
        for output_key, attr_name, fallback in (
            ("role", "kAXRoleAttribute", "AXRole"),
            ("subrole", "kAXSubroleAttribute", "AXSubrole"),
            ("title", "kAXTitleAttribute", "AXTitle"),
            ("description", "kAXDescriptionAttribute", "AXDescription"),
            ("value", "kAXValueAttribute", "AXValue"),
            ("identifier", "kAXIdentifierAttribute", "AXIdentifier"),
            ("enabled", "kAXEnabledAttribute", "AXEnabled"),
            ("focused", "kAXFocusedAttribute", "AXFocused"),
        ):
            value = _ax_copy_attribute(accessibility, element, attr_name, fallback)
            if value is None or value == "":
                continue
            node[output_key] = _ax_scalar(value)

        frame = self._ax_frame(accessibility, element, screen_size=screen_size)
        if frame:
            node["frame"] = frame
        actions = _ax_copy_actions(accessibility, element)
        if actions:
            node["actions"] = actions

        if depth >= max_depth:
            return node

        children: list[dict[str, Any]] = []
        for index, child in enumerate(self._ax_children(accessibility, element, root=not path)):
            child_node = self._serialize_ax_element(
                accessibility,
                child,
                path=[*path, index],
                depth=depth + 1,
                max_depth=max_depth,
                max_nodes=max_nodes,
                budget=budget,
                screen_size=screen_size,
            )
            if child_node is not None:
                children.append(child_node)
            if bool(budget.get("truncated")):
                break
        if children:
            node["children"] = children
        return node

    def _ax_children(self, accessibility: Any, element: Any, *, root: bool = False) -> list[Any]:
        candidates: list[Any] = []
        if root:
            windows = _ax_copy_attribute(accessibility, element, "kAXWindowsAttribute", "AXWindows")
            candidates = _ax_iterable(windows)
        if not candidates:
            children = _ax_copy_attribute(accessibility, element, "kAXChildrenAttribute", "AXChildren")
            candidates = _ax_iterable(children)
        return candidates

    def _ax_frame(
        self,
        accessibility: Any,
        element: Any,
        *,
        screen_size: tuple[int, int],
    ) -> dict[str, Any] | None:
        position = _ax_point(
            _ax_copy_attribute(accessibility, element, "kAXPositionAttribute", "AXPosition")
        )
        size = _ax_size(_ax_copy_attribute(accessibility, element, "kAXSizeAttribute", "AXSize"))
        if position is None or size is None:
            return None
        x, y = position
        width, height = size
        frame: dict[str, Any] = {
            "x": round(x, 2),
            "y": round(y, 2),
            "width": round(width, 2),
            "height": round(height, 2),
        }
        screen_width, screen_height = screen_size
        if screen_width > 0 and screen_height > 0:
            frame["normalized"] = {
                "x": round(x / screen_width, 6),
                "y": round(y / screen_height, 6),
                "width": round(width / screen_width, 6),
                "height": round(height / screen_height, 6),
            }
        return frame

    def _resolve_ax_target(self, accessibility: Any, params: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
        target_value = params.get("target")
        target = dict(target_value) if isinstance(target_value, dict) else {}
        path = _normalize_ax_path(params.get("path", target.get("path")))
        _app_info, root = self._frontmost_ax_root(accessibility)
        screen_size = (0, 0)
        if self._session is not None:
            screen_size = (self._session.session.width, self._session.session.height)

        if path:
            element = self._ax_element_for_path(accessibility, root, path)
            if element is not None:
                summary = self._ax_target_summary(accessibility, element, path=path, screen_size=screen_size)
                if self._ax_summary_matches(summary, target, allow_empty=True):
                    return element, summary

        matches = self._find_ax_matches(accessibility, root, target=target, screen_size=screen_size)
        if not matches:
            raise MacOSComputerUseError(
                "COMPUTER_USE_AX_TARGET_NOT_FOUND",
                "No matching macOS Accessibility element was found.",
            )
        best_score = matches[0][0]
        best = [item for item in matches if item[0] == best_score]
        if len(best) > 1:
            previews = [item[2] for item in best[:5]]
            raise MacOSComputerUseError(
                "COMPUTER_USE_AX_TARGET_AMBIGUOUS",
                f"AX target matched {len(best)} elements. Narrow the target. Matches: {previews}",
            )
        _score, element, summary = best[0]
        return element, summary

    def _ax_element_for_path(self, accessibility: Any, root: Any, path: list[int]) -> Any | None:
        element = root
        for depth, index in enumerate(path):
            children = self._ax_children(accessibility, element, root=depth == 0)
            if index < 0 or index >= len(children):
                return None
            element = children[index]
        return element

    def _ax_app_root_for_window_id(self, accessibility: Any, window_id: str) -> tuple[dict[str, Any], Any]:
        parsed_pid, parsed_bundle, _parsed_path = self._parse_ax_window_id(window_id)
        candidates = self._ax_application_roots(accessibility)
        for app_info, app_root in candidates:
            pid_matches = parsed_pid is None or str(app_info.get("pid") or "") == str(parsed_pid)
            bundle_matches = not parsed_bundle or str(app_info.get("bundle_id") or "") == parsed_bundle
            if pid_matches and bundle_matches:
                return app_info, app_root
        if not window_id and candidates:
            return candidates[0][0], candidates[0][1]
        raise MacOSComputerUseError(
            "COMPUTER_USE_WINDOW_NOT_FOUND",
            "No matching macOS Accessibility app/window root was found.",
        )

    def _ax_element_for_window_path(
        self,
        accessibility: Any,
        *,
        window_id: str,
        path: list[int],
    ) -> tuple[Any, dict[str, Any]]:
        app_info, app_root = self._ax_app_root_for_window_id(accessibility, window_id)
        element = self._ax_element_for_path(accessibility, app_root, path)
        if element is None:
            raise MacOSComputerUseError(
                "COMPUTER_USE_AX_TARGET_NOT_FOUND",
                "No matching macOS Accessibility element was found for the cached path.",
            )
        return element, app_info

    def _cache_element_indices(self, tree: dict[str, Any], *, window_id: str) -> None:
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

    def _resolve_element_action_target(self, accessibility: Any, params: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
        screen_size = (0, 0)
        if self._session is not None:
            screen_size = (self._session.session.width, self._session.session.height)
        element_index = params.get("element_index")
        if element_index is not None:
            try:
                index = int(element_index)
            except (TypeError, ValueError) as exc:
                raise MacOSComputerUseError(
                    "COMPUTER_USE_BAD_ELEMENT_INDEX",
                    "element_index must be an integer from the latest get_window_state.",
                ) from exc
            cached = self._element_index_cache.get(index)
            if cached is None:
                raise MacOSComputerUseError(
                    "COMPUTER_USE_ELEMENT_INDEX_STALE",
                    "element_index was not found. Call get_window_state and retry with a fresh index.",
                )
            requested_window_id = str(params.get("window_id") or "").strip()
            cached_window_id = str(cached.get("window_id") or "").strip()
            if requested_window_id and requested_window_id != cached_window_id:
                raise MacOSComputerUseError(
                    "COMPUTER_USE_ELEMENT_WINDOW_MISMATCH",
                    "element_index belongs to a different cached window_id.",
                )
            path = _normalize_ax_path(cached.get("path"))
            element, _app_info = self._ax_element_for_window_path(
                accessibility,
                window_id=cached_window_id,
                path=path,
            )
            summary = self._ax_target_summary(accessibility, element, path=path, screen_size=screen_size)
            summary["element_index"] = index
            return element, summary

        target_value = params.get("target")
        target = dict(target_value) if isinstance(target_value, dict) else {}
        path = _normalize_ax_path(params.get("path", target.get("path")))
        window_id = str(params.get("window_id") or "").strip()
        if window_id and path:
            element, _app_info = self._ax_element_for_window_path(
                accessibility,
                window_id=window_id,
                path=path,
            )
            summary = self._ax_target_summary(accessibility, element, path=path, screen_size=screen_size)
            if self._ax_summary_matches(summary, target, allow_empty=True):
                return element, summary
        return self._resolve_ax_target(accessibility, params)

    def _find_ax_matches(
        self,
        accessibility: Any,
        root: Any,
        *,
        target: dict[str, Any],
        screen_size: tuple[int, int],
    ) -> list[tuple[int, Any, dict[str, Any]]]:
        if not any(str(target.get(key) or "").strip() for key in ("role", "title", "description", "value", "identifier", "subrole")):
            raise MacOSComputerUseError(
                "COMPUTER_USE_AX_TARGET_REQUIRED",
                "ax_action requires path or a semantic target.",
            )
        matches: list[tuple[int, Any, dict[str, Any]]] = []
        queue: list[tuple[Any, list[int], int]] = [(root, [], 0)]
        visited = 0
        while queue and visited < _AX_HARD_MAX_NODES:
            element, path, depth = queue.pop(0)
            visited += 1
            summary = self._ax_target_summary(accessibility, element, path=path, screen_size=screen_size)
            score = self._ax_match_score(summary, target)
            if score > 0:
                matches.append((score, element, summary))
            if depth >= _AX_HARD_MAX_DEPTH:
                continue
            for index, child in enumerate(self._ax_children(accessibility, element, root=not path)):
                queue.append((child, [*path, index], depth + 1))
        return sorted(matches, key=lambda item: item[0], reverse=True)

    def _ax_target_summary(
        self,
        accessibility: Any,
        element: Any,
        *,
        path: list[int],
        screen_size: tuple[int, int],
    ) -> dict[str, Any]:
        summary: dict[str, Any] = {"path": list(path)}
        for output_key, attr_name, fallback in (
            ("role", "kAXRoleAttribute", "AXRole"),
            ("subrole", "kAXSubroleAttribute", "AXSubrole"),
            ("title", "kAXTitleAttribute", "AXTitle"),
            ("description", "kAXDescriptionAttribute", "AXDescription"),
            ("value", "kAXValueAttribute", "AXValue"),
            ("identifier", "kAXIdentifierAttribute", "AXIdentifier"),
            ("enabled", "kAXEnabledAttribute", "AXEnabled"),
            ("focused", "kAXFocusedAttribute", "AXFocused"),
        ):
            value = _ax_copy_attribute(accessibility, element, attr_name, fallback)
            if value is not None and value != "":
                summary[output_key] = _ax_scalar(value)
        frame = self._ax_frame(accessibility, element, screen_size=screen_size)
        if frame:
            summary["frame"] = frame
        actions = _ax_copy_actions(accessibility, element)
        if actions:
            summary["actions"] = actions
        return summary

    def _ax_summary_matches(
        self,
        summary: dict[str, Any],
        target: dict[str, Any],
        *,
        allow_empty: bool = False,
    ) -> bool:
        if allow_empty and not target:
            return True
        return self._ax_match_score(summary, target) > 0

    def _ax_match_score(self, summary: dict[str, Any], target: dict[str, Any]) -> int:
        score = 0
        used = 0
        for key, weight in (
            ("identifier", 80),
            ("role", 20),
            ("subrole", 15),
            ("title", 50),
            ("description", 35),
            ("value", 25),
        ):
            expected = str(target.get(key) or "").strip().lower()
            if not expected:
                continue
            used += 1
            actual = str(summary.get(key) or "").strip().lower()
            if not actual:
                return 0
            if actual == expected:
                score += weight
            elif key in {"title", "description", "value"} and expected in actual:
                score += max(1, weight // 2)
            else:
                return 0
        return score if used else 0

    def _ax_summary_is_offscreen(self, summary: dict[str, Any]) -> bool:
        frame = summary.get("frame")
        if not isinstance(frame, dict):
            return False
        normalized = frame.get("normalized")
        if not isinstance(normalized, dict):
            return False
        try:
            x = float(normalized.get("x"))
            y = float(normalized.get("y"))
            width = float(normalized.get("width"))
            height = float(normalized.get("height"))
        except (TypeError, ValueError):
            return False
        return x + width <= 0 or y + height <= 0 or x >= 1 or y >= 1

    def _background_unavailable(
        self,
        *,
        session_id: str,
        context_id: str,
        target: dict[str, Any],
        operation: str,
        reason: str,
        requested_dispatch: str = "background",
    ) -> dict[str, Any]:
        return {
            "session_id": session_id,
            "context_id": context_id,
            "operation": operation,
            "target": target,
            "requested_dispatch": requested_dispatch,
            "actual_dispatch": "none",
            "background_unavailable": True,
            "reason": reason,
        }

    def _try_background_ax_action(
        self,
        accessibility: Any,
        element: Any,
        *,
        target: dict[str, Any],
        operation: str,
        params: dict[str, Any],
        session_id: str,
        context_id: str,
        requested_dispatch: str,
    ) -> dict[str, Any]:
        if operation == "focus":
            return self._background_unavailable(
                session_id=session_id,
                context_id=context_id,
                target=target,
                operation=operation,
                reason="AX focus changes active UI state and is treated as foreground dispatch.",
                requested_dispatch=requested_dispatch,
            )
        if operation not in {"press", "set_value"}:
            return self._background_unavailable(
                session_id=session_id,
                context_id=context_id,
                target=target,
                operation=operation,
                reason=f"operation {operation!r} is not supported for background dispatch.",
                requested_dispatch=requested_dispatch,
            )
        if operation == "set_value" and coerce_bool(params.get("submit")):
            return self._background_unavailable(
                session_id=session_id,
                context_id=context_id,
                target=target,
                operation=operation,
                reason="submit requires keyboard input and cannot be guaranteed in background.",
                requested_dispatch=requested_dispatch,
            )
        try:
            result = self._perform_ax_element_action(
                accessibility,
                element,
                target=target,
                operation=operation,
                params=params,
                session_id=session_id,
                context_id=context_id,
            )
        except MacOSComputerUseError as exc:
            return self._background_unavailable(
                session_id=session_id,
                context_id=context_id,
                target=target,
                operation=operation,
                reason=str(exc),
                requested_dispatch=requested_dispatch,
            )
        result["requested_dispatch"] = requested_dispatch
        result["actual_dispatch"] = "background"
        result["background_unavailable"] = False
        return result

    def _perform_ax_element_action(
        self,
        accessibility: Any,
        element: Any,
        *,
        target: dict[str, Any],
        operation: str,
        params: dict[str, Any],
        session_id: str,
        context_id: str,
    ) -> dict[str, Any]:
        if operation not in {"press", "focus", "set_value"}:
            raise MacOSComputerUseError(
                "COMPUTER_USE_BAD_AX_ACTION",
                "element_action operation must be one of: press, focus, set_value.",
            )
        if operation == "press":
            action_name = _ax_constant(accessibility, "kAXPressAction", "AXPress")
            error_code = self._perform_ax_action(accessibility, element, action_name)
            if error_code != 0:
                raise MacOSComputerUseError(
                    "COMPUTER_USE_AX_ACTION_FAILED",
                    f"AX press failed with error {error_code}.",
                )
        elif operation == "focus":
            focused_attr = _ax_constant(accessibility, "kAXFocusedAttribute", "AXFocused")
            error_code = self._set_ax_attribute(accessibility, element, focused_attr, True)
            if error_code != 0:
                raise MacOSComputerUseError(
                    "COMPUTER_USE_AX_ACTION_FAILED",
                    f"AX focus failed with error {error_code}.",
                )
        else:
            value = params.get("value", params.get("text"))
            if value is None:
                raise MacOSComputerUseError(
                    "COMPUTER_USE_AX_VALUE_REQUIRED",
                    "element_action set_value requires value or text.",
                )
            value_attr = _ax_constant(accessibility, "kAXValueAttribute", "AXValue")
            error_code = self._set_ax_attribute(accessibility, element, value_attr, str(value))
            if error_code != 0:
                raise MacOSComputerUseError(
                    "COMPUTER_USE_AX_ACTION_FAILED",
                    f"AX set_value failed with error {error_code}.",
                )
        return {
            "session_id": session_id,
            "context_id": context_id,
            "operation": operation,
            "target": target,
        }

    def _perform_ax_action(self, accessibility: Any, element: Any, action_name: Any) -> int:
        perform_action = getattr(accessibility, "AXUIElementPerformAction")
        try:
            result = perform_action(element, action_name)
        except Exception:
            return 1
        error_code, _value = _ax_result_value(result)
        return error_code

    def _set_ax_attribute(self, accessibility: Any, element: Any, attribute: Any, value: Any) -> int:
        set_attribute = getattr(accessibility, "AXUIElementSetAttributeValue", None)
        if set_attribute is None:
            return 1
        try:
            result = set_attribute(element, attribute, value)
        except Exception:
            return 1
        error_code, _value = _ax_result_value(result)
        return error_code

    def _ensure_accessibility_permission(
        self,
        *,
        allow_prompt: bool,
        timeout: float,
        allow: bool,
    ) -> None:
        accessibility = _load_accessibility_module()
        trusted = self._accessibility_trusted(accessibility, prompt=allow_prompt)
        _emit_debug(
            "accessibility.check",
            allow_prompt=allow_prompt,
            trusted=trusted,
            timeout=timeout,
            allow=allow,
        )
        if trusted:
            return

        if allow_prompt and timeout > 0:
            deadline = time.monotonic() + timeout
            poll_count = 0
            started_at = time.monotonic()
            while time.monotonic() < deadline:
                time.sleep(1.0)
                poll_count += 1
                trusted = self._accessibility_trusted(accessibility, prompt=False)
                if poll_count == 1 or poll_count % 5 == 0 or trusted:
                    _emit_debug(
                        "accessibility.poll",
                        poll_count=poll_count,
                        trusted=trusted,
                        elapsed_seconds=round(time.monotonic() - started_at, 1),
                    )
                if trusted:
                    return

        if allow:
            raise MacOSComputerUseError(
                "COMPUTER_USE_REARM_REQUIRED",
                "macOS Accessibility permission is not available. "
                f"{_MACOS_ACCESSIBILITY_MANUAL_APPROVAL}",
            )
        raise MacOSComputerUseError(
            "COMPUTER_USE_APPROVAL_REQUIRED",
            "macOS Accessibility permission is required. "
            f"{_MACOS_ACCESSIBILITY_MANUAL_APPROVAL}",
        )

    def _probe_capture_dimensions(
        self,
        *,
        allow_prompt: bool,
        timeout: float,
        allow: bool,
    ) -> tuple[int, int]:
        deadline = time.monotonic() + max(timeout, 0.0)
        attempt = 0
        started_at = time.monotonic()
        while True:
            attempt += 1
            try:
                _emit_debug(
                    "capture_probe.attempt",
                    attempt=attempt,
                    allow_prompt=allow_prompt,
                    allow=allow,
                )
                _png_bytes, width, height = self._driver.capture_png()
                _emit_debug(
                    "capture_probe.success",
                    attempt=attempt,
                    width=width,
                    height=height,
                    elapsed_seconds=round(time.monotonic() - started_at, 1),
                )
                return width, height
            except MacOSComputerUseError as exc:
                remaining_seconds = max(0.0, deadline - time.monotonic())
                _emit_debug(
                    "capture_probe.error",
                    attempt=attempt,
                    code=exc.code,
                    error=str(exc),
                    remaining_seconds=round(remaining_seconds, 1),
                )
                if allow_prompt and time.monotonic() < deadline:
                    time.sleep(1.0)
                    continue
                if allow:
                    raise MacOSComputerUseError(
                        "COMPUTER_USE_REARM_REQUIRED",
                        "Silent screen capture was not available. "
                        f"{_MACOS_SCREEN_RECORDING_MANUAL_APPROVAL}",
                    ) from exc
                if exc.code == "COMPUTER_USE_CAPTURE_UNAVAILABLE":
                    raise MacOSComputerUseError(
                        "COMPUTER_USE_APPROVAL_REQUIRED",
                        "macOS Screen Recording permission is required. "
                        f"{_MACOS_SCREEN_RECORDING_MANUAL_APPROVAL}",
                    ) from exc
                raise

    def _accessibility_trusted(self, accessibility: Any, *, prompt: bool) -> bool:
        if prompt:
            try:
                options = {accessibility.kAXTrustedCheckOptionPrompt: True}
                return bool(accessibility.AXIsProcessTrustedWithOptions(options))
            except Exception:
                return bool(accessibility.AXIsProcessTrusted())
        return bool(accessibility.AXIsProcessTrusted())

    def _screen_recording_granted(self, quartz: Any) -> bool:
        preflight = getattr(quartz, "CGPreflightScreenCaptureAccess", None)
        if callable(preflight):
            return bool(preflight())
        try:
            self._driver.capture_png()
        except MacOSComputerUseError:
            return False
        return True

    def _require_session(self, params: dict[str, Any]) -> _RuntimeSession:
        context_id = normalize_context_id(params.get("context_id"))
        session = self._session
        if session is None or not session.session.active or session.session.context_id != context_id:
            raise MacOSComputerUseError(
                "COMPUTER_USE_SESSION_REQUIRED",
                "No computer-use session is active.",
            )

        requested_session_id = str(params.get("session_id", "")).strip()
        if requested_session_id and requested_session_id != session.session.session_id:
            raise MacOSComputerUseError(
                "COMPUTER_USE_SESSION_MISMATCH",
                "Requested session_id does not match the active computer-use session.",
            )
        return session

    def close(self) -> None:
        self._tag_target = None
        if self._session is not None and self._session.session.active:
            self.stop_session({"context_id": self._session.session.context_id})


def _build_error_response(
    request_id: str,
    error: MacOSComputerUseError,
) -> dict[str, Any]:
    payload = {
        "request_id": request_id,
        "ok": False,
        "error": str(error),
        "code": error.code,
    }
    if error.result is not None:
        payload["result"] = error.result
    return payload


def serve_stdio(runtime: MacOSComputerUseRuntime | None = None) -> int:
    runtime = runtime or MacOSComputerUseRuntime()
    try:
        while True:
            raw_line = sys.stdin.readline()
            if not raw_line:
                break
            try:
                request = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                response = {
                    "request_id": "",
                    "ok": False,
                    "error": f"Invalid JSON: {exc}",
                    "code": "INVALID_JSON",
                }
                sys.stdout.write(json.dumps(response) + "\n")
                sys.stdout.flush()
                continue

            if not isinstance(request, dict):
                response = {
                    "request_id": "",
                    "ok": False,
                    "error": "Request must be a JSON object.",
                    "code": "INVALID_REQUEST",
                }
                sys.stdout.write(json.dumps(response) + "\n")
                sys.stdout.flush()
                continue

            request_id = str(request.get("request_id", "") or "")
            action = str(request.get("action", "") or "").strip().lower()
            if action == "shutdown":
                break

            try:
                with contextlib.redirect_stdout(sys.stderr):
                    if action in {
                        "permission_status",
                        "request_accessibility",
                        "request_screen_recording",
                        "start_session",
                        "status",
                        "capture",
                        "tag_context",
                        "tag_replace",
                        "tag_release",
                        "list_windows",
                        "get_window_state",
                        "element_action",
                        "ax_snapshot",
                        "ax_action",
                        "move",
                        "click",
                        "scroll",
                        "key",
                        "type",
                        "stop_session",
                    }:
                        if action not in {
                            "start_session",
                            "status",
                            "stop_session",
                            "tag_context",
                            "tag_replace",
                            "tag_release",
                        }:
                            request = normalize_action_payload(
                                action,
                                request,
                                context_id=normalize_context_id(request.get("context_id")),
                            )
                        result = runtime.dispatch(action, request)
                        response = {
                            "request_id": request_id,
                            "ok": True,
                            "result": result,
                        }
                    else:
                        raise MacOSComputerUseError(
                            "UNKNOWN_METHOD",
                            f"Unknown computer-use helper method: {action}",
                        )
            except MacOSComputerUseError as exc:
                response = _build_error_response(request_id, exc)
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
        runtime.close()
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
