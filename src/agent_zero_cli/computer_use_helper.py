from __future__ import annotations

import argparse
import base64
import io
import json
import os
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import dbus
from dbus.mainloop.glib import DBusGMainLoop
import gi
from PIL import Image

gi.require_version("Gst", "1.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gst  # noqa: E402

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
    "tab": "Tab",
    "up": "Up",
}

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

    def capture_png(self, output_path: str | None = None, *, timeout: float = 5.0) -> dict[str, Any]:
        deadline = time.monotonic() + max(timeout, 0.1)
        with self._sample_lock:
            while not self._sample_bytes and time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                self._sample_lock.wait(timeout=max(remaining, 0.05))
            if not self._sample_bytes:
                raise PortalError("COMPUTER_USE_CAPTURE_UNAVAILABLE", "No screen frame is available yet.")
            data = self._sample_bytes
            width = self._sample_width
            height = self._sample_height

        image = Image.frombytes("RGBA", (width, height), data)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        png_bytes = buffer.getvalue()
        result = {
            "width": width,
            "height": height,
            "captured_at": time.time(),
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
        handler = handlers.get(method)
        if handler is None:
            raise PortalError("UNKNOWN_METHOD", f"Unknown computer-use helper method: {method}")
        return handler(params)

    def start_session(self, params: dict[str, Any]) -> dict[str, Any]:
        trust_mode = str(params.get("trust_mode") or "persistent").strip().lower()
        context_id = str(params.get("context_id") or "default").strip() or "default"
        restore_token = str(params.get("restore_token") or "").strip()
        allow_prompt = bool(params.get("allow_prompt", trust_mode != "free_run"))
        request_timeout = float(params.get("request_timeout_seconds") or 0.0)
        free_run = trust_mode == "free_run" or not allow_prompt
        if trust_mode == "free_run" and not restore_token:
            raise PortalError(
                "COMPUTER_USE_REARM_REQUIRED",
                "Free-run requires a stored restore token.",
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
                free_run=free_run,
            )
            self._select_sources(session_handle, timeout=timeout, free_run=free_run)
            start_results = self._start_remote_desktop(
                session_handle,
                timeout=timeout,
                free_run=free_run,
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
            return {"active": False}
        payload = self._session_payload(self._session)
        payload["active"] = True
        return payload

    def capture(self, params: dict[str, Any]) -> dict[str, Any]:
        session = self._require_session(params)
        capture_path = str(params.get("capture_path") or "").strip()
        result = session.capture_stream.capture_png(capture_path or None)
        result["stream_id"] = session.stream_id
        result["session_id"] = session.session_id
        return result

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
        normalized = [self._keysym(name) for name in keys]
        for keysym in normalized:
            self._remote_desktop.NotifyKeyboardKeysym(
                dbus.ObjectPath(session.session_handle),
                _dbus_dict({}),
                dbus.Int32(keysym),
                dbus.UInt32(1),
            )
        for keysym in reversed(normalized):
            self._remote_desktop.NotifyKeyboardKeysym(
                dbus.ObjectPath(session.session_handle),
                _dbus_dict({}),
                dbus.Int32(keysym),
                dbus.UInt32(0),
            )
        return {"keys": keys, "session_id": session.session_id}

    def type_text(self, params: dict[str, Any]) -> dict[str, Any]:
        session = self._require_session(params)
        text = str(params.get("text") or "")
        submit = bool(params.get("submit"))
        if not text:
            raise PortalError("COMPUTER_USE_TEXT_REQUIRED", "type requires text")
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
        return {"text": text, "submitted": submit, "session_id": session.session_id}

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
        free_run: bool,
    ) -> None:
        options: dict[str, Any] = {
            "types": _dbus_u32(DEVICE_TYPE_KEYBOARD | DEVICE_TYPE_POINTER),
            "persist_mode": _dbus_u32(PERSIST_MODE_NONE),
        }
        if trust_mode in {"persistent", "free_run"}:
            options["persist_mode"] = _dbus_u32(PERSIST_MODE_EXPLICIT)
            if restore_token:
                options["restore_token"] = restore_token
        self._call_request(
            self._remote_desktop.SelectDevices,
            options,
            dbus.ObjectPath(session_handle),
            timeout=timeout,
            free_run=free_run,
        )

    def _select_sources(self, session_handle: str, *, timeout: float | None, free_run: bool) -> None:
        self._call_request(
            self._screencast.SelectSources,
            {
                "types": _dbus_u32(SOURCE_TYPE_MONITOR),
                "multiple": False,
                "cursor_mode": _dbus_u32(CURSOR_MODE_EMBEDDED),
            },
            dbus.ObjectPath(session_handle),
            timeout=timeout,
            free_run=free_run,
        )

    def _start_remote_desktop(
        self,
        session_handle: str,
        *,
        timeout: float | None,
        free_run: bool,
    ) -> dict[str, Any]:
        return self._call_request(
            self._remote_desktop.Start,
            {},
            dbus.ObjectPath(session_handle),
            "",
            timeout=timeout,
            free_run=free_run,
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
        free_run: bool = False,
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
            if free_run:
                raise PortalError(
                    "COMPUTER_USE_REARM_REQUIRED",
                    "Silent restore was not available. Re-arm computer use with Confirm with User.",
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
            if free_run:
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

    def _keysym(self, value: str) -> int:
        normalized = _KEY_ALIASES.get(value.strip().lower(), value)
        if len(normalized) == 1:
            keysym = int(Gdk.unicode_to_keyval(ord(normalized)))
        else:
            keysym = int(Gdk.keyval_from_name(normalized))
        if keysym <= 0:
            raise PortalError("COMPUTER_USE_BAD_KEY", f"Unsupported key: {value}")
        return keysym

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
