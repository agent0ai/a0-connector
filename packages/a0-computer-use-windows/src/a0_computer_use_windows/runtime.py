from __future__ import annotations

import argparse
import base64
import contextlib
import io
import json
import os
import sys
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

from a0_computer_use_windows.detection import (
    windows_backend_support_reason,
    windows_backend_supported,
)
from a0_computer_use_windows.shared import (
    CAPTURE_DEBUG_DIR_ENV,
    STATE_DIR_ENV,
    WINDOWS_BACKEND_FEATURES,
    WINDOWS_BACKEND_FAMILY,
    WINDOWS_BACKEND_ID,
    WINDOWS_TRUST_MODES,
    TrustModePolicy,
    coerce_bool,
    coerce_int,
    normalize_action_payload,
    normalize_context_id,
    normalize_restore_token,
    resolve_trust_mode_policy,
    safe_context_segment,
)


class WindowsComputerUseError(RuntimeError):
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


class WindowsDesktopDriver(Protocol):
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
class WindowsSession:
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
            "backend_id": WINDOWS_BACKEND_ID,
            "backend_family": WINDOWS_BACKEND_FAMILY,
            "features": list(WINDOWS_BACKEND_FEATURES),
            "supported": windows_backend_supported(),
            "support_reason": windows_backend_support_reason(),
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
    def from_record(cls, payload: dict[str, Any]) -> "WindowsSession":
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


class WindowsSessionStore:
    def __init__(self, state_dir: str | os.PathLike[str] | None = None) -> None:
        configured = str(state_dir or os.environ.get(STATE_DIR_ENV, "")).strip()
        if configured:
            self.state_dir = Path(configured)
        else:
            self.state_dir = Path.home() / ".a0" / "computer-use-windows"
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

    def get(self, context_id: str) -> WindowsSession | None:
        record = self._read_records().get(context_id)
        if record is None:
            return None
        return WindowsSession.from_record(record)

    def put(self, session: WindowsSession) -> None:
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


def _load_dxcam_module() -> Any:
    try:
        import dxcam  # type: ignore
    except Exception as exc:  # pragma: no cover - only exercised on Windows
        raise WindowsComputerUseError(
            "COMPUTER_USE_UNSUPPORTED",
            "dxcam is required for Windows computer-use capture.",
        ) from exc
    return dxcam


def _load_pywinauto_modules() -> tuple[Any, Any]:  # pragma: no cover - only exercised on Windows
    try:
        from pywinauto import keyboard, mouse
    except Exception as exc:
        raise WindowsComputerUseError(
            "COMPUTER_USE_UNSUPPORTED",
            "pywinauto is required for Windows computer-use input injection.",
        ) from exc
    return keyboard, mouse


def _load_desktop_module() -> Any:  # pragma: no cover - only exercised on Windows
    try:
        from pywinauto import Desktop
    except Exception as exc:
        raise WindowsComputerUseError(
            "COMPUTER_USE_UNSUPPORTED",
            "pywinauto is required for Windows desktop automation.",
        ) from exc
    return Desktop


