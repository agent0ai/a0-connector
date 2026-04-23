from __future__ import annotations

import argparse
import base64
import contextlib
import json
import os
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
    normalize_restore_token,
    resolve_trust_mode_policy,
    safe_context_segment,
)

_DEBUG_ENV = "A0_COMPUTER_USE_DEBUG"
_DEBUG_LOG_ENV = "A0_COMPUTER_USE_DEBUG_LOG"

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


@dataclass(frozen=True)
class _ResolvedKey:
    keycode: int
    requires_shift: bool = False
    modifier_flag_name: str = ""


class _MacOSDesktopAutomation:
    def __init__(self) -> None:
        self._quartz = _load_quartz_module()
        self._event_source = None

    def screen_size(self) -> tuple[int, int]:
        _png_bytes, width, height = self.capture_png()
        return width, height

    def capture_png(self) -> tuple[bytes, int, int]:
        screencapture = shutil.which("screencapture")
        if not screencapture:
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
            _emit_debug("driver.capture_png.ok", width=width, height=height, bytes=len(png_bytes))
            return png_bytes, width, height
        except FileNotFoundError as exc:
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

    def click(self, x: float, y: float, *, button: str, count: int) -> None:
        quartz = self._quartz
        point = (float(x), float(y))
        button_value, down_type, up_type = self._mouse_button_spec(button)
        self.move(x, y)
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

    @property
    def supported(self) -> bool:
        return macos_backend_supported()

    def hello_metadata(self) -> dict[str, Any]:
        return {
            "supported": self.supported,
            "backend_id": MACOS_BACKEND_ID,
            "backend_family": MACOS_BACKEND_FAMILY,
            "features": list(MACOS_BACKEND_FEATURES),
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
            "support_reason": macos_backend_support_reason(),
        }

    def dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        handlers = {
            "start_session": self.start_session,
            "status": self.status,
            "capture": self.capture,
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
        if normalized_method in {"capture", "move", "click", "scroll", "key", "type"}:
            normalized_params = normalize_action_payload(
                normalized_method,
                normalized_params,
                context_id=normalize_context_id(normalized_params.get("context_id")),
            )
        return handler(normalized_params)

    def start_session(self, params: dict[str, Any]) -> dict[str, Any]:
        trust_mode = str(params.get("trust_mode") or "persistent").strip().lower()
        context_id = normalize_context_id(params.get("context_id"))
        restore_token = normalize_restore_token(params.get("restore_token"))
        allow_prompt = bool(params.get("allow_prompt", trust_mode != "free_run"))
        request_timeout = float(params.get("request_timeout_seconds") or 0.0)
        free_run = trust_mode == "free_run" or not allow_prompt
        policy = resolve_trust_mode_policy(trust_mode, restore_token)
        _emit_debug(
            "start_session.begin",
            context_id=context_id,
            trust_mode=trust_mode,
            allow_prompt=allow_prompt,
            request_timeout_seconds=request_timeout,
            free_run=free_run,
            restore_token_present=bool(restore_token),
        )

        if policy.trust_mode not in MACOS_TRUST_MODES:
            raise MacOSComputerUseError(
                "COMPUTER_USE_UNSUPPORTED",
                f"Unsupported trust mode: {trust_mode!r}",
            )
        if policy.trust_mode == "free_run" and not policy.reuse_allowed:
            raise MacOSComputerUseError(
                "COMPUTER_USE_REARM_REQUIRED",
                "Free-run requires a stored restore token.",
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
            free_run=free_run,
        )
        _emit_debug("start_session.accessibility.ok", context_id=context_id)
        _emit_debug("start_session.capture_probe.begin", context_id=context_id)
        width, height = self._probe_capture_dimensions(
            allow_prompt=allow_prompt,
            timeout=request_timeout,
            free_run=free_run,
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
        capture_path_value = str(params.get("capture_path") or "").strip()
        if capture_path_value:
            capture_path = Path(capture_path_value)
            capture_path.parent.mkdir(parents=True, exist_ok=True)
            capture_path.write_bytes(png_bytes)
            result["capture_path"] = str(capture_path)
        elif self._capture_debug_dir is not None:
            debug_path = self._capture_debug_dir / safe_context_segment(session.session.context_id)
            debug_path.mkdir(parents=True, exist_ok=True)
            filename = f"{uuid.uuid4().hex}.png"
            capture_path = debug_path / filename
            capture_path.write_bytes(png_bytes)
            result["capture_path"] = str(capture_path)
            result["png_base64"] = base64.b64encode(png_bytes).decode("ascii")
        else:
            result["png_base64"] = base64.b64encode(png_bytes).decode("ascii")
        return result

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
        return {
            "session_id": session.session.session_id,
            "button": button_name,
            "count": count,
            "x": x,
            "y": y,
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
        session = self._session
        if session is not None and session.session.context_id == context_id:
            session.session.active = False
            session.session.updated_at = time.time()
            if session.policy.persist_metadata:
                self._store.put(session.session)
            self._session = None
        return {"active": False, "status": "stopped", "session_id": ""}

    def _ensure_accessibility_permission(
        self,
        *,
        allow_prompt: bool,
        timeout: float,
        free_run: bool,
    ) -> None:
        accessibility = _load_accessibility_module()
        trusted = self._accessibility_trusted(accessibility, prompt=allow_prompt)
        _emit_debug(
            "accessibility.check",
            allow_prompt=allow_prompt,
            trusted=trusted,
            timeout=timeout,
            free_run=free_run,
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

        if free_run:
            raise MacOSComputerUseError(
                "COMPUTER_USE_REARM_REQUIRED",
                "macOS Accessibility permission is not available. Re-arm computer use with Confirm with User.",
            )
        raise MacOSComputerUseError(
            "COMPUTER_USE_APPROVAL_REQUIRED",
            "macOS Accessibility permission is required.",
        )

    def _probe_capture_dimensions(
        self,
        *,
        allow_prompt: bool,
        timeout: float,
        free_run: bool,
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
                    free_run=free_run,
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
                if free_run:
                    raise MacOSComputerUseError(
                        "COMPUTER_USE_REARM_REQUIRED",
                        "Silent screen capture was not available. Re-arm computer use with Confirm with User.",
                    ) from exc
                if exc.code == "COMPUTER_USE_CAPTURE_UNAVAILABLE":
                    raise MacOSComputerUseError(
                        "COMPUTER_USE_APPROVAL_REQUIRED",
                        "macOS screen recording permission is required.",
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
                    if action in {"start_session", "status", "capture", "move", "click", "scroll", "key", "type", "stop_session"}:
                        if action not in {"start_session", "status", "stop_session"}:
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