class _WindowsDesktopAutomation:
    def __init__(self) -> None:
        self._camera: Any = None
        try:
            desktop_cls = _load_desktop_module()
            self._desktop = desktop_cls(backend="uia")
        except Exception as exc:  # pragma: no cover - only exercised on Windows
            raise WindowsComputerUseError(
                "COMPUTER_USE_UNSUPPORTED",
                "pywinauto UIA desktop automation could not initialize.",
            ) from exc

    def screen_size(self) -> tuple[int, int]:
        try:  # pragma: no cover - only exercised on Windows
            import ctypes

            user32 = ctypes.windll.user32
            return int(user32.GetSystemMetrics(0)), int(user32.GetSystemMetrics(1))
        except Exception as exc:
            raise WindowsComputerUseError(
                "COMPUTER_USE_CAPTURE_UNAVAILABLE",
                "Unable to read the Windows desktop dimensions.",
            ) from exc

    def capture_png(self) -> tuple[bytes, int, int]:
        dxcam = _load_dxcam_module()
        camera = self._camera
        if camera is None:
            # Prefer dxcam's NumPy processor so capture works without a cv2 dependency.
            camera = dxcam.create(output_idx=0, processor_backend="numpy")
            self._camera = camera
        if camera is None:
            raise WindowsComputerUseError(
                "COMPUTER_USE_CAPTURE_UNAVAILABLE",
                "dxcam could not initialize a capture session.",
            )

        frame = camera.grab()
        if frame is None:
            raise WindowsComputerUseError(
                "COMPUTER_USE_CAPTURE_UNAVAILABLE",
                "dxcam did not return a screen frame.",
            )

        from PIL import Image

        image = Image.fromarray(frame)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue(), int(image.width), int(image.height)

    def move(self, x: float, y: float) -> None:  # pragma: no cover - only exercised on Windows
        _, mouse = _load_pywinauto_modules()
        mouse.move(coords=(int(round(x)), int(round(y))))

    def click(self, x: float, y: float, *, button: str, count: int) -> None:  # pragma: no cover
        _, mouse = _load_pywinauto_modules()
        coords = (int(round(x)), int(round(y)))
        if count > 1 and button == "left":
            for _ in range(count):
                mouse.double_click(button="left", coords=coords)
            return
        for _ in range(count):
            mouse.click(button=button, coords=coords)

    def scroll(self, dx: int, dy: int) -> None:  # pragma: no cover - only exercised on Windows
        _, mouse = _load_pywinauto_modules()
        if dy:
            mouse.scroll(coords=None, wheel_dist=dy)
        if dx:
            mouse.scroll(coords=None, wheel_dist=dx, horiz=True)

    def key(self, keys: list[str]) -> None:  # pragma: no cover - only exercised on Windows
        keyboard, _ = _load_pywinauto_modules()
        keyboard.send_keys(_format_key_sequence(keys), pause=0.01, with_spaces=True)

    def type_text(self, text: str, *, submit: bool) -> None:  # pragma: no cover - Windows only
        keyboard, _ = _load_pywinauto_modules()
        keyboard.send_keys(text, pause=0.01, with_spaces=True)
        if submit:
            keyboard.send_keys("{ENTER}", pause=0.01, with_spaces=True)


def _normalize_key_token(key: str) -> str:
    aliases = {
        "alt": "ALT",
        "ctrl": "CTRL",
        "control": "CTRL",
        "delete": "DELETE",
        "down": "DOWN",
        "enter": "ENTER",
        "esc": "ESC",
        "escape": "ESC",
        "left": "LEFT",
        "pagedown": "PGDN",
        "pageup": "PGUP",
        "pgdn": "PGDN",
        "pgup": "PGUP",
        "right": "RIGHT",
        "shift": "SHIFT",
        "space": "SPACE",
        "super": "WIN",
        "tab": "TAB",
        "up": "UP",
        "backspace": "BACKSPACE",
    }
    cleaned = str(key or "").strip()
    if not cleaned:
        return ""
    if len(cleaned) == 1 and cleaned.isprintable():
        return cleaned
    return aliases.get(cleaned.lower(), cleaned.upper())


def _format_key_sequence(keys: list[str]) -> str:
    normalized = [_normalize_key_token(key) for key in keys if _normalize_key_token(key)]
    if not normalized:
        raise WindowsComputerUseError("COMPUTER_USE_KEYS_REQUIRED", "key requires a non-empty keys list.")
    if len(normalized) == 1:
        token = normalized[0]
        return token if len(token) == 1 else f"{{{token}}}"

    modifiers = normalized[:-1]
    body = normalized[-1]
    prefix = "".join(f"{{{modifier} down}}" for modifier in modifiers)
    suffix = "".join(f"{{{modifier} up}}" for modifier in reversed(modifiers))
    if len(body) == 1:
        return prefix + body + suffix
    return prefix + f"{{{body}}}" + suffix


def _load_default_driver() -> _WindowsDesktopAutomation:
    if not windows_backend_supported():  # pragma: no cover - only exercised on Windows
        raise WindowsComputerUseError("COMPUTER_USE_UNSUPPORTED", windows_backend_support_reason())
    return _WindowsDesktopAutomation()


@dataclass
class _RuntimeSession:
    session: WindowsSession
    policy: TrustModePolicy


class WindowsComputerUseRuntime:
    def __init__(
        self,
        *,
        driver: Any | None = None,
        store: WindowsSessionStore | None = None,
        state_dir: str | os.PathLike[str] | None = None,
        capture_debug_dir: str | os.PathLike[str] | None = None,
    ) -> None:
        self._driver = driver or _load_default_driver()
        self._store = store or WindowsSessionStore(state_dir=state_dir)
        self._capture_debug_dir = (
            Path(capture_debug_dir)
            if capture_debug_dir is not None
            else _default_capture_debug_dir()
        )
        self._session: _RuntimeSession | None = None

    @property
    def supported(self) -> bool:
        return windows_backend_supported()

    def hello_metadata(self) -> dict[str, Any]:
        return {
            "supported": self.supported,
            "backend_id": WINDOWS_BACKEND_ID,
            "backend_family": WINDOWS_BACKEND_FAMILY,
            "features": list(WINDOWS_BACKEND_FEATURES),
            "support_reason": windows_backend_support_reason(),
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
            "backend_id": WINDOWS_BACKEND_ID,
            "backend_family": WINDOWS_BACKEND_FAMILY,
            "features": list(WINDOWS_BACKEND_FEATURES),
            "support_reason": windows_backend_support_reason(),
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
            raise WindowsComputerUseError(
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
        policy = resolve_trust_mode_policy(trust_mode, restore_token)

        if policy.trust_mode not in WINDOWS_TRUST_MODES:
            raise WindowsComputerUseError(
                "COMPUTER_USE_UNSUPPORTED",
                f"Unsupported trust mode: {trust_mode!r}",
            )
        if policy.trust_mode == "free_run" and not policy.reuse_allowed:
            raise WindowsComputerUseError(
                "COMPUTER_USE_REARM_REQUIRED",
                "Free-run requires a stored restore token.",
            )

        if self._session is not None and self._session.session.context_id == context_id:
            self._session.session.active = True
            return self._session.session.to_payload(reused=False)

        reusable = self._store.get(context_id)
        if reusable is not None and policy.reuse_allowed and reusable.restore_token == restore_token:
            reusable = WindowsSession(
                context_id=context_id,
                session_id=reusable.session_id,
                trust_mode=policy.trust_mode,
                restore_token=reusable.restore_token,
                active=True,
                width=reusable.width,
                height=reusable.height,
            )
            self._session = _RuntimeSession(session=reusable, policy=policy)
            self._store.put(reusable)
            return reusable.to_payload(reused=True)

        width, height = self._driver.screen_size()
        session = WindowsSession(
            context_id=context_id,
            session_id=uuid.uuid4().hex,
            trust_mode=policy.trust_mode,
            restore_token=restore_token if policy.persist_metadata else "",
            active=True,
            width=width,
            height=height,
        )
        if policy.persist_metadata and not session.restore_token:
            session = WindowsSession(
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
            raise WindowsComputerUseError(
                "COMPUTER_USE_KEYS_REQUIRED",
                "key requires a non-empty keys list.",
            )
        normalized = [str(item).strip() for item in keys if str(item).strip()]
        if not normalized:
            raise WindowsComputerUseError(
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
            raise WindowsComputerUseError(
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

    def _require_session(self, params: dict[str, Any]) -> _RuntimeSession:
        context_id = normalize_context_id(params.get("context_id"))
        session = self._session
        if session is None or not session.session.active or session.session.context_id != context_id:
            raise WindowsComputerUseError(
                "COMPUTER_USE_SESSION_REQUIRED",
                "No computer-use session is active.",
            )

        requested_session_id = str(params.get("session_id", "")).strip()
        if requested_session_id and requested_session_id != session.session.session_id:
            raise WindowsComputerUseError(
                "COMPUTER_USE_SESSION_MISMATCH",
                "Requested session_id does not match the active computer-use session.",
            )
        return session

    def close(self) -> None:
        if self._session is not None and self._session.session.active:
            self.stop_session({"context_id": self._session.session.context_id})


def _build_error_response(
    request_id: str,
    error: WindowsComputerUseError,
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


def serve_stdio(runtime: WindowsComputerUseRuntime | None = None) -> int:
    runtime = runtime or WindowsComputerUseRuntime()
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
                            request = normalize_action_payload(action, request, context_id=normalize_context_id(request.get("context_id")))
                        result = runtime.dispatch(action, request)
                        response = {
                            "request_id": request_id,
                            "ok": True,
                            "result": result,
                        }
                    else:
                        raise WindowsComputerUseError(
                            "UNKNOWN_METHOD",
                            f"Unknown computer-use helper method: {action}",
                        )
            except WindowsComputerUseError as exc:
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
