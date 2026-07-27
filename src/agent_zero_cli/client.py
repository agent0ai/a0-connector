"""A0Client for the current Agent Zero connector API over HTTP + `/ws`."""

from __future__ import annotations

import asyncio
import base64
import binascii
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
import hashlib
from http.cookiejar import Cookie
import json
import os
from pathlib import Path
import tempfile
import time
import uuid
from typing import Any, Callable, Iterator, Mapping
from urllib.parse import urlparse

import aiohttp
import httpx
import socketio

from agent_zero_cli import __version__
from agent_zero_cli.attachments import AttachmentRef, AttachmentUpload, remote_upload_path

_PLUGIN_API = "/api/plugins/_a0_connector/v1"
# Agent Zero's installer defaults to the first free port starting at 5080.
DEFAULT_HOST = "http://localhost:5080"
PROTOCOL_VERSION = "a0-connector.v1"
_SOCKET_IO_PATH = "/socket.io"
WS_NAMESPACE = "/ws"
WS_HANDLER = "plugins/_a0_connector/ws_connector"

_EVENT_HELLO = "connector_hello"
_EVENT_SUBSCRIBE = "connector_subscribe_context"
_EVENT_UNSUBSCRIBE = "connector_unsubscribe_context"
_EVENT_SEND_MESSAGE = "connector_send_message"
_EVENT_MESSAGE_QUEUE_ADD = "connector_message_queue_add"
_EVENT_MESSAGE_QUEUE_REMOVE = "connector_message_queue_remove"
_EVENT_MESSAGE_QUEUE_SEND = "connector_message_queue_send"
_EVENT_MESSAGE_QUEUE_UPDATED = "connector_message_queue_updated"
_EVENT_CONTEXT_SNAPSHOT = "connector_context_snapshot"
_EVENT_CONTEXT_EVENT = "connector_context_event"
_EVENT_CONTEXT_COMPLETE = "connector_context_complete"
_EVENT_SETTINGS_UPDATED = "connector_settings_updated"
_EVENT_FILE_OP = "connector_file_op"
_EVENT_FILE_OP_RESULT = "connector_file_op_result"
_EVENT_EXEC_OP = "connector_exec_op"
_EVENT_EXEC_OP_RESULT = "connector_exec_op_result"
_EVENT_COMPUTER_USE_OP = "connector_computer_use_op"
_EVENT_COMPUTER_USE_OP_RESULT = "connector_computer_use_op_result"
_EVENT_BROWSER_OP = "connector_browser_op"
_EVENT_BROWSER_OP_RESULT = "connector_browser_op_result"
_EVENT_GATEWAY_CONTROL = "connector_gateway_control"
_EVENT_GATEWAY_CONTROL_RESULT = "connector_gateway_control_result"
_EVENT_REMOTE_TREE_UPDATE = "connector_remote_tree_update"
_EVENT_ERROR = "connector_error"
_EVENT_TRANSFER_START = "connector_transfer_start"
_EVENT_TRANSFER_CHUNK = "connector_transfer_chunk"
_EVENT_TRANSFER_END = "connector_transfer_end"
_EVENT_TRANSFER_ABORT = "connector_transfer_abort"
DEFAULT_WS_MAX_PAYLOAD_BYTES = 50 * 1024 * 1024
LEGACY_WS_MAX_PAYLOAD_BYTES = 4 * 1024 * 1024
TRANSFER_PROTOCOL_VERSION = 1
_TRANSFER_CHUNK_BYTES = 64 * 1024
_TRANSFER_ENCODED_CHUNK_MAX = ((_TRANSFER_CHUNK_BYTES + 2) // 3) * 4
_TRANSFER_SPOOL_THRESHOLD_BYTES = 1024 * 1024
_TRANSFER_SINGLE_FRAME_BYTES = 1024 * 1024
_TRANSFER_IDLE_TIMEOUT_SECONDS = 30.0
_MAX_INCOMING_TRANSFERS = 4
_BULK_TIMEOUT_FLOOR_SECONDS = 30.0
_BULK_TIMEOUT_BYTES_PER_SECOND = 1024 * 1024
_FILE_CHUNK_BYTES = 1024 * 1024

_SOCKET_IO_PROBE_QUERY = {"transport": "polling", "EIO": "4"}
_BLANK_SOCKET_IO_REJECTION = "server rejected the Socket.IO connection without an error message"
_ALREADY_CONNECTED_REJECTION = "Already connected"
# Mirror the browser/manual-URL posture: accept self-signed or privately-issued
# certificates instead of blocking HTTPS connections outright.
_VERIFY_TLS_CERTIFICATES = False
_TLS_CERTIFICATE_ERROR_MARKERS = (
    "certificate verify failed",
    "sslcertverificationerror",
    "unable to get local issuer certificate",
    "self-signed certificate",
)


def _positive_int_env(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


A0_WS_MAX_PAYLOAD_BYTES = _positive_int_env(
    "A0_WS_MAX_PAYLOAD_BYTES",
    DEFAULT_WS_MAX_PAYLOAD_BYTES,
)

_TRANSFER_REQUEST_EVENTS = {
    _EVENT_FILE_OP,
    _EVENT_EXEC_OP,
    _EVENT_COMPUTER_USE_OP,
    _EVENT_BROWSER_OP,
    _EVENT_GATEWAY_CONTROL,
}


@dataclass
class _IncomingTransfer:
    transfer_id: str
    kind: str
    op_id: str
    context_id: str | None
    total_bytes: int
    sha256: str
    digest: Any
    updated_at: float
    buffer: bytearray | None = None
    temp_path: Path | None = None
    next_index: int = 0
    received_bytes: int = 0
    timeout_task: asyncio.Task[None] | None = None


@dataclass
class _OutgoingTransfer:
    transfer_id: str
    kind: str
    op_id: str
    context_id: str | None
    cancel_reason: str = ""
    abort_sent: bool = False


def _peer_ws_max_payload_bytes(value: Any) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError):
        return min(LEGACY_WS_MAX_PAYLOAD_BYTES, A0_WS_MAX_PAYLOAD_BYTES)
    if limit <= 0:
        return min(LEGACY_WS_MAX_PAYLOAD_BYTES, A0_WS_MAX_PAYLOAD_BYTES)
    return min(limit, A0_WS_MAX_PAYLOAD_BYTES)


def _transfer_protocol(value: Any) -> int:
    try:
        version = int(value)
    except (TypeError, ValueError):
        return 0
    return TRANSFER_PROTOCOL_VERSION if version == TRANSFER_PROTOCOL_VERSION else 0


def _socketio_event_size(
    event: str,
    payload: dict[str, Any],
    *,
    acknowledged: bool,
) -> int:
    encoded = socketio.packet.Packet(
        socketio.packet.EVENT,
        data=[event, payload],
        namespace=WS_NAMESPACE,
        id=(2**63 - 1) if acknowledged else None,
    ).encode()
    parts = encoded if isinstance(encoded, list) else [encoded]
    return sum(
        len(part.encode("utf-8")) if isinstance(part, str) else len(part)
        for part in parts
    ) + len(parts)


class A0ProtocolError(RuntimeError):
    """Raised when the connector returns an application-level error."""


class A0ConnectorPluginMissingError(RuntimeError):
    """HTTP 404 on the connector API — the _a0_connector plugin is not loaded on Agent Zero."""


class A0WebSocketConnectionError(RuntimeError):
    """WebSocket/Socket.IO connection failed with a user-facing message."""


def _ensure_aiohttp_ws_timeout_compat() -> None:
    """Patch older aiohttp versions so python-engineio websocket connects still work."""
    if hasattr(aiohttp, "ClientWSTimeout"):
        return

    def _client_ws_timeout_compat(*, ws_close: float | None = None, **_: Any) -> float | None:
        return ws_close

    aiohttp.ClientWSTimeout = _client_ws_timeout_compat  # type: ignore[attr-defined]


def _socketio_client_kwargs() -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "ssl_verify": _VERIFY_TLS_CERTIFICATES,
        "reconnection": False,
    }
    websocket_options: dict[str, Any] = {
        # aiohttp rejects projected frame sizes greater than or equal to this
        # parser bound. Keep the negotiated application ceiling inclusive.
        "max_msg_size": A0_WS_MAX_PAYLOAD_BYTES + 1,
    }
    if not _VERIFY_TLS_CERTIFICATES:
        # Some python-engineio/aiohttp combinations still let the WebSocket
        # upgrade fall back to aiohttp's default SSL context. Make the intent
        # explicit for ws_connect too, not only for the Engine.IO HTTP probe.
        websocket_options["ssl"] = False
    kwargs["websocket_extra_options"] = websocket_options
    return kwargs


def _bulk_transfer_timeout(total_bytes: int) -> httpx.Timeout:
    transfer_seconds = _BULK_TIMEOUT_FLOOR_SECONDS + (
        max(0, total_bytes) / _BULK_TIMEOUT_BYTES_PER_SECOND
    )
    return httpx.Timeout(
        connect=10.0,
        read=transfer_seconds,
        write=transfer_seconds,
        pool=10.0,
    )


def _attachment_size_and_sha256(upload: AttachmentUpload) -> tuple[int, str]:
    if isinstance(upload.content, bytes):
        return len(upload.content), hashlib.sha256(upload.content).hexdigest()

    path = Path(upload.content)
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_FILE_CHUNK_BYTES), b""):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


@contextmanager
def _multipart_files(
    uploads: list[AttachmentUpload],
) -> Iterator[list[tuple[str, tuple[str, Any, str]]]]:
    with ExitStack() as stack:
        files = []
        for upload in uploads:
            content = (
                upload.content
                if isinstance(upload.content, bytes)
                else stack.enter_context(Path(upload.content).open("rb"))
            )
            files.append(
                ("file", (upload.filename, content, upload.mime_type))
            )
        yield files


def _fsync_directory(path: Path) -> None:
    try:
        directory_fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


class A0Client:
    """Client for communicating with a running Agent Zero instance."""

    def __init__(self, base_url: str) -> None:
        _ensure_aiohttp_ws_timeout_compat()
        self.base_url = base_url.rstrip("/")
        self.http = httpx.AsyncClient(
            timeout=httpx.Timeout(10.0),
            verify=_VERIFY_TLS_CERTIFICATES,
        )
        self.sio = socketio.AsyncClient(**_socketio_client_kwargs())
        self.connected = False
        self._csrf_token: str | None = None
        self._events_registered = False
        self._last_connect_error: Any = None
        self._suppress_disconnect_callback = False
        self._op_result_notification_tasks: set[asyncio.Task[None]] = set()
        self.peer_ws_max_payload_bytes = _peer_ws_max_payload_bytes(None)
        self.peer_transfer_protocol = 0
        self._incoming_transfers: dict[str, _IncomingTransfer] = {}
        self._outgoing_transfers: dict[str, _OutgoingTransfer] = {}

        self.on_connect: Callable[[], None] | None = None
        self.on_disconnect: Callable[[], None] | None = None
        self.on_context_event: Callable[[dict[str, Any]], None] | None = None
        self.on_context_snapshot: Callable[[dict[str, Any]], None] | None = None
        self.on_context_complete: Callable[[dict[str, Any]], None] | None = None
        self.on_message_queue_updated: Callable[[dict[str, Any]], None] | None = None
        self.on_settings_updated: Callable[[dict[str, Any]], None] | None = None
        self.on_error: Callable[[dict[str, Any]], None] | None = None
        self.on_file_op: Callable[[dict[str, Any]], Any] | None = None
        self.on_exec_op: Callable[[dict[str, Any]], Any] | None = None
        self.on_computer_use_op: Callable[[dict[str, Any]], Any] | None = None
        self.on_computer_use_op_result_sent: Callable[[dict[str, Any], dict[str, Any]], Any] | None = None
        self.on_browser_op: Callable[[dict[str, Any]], Any] | None = None
        self.on_browser_op_result_sent: Callable[[dict[str, Any], dict[str, Any]], Any] | None = None
        self.on_gateway_control: Callable[[dict[str, Any]], Any] | None = None
        self.on_gateway_control_result_sent: Callable[[dict[str, Any], dict[str, Any]], Any] | None = None

    def _api_url(self, endpoint: str) -> str:
        return f"{self.base_url}{_PLUGIN_API}/{endpoint}"

    def _core_api_url(self, endpoint: str) -> str:
        return f"{self.base_url}/api/{endpoint.lstrip('/')}"

    def _login_url(self) -> str:
        return f"{self.base_url}/login"

    def _logout_url(self) -> str:
        return f"{self.base_url}/logout"

    def _socket_io_url(self) -> str:
        return f"{self.base_url}{_SOCKET_IO_PATH}"

    def _ws_auth(self) -> dict[str, Any]:
        return {"handlers": [WS_HANDLER]}

    def _cookie_header(self, url: str) -> str:
        request = httpx.Request("GET", url)
        self.http.cookies.set_cookie_header(request)
        return request.headers.get("Cookie", "")

    def _ws_headers(self) -> dict[str, str]:
        headers = {
            "Origin": self.base_url,
            "Referer": f"{self.base_url}/",
        }
        cookie_header = self._cookie_header(self._socket_io_url())
        if cookie_header:
            headers["Cookie"] = cookie_header
        return headers

    def _browser_headers(self) -> dict[str, str]:
        return {
            "Origin": self.base_url,
            "Referer": f"{self.base_url}/",
        }

    def _session_cookie_records(self) -> list[dict[str, Any]]:
        now = time.time()
        records: list[dict[str, Any]] = []
        for cookie in self.http.cookies.jar:
            domain = str(cookie.domain or "").strip()
            if not domain:
                continue
            if cookie.expires is not None and cookie.expires <= now:
                continue
            records.append(
                {
                    "name": cookie.name,
                    "value": cookie.value,
                    "domain": domain,
                    "path": str(cookie.path or "/") or "/",
                    "secure": bool(cookie.secure),
                    "expires": int(cookie.expires) if cookie.expires is not None else None,
                }
            )
        return records

    def _load_session_cookie_records(self, records: list[dict[str, Any]]) -> bool:
        cookies: list[Cookie] = []
        for record in records:
            if not isinstance(record, dict):
                continue
            name = str(record.get("name") or "").strip()
            domain = str(record.get("domain") or "").strip()
            if not name or not domain:
                continue

            expires_raw = record.get("expires")
            expires: int | None
            if expires_raw is None or expires_raw == "":
                expires = None
            else:
                try:
                    expires = int(expires_raw)
                except (TypeError, ValueError):
                    continue

            cookies.append(
                Cookie(
                    version=0,
                    name=name,
                    value=str(record.get("value") or ""),
                    port=None,
                    port_specified=False,
                    domain=domain,
                    domain_specified=True,
                    domain_initial_dot=domain.startswith("."),
                    path=str(record.get("path") or "/") or "/",
                    path_specified=True,
                    secure=bool(record.get("secure")),
                    expires=expires,
                    discard=expires is None,
                    comment=None,
                    comment_url=None,
                    rest={},
                    rfc2109=False,
                )
            )

        if not cookies:
            return False

        self.clear_session()
        for cookie in cookies:
            self.http.cookies.jar.set_cookie(cookie)
        return True

    def persist_session(self, host: str) -> None:
        from agent_zero_cli.config import save_persisted_session

        save_persisted_session(host, self._session_cookie_records())

    def restore_session(self, host: str) -> bool:
        from agent_zero_cli.config import load_persisted_session

        records = load_persisted_session(host)
        if not records:
            return False
        return self._load_session_cookie_records(records)

    def clear_persisted_session(self, host: str) -> None:
        from agent_zero_cli.config import delete_persisted_session

        delete_persisted_session(host)

    def _unwrap_envelope(self, payload: dict[str, Any] | None) -> dict[str, Any]:
        if not isinstance(payload, dict):
            return {}
        nested = payload.get("data")
        if isinstance(nested, dict):
            return nested
        return payload

    def _json(self, response: httpx.Response) -> dict[str, Any]:
        payload = response.json()
        return payload if isinstance(payload, dict) else {}

    def _response_message(self, response: httpx.Response) -> str:
        try:
            payload = self._json(response)
        except Exception:
            payload = {}

        for key in ("message", "error"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

        text = response.text.strip()
        if text:
            return text

        return f"HTTP {response.status_code}"

    def _is_login_redirect(self, response: httpx.Response) -> bool:
        if response.status_code not in {301, 302, 303, 307, 308}:
            return False

        location = response.headers.get("location", "").strip()
        if not location:
            return False

        path = urlparse(location).path or location
        return path == "/login" or path.endswith("/login")

    def _raise_for_results(self, response: dict[str, Any] | None, event: str) -> dict[str, Any]:
        if not isinstance(response, dict):
            raise A0ProtocolError(f"{event} returned an invalid response")

        results = response.get("results")
        if not isinstance(results, list):
            return {}

        for item in results:
            if not isinstance(item, dict):
                continue
            if item.get("ok") is True:
                data = item.get("data")
                return data if isinstance(data, dict) else {}
            error = item.get("error")
            if isinstance(error, dict):
                code = error.get("code", "ERROR")
                message = error.get("error") or error.get("message") or "Unknown error"
                raise A0ProtocolError(f"{code}: {message}")

        return {}

    def _format_connect_error(
        self,
        exc: BaseException | None = None,
        payload: Any = None,
    ) -> str:
        payload = self._unwrap_envelope(payload) if isinstance(payload, dict) else payload

        if isinstance(payload, dict):
            code = payload.get("code")
            message = payload.get("error") or payload.get("message") or payload.get("reason")
            details = payload.get("details")

            parts: list[str] = []
            if code:
                parts.append(str(code))
            if message:
                parts.append(str(message))

            formatted = ": ".join(parts) if parts else ""
            if details:
                suffix = details if isinstance(details, str) else repr(details)
                formatted = f"{formatted} ({suffix})" if formatted else str(suffix)
            if formatted:
                return formatted

        if isinstance(payload, str) and payload.strip():
            return payload.strip()

        if exc is not None:
            message = str(exc).strip()
            if message:
                return message

        return _BLANK_SOCKET_IO_REJECTION

    def _is_already_connected_error(
        self,
        exc: BaseException | None = None,
        payload: Any = None,
    ) -> bool:
        reason = self._format_connect_error(exc, payload)
        return reason.strip().lower() == _ALREADY_CONNECTED_REJECTION.lower()

    async def _probe_socketio_transport(self) -> None:
        probe_url = self._socket_io_url()

        try:
            response = await self.http.get(
                probe_url,
                params=_SOCKET_IO_PROBE_QUERY,
                headers=self._ws_headers(),
            )
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            raise A0WebSocketConnectionError(
                "Socket.IO transport probe failed: could not reach "
                f"{probe_url}?transport=polling&EIO=4. Ensure Agent Zero is running and any "
                "reverse proxy forwards /socket.io unchanged (not just /api/plugins/)."
            ) from exc
        except httpx.HTTPError as exc:
            raise A0WebSocketConnectionError(
                "Socket.IO transport probe failed before the websocket handshake. Ensure any "
                "reverse proxy forwards /socket.io unchanged (not just /api/plugins/)."
            ) from exc

        if response.status_code != 200:
            raise A0WebSocketConnectionError(
                "Socket.IO transport probe failed: "
                f"GET {probe_url}?transport=polling&EIO=4 returned HTTP {response.status_code}. "
                "Ensure Agent Zero is running and any reverse proxy forwards /socket.io unchanged "
                "(not just /api/plugins/)."
            )

        if not response.text.lstrip().startswith("0{"):
            raise A0WebSocketConnectionError(
                "Socket.IO transport probe reached /socket.io, but the response was not a valid "
                "Engine.IO handshake. Ensure any reverse proxy forwards /socket.io unchanged "
                "without rewriting or caching it."
            )

    def _format_namespace_rejection_error(self, exc: BaseException | None = None) -> str:
        reason = self._format_connect_error(exc, self._last_connect_error)
        if reason.strip().lower() == _ALREADY_CONNECTED_REJECTION.lower():
            return (
                "Socket.IO could not start a clean connector session because the previous "
                "transport still appeared connected. Retry will reset the transport before "
                "opening a fresh /ws session."
            )

        reason_lower = reason.lower()
        if any(marker in reason_lower for marker in _TLS_CERTIFICATE_ERROR_MARKERS):
            return (
                f"Socket.IO transport probe succeeded, but the {WS_NAMESPACE} namespace "
                f"connection failed TLS certificate verification: {reason}. Update the CLI so "
                "its Socket.IO transport uses the connector TLS settings, or fix the server "
                "certificate chain if strict verification is enabled."
            )

        guidance = (
            "This usually means an Origin/Referer or proxy host mismatch. Check that "
            "AGENT_ZERO_HOST exactly matches the Agent Zero URL (for example localhost vs "
            "127.0.0.1) and that any reverse proxy forwards Host, X-Forwarded-Host, and "
            "X-Forwarded-Proto correctly."
        )

        if reason == _BLANK_SOCKET_IO_REJECTION:
            return (
                f"Socket.IO transport probe succeeded, but the {WS_NAMESPACE} namespace "
                f"connection was rejected. {guidance}"
            )

        return (
            f"Socket.IO transport probe succeeded, but the {WS_NAMESPACE} namespace connection "
            f"was rejected: {reason}. {guidance}"
        )

    async def _post(
        self,
        endpoint: str,
        payload: dict[str, Any] | None = None,
    ) -> httpx.Response:
        return await self.http.post(
            self._api_url(endpoint),
            json=payload or {},
        )

    async def _call(self, event: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        data = payload or {}
        self._raise_if_payload_too_large(event, data, acknowledged=True)
        response = await self.sio.call(
            event,
            data,
            namespace=WS_NAMESPACE,
        )
        return self._raise_for_results(response, event)

    def _raise_if_payload_too_large(
        self,
        event: str,
        payload: dict[str, Any],
        *,
        acknowledged: bool,
    ) -> None:
        actual_bytes = _socketio_event_size(
            event,
            payload,
            acknowledged=acknowledged,
        )
        if actual_bytes <= self.peer_ws_max_payload_bytes:
            return
        raise A0ProtocolError(
            f"PAYLOAD_TOO_LARGE: {event} serializes to {actual_bytes} bytes; "
            f"the peer limit is {self.peer_ws_max_payload_bytes} bytes. "
            "Use the HTTP bulk-transfer path."
        )

    def _payload_too_large_result(
        self,
        event: str,
        result: dict[str, Any],
        actual_bytes: int,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": False,
            "code": "PAYLOAD_TOO_LARGE",
            "error": (
                f"PAYLOAD_TOO_LARGE: {event} serializes to {actual_bytes} bytes; "
                f"the peer limit is {self.peer_ws_max_payload_bytes} bytes. "
                "Use the HTTP bulk-transfer path."
            ),
            "details": {
                "type": "payload_too_large",
                "event": event,
                "actual_bytes": actual_bytes,
                "limit_bytes": self.peer_ws_max_payload_bytes,
                "alternative": "http_bulk_transfer",
            },
        }
        for key in ("op_id", "request_id"):
            value = result.get(key)
            if value:
                payload[key] = value
        return payload

    async def _emit_op_result(
        self,
        event: str,
        result: dict[str, Any],
        *,
        context_id: str | None = None,
    ) -> dict[str, Any]:
        actual_bytes = _socketio_event_size(event, result, acknowledged=False)
        raw = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if (
            len(raw) <= _TRANSFER_SINGLE_FRAME_BYTES
            and actual_bytes <= self.peer_ws_max_payload_bytes
        ):
            await self.sio.emit(event, result, namespace=WS_NAMESPACE)
            return result

        if (
            self.peer_transfer_protocol == TRANSFER_PROTOCOL_VERSION
            and len(raw) <= self.peer_ws_max_payload_bytes
        ):
            cancel_reason = await self._send_transfer(
                event,
                result,
                raw,
                context_id=context_id,
            )
            if cancel_reason:
                return {
                    "op_id": result.get("op_id"),
                    "request_id": result.get("request_id"),
                    "ok": False,
                    "code": "TRANSFER_ABORTED",
                    "error": cancel_reason,
                }
            return result

        payload = self._payload_too_large_result(event, result, actual_bytes)
        await self.sio.emit(event, payload, namespace=WS_NAMESPACE)
        return payload

    async def _send_transfer(
        self,
        kind: str,
        payload: dict[str, Any],
        raw: bytes,
        *,
        context_id: str | None = None,
    ) -> str:
        transfer_id = uuid.uuid4().hex
        op_id = str(payload.get("op_id") or payload.get("request_id") or "")
        state = _OutgoingTransfer(
            transfer_id=transfer_id,
            kind=kind,
            op_id=op_id,
            context_id=context_id,
        )
        self._outgoing_transfers[transfer_id] = state
        start = {
            "transfer_id": transfer_id,
            "op_id": op_id,
            "kind": kind,
            "total_bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
        if context_id:
            start["context_id"] = context_id
        try:
            await self.sio.emit(_EVENT_TRANSFER_START, start, namespace=WS_NAMESPACE)
            if state.cancel_reason:
                return state.cancel_reason
            for index, offset in enumerate(range(0, len(raw), _TRANSFER_CHUNK_BYTES)):
                if state.cancel_reason:
                    return state.cancel_reason
                await self.sio.emit(
                    _EVENT_TRANSFER_CHUNK,
                    {
                        "transfer_id": transfer_id,
                        "index": index,
                        "data": base64.b64encode(
                            raw[offset : offset + _TRANSFER_CHUNK_BYTES]
                        ).decode("ascii"),
                    },
                    namespace=WS_NAMESPACE,
                )
                if state.cancel_reason:
                    return state.cancel_reason
            await self.sio.emit(
                _EVENT_TRANSFER_END,
                {"transfer_id": transfer_id},
                namespace=WS_NAMESPACE,
            )
            return ""
        except Exception as exc:
            if not state.abort_sent:
                state.abort_sent = True
                try:
                    await self.sio.emit(
                        _EVENT_TRANSFER_ABORT,
                        {
                            "transfer_id": transfer_id,
                            "op_id": op_id,
                            "kind": kind,
                            "reason": str(exc)[:512],
                        },
                        namespace=WS_NAMESPACE,
                    )
                except Exception:
                    pass
            raise
        finally:
            self._outgoing_transfers.pop(transfer_id, None)

    def _register_event_handlers(self) -> None:
        if self._events_registered:
            return

        @self.sio.on("connect", namespace=WS_NAMESPACE)
        async def _on_connect() -> None:
            self.connected = True
            callback = self.on_connect
            if callback is not None:
                callback()

        @self.sio.on("disconnect", namespace=WS_NAMESPACE)
        async def _on_disconnect() -> None:
            self.connected = False
            self._clear_transfers("connector disconnected during transfer")
            if self._suppress_disconnect_callback:
                return
            callback = self.on_disconnect
            if callback is not None:
                callback()

        @self.sio.on("connect_error")
        async def _on_connect_error_root(payload: Any) -> None:
            self._last_connect_error = payload

        @self.sio.on("connect_error", namespace=WS_NAMESPACE)
        async def _on_connect_error(payload: Any) -> None:
            self._last_connect_error = payload

        @self.sio.on(_EVENT_CONTEXT_SNAPSHOT, namespace=WS_NAMESPACE)
        async def _on_context_snapshot(payload: dict[str, Any]) -> None:
            callback = self.on_context_snapshot
            if callback is not None:
                callback(self._unwrap_envelope(payload))

        @self.sio.on(_EVENT_CONTEXT_EVENT, namespace=WS_NAMESPACE)
        async def _on_context_event(payload: dict[str, Any]) -> None:
            callback = self.on_context_event
            if callback is not None:
                callback(self._unwrap_envelope(payload))

        @self.sio.on(_EVENT_CONTEXT_COMPLETE, namespace=WS_NAMESPACE)
        async def _on_context_complete(payload: dict[str, Any]) -> None:
            callback = self.on_context_complete
            if callback is not None:
                callback(self._unwrap_envelope(payload))

        @self.sio.on(_EVENT_MESSAGE_QUEUE_UPDATED, namespace=WS_NAMESPACE)
        async def _on_message_queue_updated(payload: dict[str, Any]) -> None:
            callback = self.on_message_queue_updated
            if callback is not None:
                callback(self._unwrap_envelope(payload))

        @self.sio.on(_EVENT_SETTINGS_UPDATED, namespace=WS_NAMESPACE)
        async def _on_settings_updated(payload: dict[str, Any]) -> None:
            callback = self.on_settings_updated
            if callback is not None:
                callback(self._unwrap_envelope(payload))

        @self.sio.on(_EVENT_ERROR, namespace=WS_NAMESPACE)
        async def _on_error(payload: dict[str, Any]) -> None:
            callback = self.on_error
            if callback is not None:
                callback(self._unwrap_envelope(payload))

        @self.sio.on(_EVENT_FILE_OP, namespace=WS_NAMESPACE)
        async def _on_file_op(payload: dict[str, Any]) -> None:
            await self._dispatch_incoming_operation(
                _EVENT_FILE_OP,
                self._unwrap_envelope(payload),
            )

        @self.sio.on(_EVENT_EXEC_OP, namespace=WS_NAMESPACE)
        async def _on_exec_op(payload: dict[str, Any]) -> None:
            await self._dispatch_incoming_operation(
                _EVENT_EXEC_OP,
                self._unwrap_envelope(payload),
            )

        @self.sio.on(_EVENT_COMPUTER_USE_OP, namespace=WS_NAMESPACE)
        async def _on_computer_use_op(payload: dict[str, Any]) -> None:
            await self._dispatch_incoming_operation(
                _EVENT_COMPUTER_USE_OP,
                self._unwrap_envelope(payload),
            )

        @self.sio.on(_EVENT_BROWSER_OP, namespace=WS_NAMESPACE)
        async def _on_browser_op(payload: dict[str, Any]) -> None:
            await self._dispatch_incoming_operation(
                _EVENT_BROWSER_OP,
                self._unwrap_envelope(payload),
            )

        @self.sio.on(_EVENT_GATEWAY_CONTROL, namespace=WS_NAMESPACE)
        async def _on_gateway_control(payload: dict[str, Any]) -> None:
            await self._dispatch_incoming_operation(
                _EVENT_GATEWAY_CONTROL,
                self._unwrap_envelope(payload),
            )

        @self.sio.on(_EVENT_TRANSFER_START, namespace=WS_NAMESPACE)
        async def _on_transfer_start(payload: dict[str, Any]) -> None:
            await self._receive_transfer_start(self._unwrap_envelope(payload))

        @self.sio.on(_EVENT_TRANSFER_CHUNK, namespace=WS_NAMESPACE)
        async def _on_transfer_chunk(payload: dict[str, Any]) -> None:
            await self._receive_transfer_chunk(self._unwrap_envelope(payload))

        @self.sio.on(_EVENT_TRANSFER_END, namespace=WS_NAMESPACE)
        async def _on_transfer_end(payload: dict[str, Any]) -> None:
            completed = await self._receive_transfer_end(self._unwrap_envelope(payload))
            if completed is not None:
                kind, request = completed
                await self._dispatch_incoming_operation(kind, request)

        @self.sio.on(_EVENT_TRANSFER_ABORT, namespace=WS_NAMESPACE)
        async def _on_transfer_abort(payload: dict[str, Any]) -> None:
            data = self._unwrap_envelope(payload)
            transfer_id = str(data.get("transfer_id") or "")
            outgoing = self._outgoing_transfers.pop(transfer_id, None)
            if outgoing is not None:
                outgoing.cancel_reason = str(data.get("reason") or "peer aborted transfer")[:512]
                outgoing.abort_sent = True
            self._drop_incoming_transfer(transfer_id)

        self._events_registered = True

    async def _dispatch_incoming_operation(
        self,
        event: str,
        request: dict[str, Any],
    ) -> None:
        if event == _EVENT_FILE_OP:
            await self._emit_op_result(
                _EVENT_FILE_OP_RESULT,
                await self._handle_file_op(request),
                context_id=self._transfer_context_id(request.get("context_id")),
            )
            return
        if event == _EVENT_EXEC_OP:
            await self._emit_op_result(
                _EVENT_EXEC_OP_RESULT,
                await self._handle_exec_op(request),
                context_id=self._transfer_context_id(request.get("context_id")),
            )
            return
        if event == _EVENT_COMPUTER_USE_OP:
            result = await self._emit_op_result(
                _EVENT_COMPUTER_USE_OP_RESULT,
                await self._handle_computer_use_op(request),
                context_id=self._transfer_context_id(request.get("context_id")),
            )
            self._notify_op_result_sent(
                self.on_computer_use_op_result_sent,
                request,
                result,
            )
            return
        if event == _EVENT_BROWSER_OP:
            result = await self._emit_op_result(
                _EVENT_BROWSER_OP_RESULT,
                await self._handle_browser_op(request),
                context_id=self._transfer_context_id(request.get("context_id")),
            )
            self._notify_op_result_sent(
                self.on_browser_op_result_sent,
                request,
                result,
            )
            return
        if event == _EVENT_GATEWAY_CONTROL:
            result = await self._emit_op_result(
                _EVENT_GATEWAY_CONTROL_RESULT,
                await self._handle_gateway_control(request),
                context_id=self._transfer_context_id(request.get("context_id")),
            )
            self._notify_op_result_sent(
                self.on_gateway_control_result_sent,
                request,
                result,
            )

    async def _receive_transfer_start(self, data: dict[str, Any]) -> None:
        transfer_id = str(data.get("transfer_id") or "")
        kind = str(data.get("kind") or "")
        op_id = str(data.get("op_id") or "")
        raw_context_id = data.get("context_id")
        context_id = self._transfer_context_id(raw_context_id)
        try:
            total_bytes = int(data.get("total_bytes"))
        except (TypeError, ValueError):
            total_bytes = -1
        sha256 = str(data.get("sha256") or "").lower()

        error = ""
        if not transfer_id or len(transfer_id) > 128:
            error = "transfer_id must be a non-empty string up to 128 characters"
        elif transfer_id in self._incoming_transfers:
            error = "transfer_id is already active"
        elif len(self._incoming_transfers) >= _MAX_INCOMING_TRANSFERS:
            error = "too many concurrent transfers"
        elif kind not in _TRANSFER_REQUEST_EVENTS:
            error = f"unsupported transfer kind: {kind or '<missing>'}"
        elif not op_id or len(op_id) > 256:
            error = "op_id must be a non-empty string up to 256 characters"
        elif raw_context_id is not None and (
            not isinstance(raw_context_id, str)
            or (str(raw_context_id).strip() and context_id is None)
        ):
            error = "context_id must be a string up to 256 characters"
        elif total_bytes < 0:
            error = "total_bytes must be a non-negative integer"
        elif total_bytes > A0_WS_MAX_PAYLOAD_BYTES:
            error = (
                f"declared transfer size {total_bytes} exceeds the receiver limit "
                f"{A0_WS_MAX_PAYLOAD_BYTES}"
            )
        elif len(sha256) != 64 or any(char not in "0123456789abcdef" for char in sha256):
            error = "sha256 must be a 64-character hexadecimal digest"
        if error:
            await self._reject_transfer(data, error)
            return

        temp_path: Path | None = None
        buffer: bytearray | None = bytearray()
        if total_bytes > _TRANSFER_SPOOL_THRESHOLD_BYTES:
            fd, temp_name = tempfile.mkstemp(prefix="a0-transfer-")
            os.close(fd)
            temp_path = Path(temp_name)
            buffer = None
        state = _IncomingTransfer(
            transfer_id=transfer_id,
            kind=kind,
            op_id=op_id,
            context_id=context_id,
            total_bytes=total_bytes,
            sha256=sha256,
            digest=hashlib.sha256(),
            updated_at=time.monotonic(),
            buffer=buffer,
            temp_path=temp_path,
        )
        self._incoming_transfers[transfer_id] = state
        state.timeout_task = asyncio.create_task(
            self._expire_incoming_transfer(transfer_id)
        )

    async def _receive_transfer_chunk(self, data: dict[str, Any]) -> None:
        transfer_id = str(data.get("transfer_id") or "")
        state = self._incoming_transfers.get(transfer_id)
        if state is None:
            await self._reject_transfer(data, "transfer is not active")
            return
        try:
            index = int(data.get("index"))
        except (TypeError, ValueError):
            await self._reject_transfer(data, "chunk index must be an integer")
            return
        encoded = data.get("data")
        if not isinstance(encoded, str) or len(encoded) > _TRANSFER_ENCODED_CHUNK_MAX:
            await self._reject_transfer(data, "chunk data exceeds the 64 KiB limit")
            return
        try:
            chunk = base64.b64decode(encoded.encode("ascii"), validate=True)
        except (UnicodeEncodeError, binascii.Error):
            await self._reject_transfer(data, "chunk data is not valid base64")
            return

        expected_bytes = min(
            _TRANSFER_CHUNK_BYTES,
            state.total_bytes - state.received_bytes,
        )
        if index != state.next_index:
            await self._reject_transfer(
                data,
                f"chunk index {index} arrived; expected {state.next_index}",
            )
            return
        if len(chunk) != expected_bytes:
            await self._reject_transfer(
                data,
                f"chunk {index} has {len(chunk)} bytes; expected {expected_bytes}",
            )
            return

        try:
            if state.buffer is not None:
                state.buffer.extend(chunk)
            elif state.temp_path is not None:
                with state.temp_path.open("ab") as handle:
                    handle.write(chunk)
            state.digest.update(chunk)
            state.received_bytes += len(chunk)
            state.next_index += 1
            state.updated_at = time.monotonic()
        except OSError as exc:
            await self._reject_transfer(data, f"could not spool transfer: {exc}")

    async def _receive_transfer_end(
        self,
        data: dict[str, Any],
    ) -> tuple[str, dict[str, Any]] | None:
        transfer_id = str(data.get("transfer_id") or "")
        state = self._incoming_transfers.get(transfer_id)
        if state is None:
            await self._reject_transfer(data, "transfer is not active")
            return None
        if state.received_bytes != state.total_bytes:
            await self._reject_transfer(
                data,
                f"transfer ended at {state.received_bytes} of {state.total_bytes} bytes",
            )
            return None
        if state.digest.hexdigest() != state.sha256:
            await self._reject_transfer(data, "transfer SHA-256 mismatch")
            return None

        try:
            raw = (
                bytes(state.buffer)
                if state.buffer is not None
                else state.temp_path.read_bytes() if state.temp_path is not None else b""
            )
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("decoded transfer is not a JSON object")
            payload_op_id = str(payload.get("op_id") or payload.get("request_id") or "")
            if payload_op_id != state.op_id:
                raise ValueError("decoded operation id does not match transfer_start")
        except Exception as exc:
            await self._reject_transfer(data, f"invalid transferred payload: {exc}")
            return None

        kind = state.kind
        self._drop_incoming_transfer(transfer_id)
        return kind, payload

    async def _expire_incoming_transfer(self, transfer_id: str) -> None:
        while True:
            state = self._incoming_transfers.get(transfer_id)
            if state is None:
                return
            remaining = _TRANSFER_IDLE_TIMEOUT_SECONDS - (
                time.monotonic() - state.updated_at
            )
            if remaining > 0:
                await asyncio.sleep(remaining)
                continue
            await self._reject_transfer(
                {
                    "transfer_id": transfer_id,
                    "op_id": state.op_id,
                    "kind": state.kind,
                },
                f"transfer idle timeout after {_TRANSFER_IDLE_TIMEOUT_SECONDS:g} seconds",
            )
            return

    async def _reject_transfer(self, data: dict[str, Any], reason: str) -> None:
        transfer_id = str(data.get("transfer_id") or "")
        state = self._incoming_transfers.get(transfer_id)
        op_id = state.op_id if state is not None else str(data.get("op_id") or "")
        kind = state.kind if state is not None else str(data.get("kind") or "")
        self._drop_incoming_transfer(transfer_id)
        try:
            await self.sio.emit(
                _EVENT_TRANSFER_ABORT,
                {
                    "transfer_id": transfer_id,
                    "op_id": op_id,
                    "kind": kind,
                    "reason": reason[:512],
                },
                namespace=WS_NAMESPACE,
            )
        except Exception:
            pass

    def _drop_incoming_transfer(self, transfer_id: str) -> None:
        state = self._incoming_transfers.pop(transfer_id, None)
        if state is None:
            return
        task = state.timeout_task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
        if state.temp_path is not None:
            try:
                state.temp_path.unlink()
            except FileNotFoundError:
                pass

    def _clear_incoming_transfers(self) -> None:
        for transfer_id in list(self._incoming_transfers):
            self._drop_incoming_transfer(transfer_id)

    @staticmethod
    def _transfer_context_id(value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        context_id = value.strip()
        return context_id if context_id and len(context_id) <= 256 else None

    async def _abort_active_transfers(
        self,
        *,
        reason: str,
        context_id: str | None = None,
    ) -> None:
        outgoing = [
            state
            for state in self._outgoing_transfers.values()
            if context_id is None or state.context_id == context_id
        ]
        incoming = [
            state
            for state in self._incoming_transfers.values()
            if context_id is None or state.context_id == context_id
        ]
        for state in outgoing:
            state.cancel_reason = reason[:512]
            state.abort_sent = True
            self._outgoing_transfers.pop(state.transfer_id, None)
        for state in incoming:
            self._drop_incoming_transfer(state.transfer_id)

        if not self.sio.connected:
            return
        for state in [*outgoing, *incoming]:
            try:
                await self.sio.emit(
                    _EVENT_TRANSFER_ABORT,
                    {
                        "transfer_id": state.transfer_id,
                        "op_id": state.op_id,
                        "kind": state.kind,
                        "reason": reason[:512],
                    },
                    namespace=WS_NAMESPACE,
                )
            except Exception:
                pass

    def _clear_transfers(self, reason: str) -> None:
        for state in self._outgoing_transfers.values():
            state.cancel_reason = reason[:512]
            state.abort_sent = True
        self._outgoing_transfers.clear()
        self._clear_incoming_transfers()

    def _notify_op_result_sent(
        self,
        callback: Callable[[dict[str, Any], dict[str, Any]], Any] | None,
        request: dict[str, Any],
        result: dict[str, Any],
    ) -> None:
        if callback is None:
            return
        try:
            notification = callback(request, result)
        except Exception:
            return
        if asyncio.iscoroutine(notification):
            task = asyncio.create_task(self._await_op_result_notification(notification))
            self._op_result_notification_tasks.add(task)
            task.add_done_callback(self._op_result_notification_tasks.discard)

    @staticmethod
    async def _await_op_result_notification(notification: Any) -> None:
        try:
            await notification
        except Exception:
            return

    async def _handle_file_op(self, data: dict[str, Any]) -> dict[str, Any]:
        callback = self.on_file_op
        op_id = data.get("op_id")
        if callback is None:
            return {
                "op_id": op_id,
                "ok": False,
                "error": "No file_op handler configured",
            }

        try:
            result = callback(data)
            if asyncio.iscoroutine(result):
                result = await result
        except Exception as exc:
            return {
                "op_id": op_id,
                "ok": False,
                "error": str(exc),
            }

        if isinstance(result, dict):
            return result

        return {
            "op_id": op_id,
            "ok": False,
            "error": "Invalid file_op handler result",
        }

    async def _handle_exec_op(self, data: dict[str, Any]) -> dict[str, Any]:
        callback = self.on_exec_op
        op_id = data.get("op_id")
        if callback is None:
            return {
                "op_id": op_id,
                "ok": False,
                "error": "No exec_op handler configured",
            }

        try:
            result = callback(data)
            if asyncio.iscoroutine(result):
                result = await result
        except Exception as exc:
            return {
                "op_id": op_id,
                "ok": False,
                "error": str(exc),
            }

        if isinstance(result, dict):
            return result

        return {
            "op_id": op_id,
            "ok": False,
            "error": "Invalid exec_op handler result",
        }

    async def _handle_computer_use_op(self, data: dict[str, Any]) -> dict[str, Any]:
        callback = self.on_computer_use_op
        op_id = data.get("op_id")
        if callback is None:
            return {
                "op_id": op_id,
                "ok": False,
                "error": "No computer_use_op handler configured",
                "code": "COMPUTER_USE_ERROR",
            }

        try:
            result = callback(data)
            if asyncio.iscoroutine(result):
                result = await result
        except Exception as exc:
            return {
                "op_id": op_id,
                "ok": False,
                "error": str(exc),
                "code": "COMPUTER_USE_ERROR",
            }

        if isinstance(result, dict):
            return result

        return {
            "op_id": op_id,
            "ok": False,
            "error": "Invalid computer_use_op handler result",
            "code": "COMPUTER_USE_ERROR",
        }

    async def _handle_browser_op(self, data: dict[str, Any]) -> dict[str, Any]:
        callback = self.on_browser_op
        op_id = data.get("op_id")
        if callback is None:
            return {
                "op_id": op_id,
                "ok": False,
                "error": "No browser_op handler configured",
                "code": "HOST_BROWSER_ERROR",
            }

        try:
            result = callback(data)
            if asyncio.iscoroutine(result):
                result = await result
        except Exception as exc:
            return {
                "op_id": op_id,
                "ok": False,
                "error": str(exc),
                "code": "HOST_BROWSER_ERROR",
            }

        if isinstance(result, dict):
            return result

        return {
            "op_id": op_id,
            "ok": False,
            "error": "Invalid browser_op handler result",
            "code": "HOST_BROWSER_ERROR",
        }

    async def _handle_gateway_control(self, data: dict[str, Any]) -> dict[str, Any]:
        callback = self.on_gateway_control
        request_id = data.get("request_id")
        if callback is None:
            return {
                "request_id": request_id,
                "ok": False,
                "error": "No gateway control handler configured",
            }
        try:
            result = callback(data)
            if asyncio.iscoroutine(result):
                result = await result
        except Exception as exc:
            return {"request_id": request_id, "ok": False, "error": str(exc)}
        if isinstance(result, dict):
            return result
        return {
            "request_id": request_id,
            "ok": False,
            "error": "Invalid gateway control handler result",
        }

    async def fetch_capabilities(self) -> dict[str, Any]:
        response = await self._post("capabilities")
        if response.status_code == 404:
            raise A0ConnectorPluginMissingError(
                "HTTP 404 — the builtin _a0_connector plugin is not available on this Agent Zero server.\n"
                "\n"
                "The web UI can work while this endpoint is missing: the CLI needs the plugin.\n"
                "On a remote host, update Agent Zero before retrying."
            )
        response.raise_for_status()
        data = self._json(response)
        capabilities = data.get("capabilities")
        self.peer_ws_max_payload_bytes = _peer_ws_max_payload_bytes(
            capabilities.get("ws_max_payload_bytes")
            if isinstance(capabilities, dict)
            else None
        )
        self.peer_transfer_protocol = _transfer_protocol(
            capabilities.get("transfer_protocol")
            if isinstance(capabilities, dict)
            else None
        )
        return data

    async def login(self, username: str, password: str) -> bool:
        """Create a browser-style authenticated session via the core /login form."""
        response = await self.http.post(
            self._login_url(),
            data={"username": username, "password": password},
            follow_redirects=False,
        )
        if response.status_code >= 500:
            response.raise_for_status()
        return await self.verify_session()

    async def verify_session(self) -> bool:
        response = await self._post("chats_list")
        if response.status_code == 200:
            return True
        if response.status_code in {401, 403} or self._is_login_redirect(response):
            return False
        response.raise_for_status()
        return False

    async def _open_websocket(self) -> None:
        self._last_connect_error = None
        await self.sio.connect(
            self.base_url,
            namespaces=[WS_NAMESPACE],
            headers=self._ws_headers(),
            auth=self._ws_auth(),
        )

    async def connect_websocket(self) -> None:
        self._register_event_handlers()
        await self.disconnect(close_http=False, notify=False)
        await self._probe_socketio_transport()
        try:
            await self._open_websocket()
        except Exception as exc:
            if self._is_already_connected_error(exc, self._last_connect_error):
                await self.disconnect(close_http=False, notify=False)
                await self._probe_socketio_transport()
                try:
                    await self._open_websocket()
                    return
                except Exception as retry_exc:
                    await self.disconnect(close_http=False, notify=False)
                    raise A0WebSocketConnectionError(
                        self._format_namespace_rejection_error(retry_exc)
                    ) from retry_exc

            await self.disconnect(close_http=False, notify=False)
            raise A0WebSocketConnectionError(self._format_namespace_rejection_error(exc)) from exc

    async def send_hello(
        self,
        *,
        context_id: str | None = None,
        computer_use: dict[str, Any] | None = None,
        host_browser: dict[str, Any] | None = None,
        remote_files: dict[str, Any] | None = None,
        remote_exec: dict[str, Any] | None = None,
        gateway: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "protocol": PROTOCOL_VERSION,
            "client": "a0",
            "client_version": __version__,
            "capabilities": {
                "ws_max_payload_bytes": A0_WS_MAX_PAYLOAD_BYTES,
                "transfer_protocol": TRANSFER_PROTOCOL_VERSION,
            },
        }
        if isinstance(context_id, str) and context_id.strip():
            payload["context_id"] = context_id.strip()
        if isinstance(computer_use, dict):
            payload["computer_use"] = dict(computer_use)
        if isinstance(host_browser, dict):
            payload["host_browser"] = dict(host_browser)
        if isinstance(remote_files, dict):
            payload["remote_files"] = dict(remote_files)
        if isinstance(remote_exec, dict):
            payload["remote_exec"] = dict(remote_exec)
        if isinstance(gateway, dict):
            payload["gateway"] = dict(gateway)
        result = await self._call(_EVENT_HELLO, payload)
        capabilities = result.get("capabilities")
        if isinstance(capabilities, dict):
            self.peer_ws_max_payload_bytes = _peer_ws_max_payload_bytes(
                capabilities.get("ws_max_payload_bytes")
            )
            self.peer_transfer_protocol = _transfer_protocol(
                capabilities.get("transfer_protocol")
            )
        else:
            self.peer_transfer_protocol = 0
        return result

    async def subscribe_context(
        self,
        context_id: str,
        from_seq: int = 0,
        *,
        history: str | None = None,
        history_before: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"context_id": context_id, "from": from_seq}
        if history:
            payload["history"] = history
        if history_before is not None:
            payload["history_before"] = history_before
        return await self._call(
            _EVENT_SUBSCRIBE,
            payload,
        )

    async def send_remote_tree_update(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._call(_EVENT_REMOTE_TREE_UPDATE, payload)

    async def unsubscribe_context(self, context_id: str) -> dict[str, Any]:
        return await self._call(
            _EVENT_UNSUBSCRIBE,
            {"context_id": context_id},
        )

    async def send_message(
        self,
        text: str,
        context_id: str,
        attachments: list[str] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "context_id": context_id,
            "message": text,
            "client_message_id": str(uuid.uuid4()),
        }
        if attachments:
            payload["attachments"] = list(attachments)
        return await self._call(_EVENT_SEND_MESSAGE, payload)

    async def add_message_to_queue(
        self,
        text: str,
        context_id: str,
        attachments: list[str] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "context_id": context_id,
            "message": text,
            "client_message_id": str(uuid.uuid4()),
        }
        if attachments:
            payload["attachments"] = list(attachments)
        return await self._call(_EVENT_MESSAGE_QUEUE_ADD, payload)

    async def remove_message_from_queue(
        self,
        context_id: str,
        *,
        item_id: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"context_id": context_id}
        if item_id:
            payload["item_id"] = item_id
        return await self._call(_EVENT_MESSAGE_QUEUE_REMOVE, payload)

    async def send_message_queue(
        self,
        context_id: str,
        *,
        item_id: str | None = None,
        send_all: bool = True,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "context_id": context_id,
            "send_all": send_all,
        }
        if item_id:
            payload["item_id"] = item_id
        return await self._call(_EVENT_MESSAGE_QUEUE_SEND, payload)

    async def fetch_csrf_token(self) -> str:
        if self._csrf_token:
            return self._csrf_token

        response = await self.http.get(
            self._core_api_url("csrf_token"),
            headers=self._browser_headers(),
        )
        if self._is_login_redirect(response):
            raise A0ProtocolError("CSRF token request requires an authenticated Agent Zero session.")
        if response.status_code >= 400:
            raise A0ProtocolError(f"CSRF token request failed: {self._response_message(response)}")

        data = self._json(response)
        if not data.get("ok"):
            message = data.get("error") or data.get("message") or "CSRF token request failed."
            raise A0ProtocolError(str(message))

        token = data.get("token")
        if not isinstance(token, str) or not token:
            raise A0ProtocolError("CSRF token response did not include a token.")

        self._csrf_token = token
        return token

    async def _csrf_headers(self) -> dict[str, str]:
        headers = self._browser_headers()
        headers["X-CSRF-Token"] = await self.fetch_csrf_token()
        return headers

    async def upload_attachments(self, uploads: list[AttachmentUpload]) -> list[AttachmentRef]:
        if not uploads:
            return []

        expected = [_attachment_size_and_sha256(upload) for upload in uploads]
        timeout = _bulk_transfer_timeout(sum(size for size, _digest in expected))

        async def post_upload() -> httpx.Response:
            with _multipart_files(uploads) as files:
                return await self.http.post(
                    self._core_api_url("upload"),
                    files=files,
                    headers=await self._csrf_headers(),
                    timeout=timeout,
                )

        response = await post_upload()
        if response.status_code == 403:
            self._csrf_token = None
            response = await post_upload()
        if self._is_login_redirect(response):
            raise A0ProtocolError("Upload requires an authenticated Agent Zero session.")
        if response.status_code >= 400:
            raise A0ProtocolError(f"Upload failed: {self._response_message(response)}")

        try:
            data = self._json(response)
        except Exception as exc:
            raise A0ProtocolError("Upload returned an invalid JSON response.") from exc
        filenames = data.get("filenames")
        if not isinstance(filenames, list) or len(filenames) != len(uploads):
            raise A0ProtocolError("Upload returned an invalid attachment response.")
        uploaded_files = data.get("files")
        if uploaded_files is not None:
            if not isinstance(uploaded_files, list) or len(uploaded_files) != len(uploads):
                raise A0ProtocolError("Upload returned invalid integrity metadata.")
            for metadata, (expected_size, expected_digest) in zip(uploaded_files, expected):
                if not isinstance(metadata, dict):
                    raise A0ProtocolError("Upload returned invalid integrity metadata.")
                if metadata.get("size") != expected_size:
                    raise A0ProtocolError(
                        f"Upload size mismatch: expected {expected_size} bytes, "
                        f"Core reported {metadata.get('size')}."
                    )
                if metadata.get("sha256") != expected_digest:
                    raise A0ProtocolError(
                        f"Upload SHA-256 mismatch: expected {expected_digest}, "
                        f"Core reported {metadata.get('sha256')}."
                    )

        refs: list[AttachmentRef] = []
        for filename, upload in zip(filenames, uploads):
            if not isinstance(filename, str) or not filename.strip():
                raise A0ProtocolError("Upload returned an invalid attachment filename.")
            normalized_name = filename.replace("\\", "/").rsplit("/", maxsplit=1)[-1]
            refs.append(
                AttachmentRef(
                    path=remote_upload_path(normalized_name),
                    name=normalized_name,
                    mime_type=upload.mime_type,
                )
            )
        return refs

    async def download_file(
        self,
        remote_path: str,
        destination: str | Path,
        *,
        expected_size: int | None = None,
    ) -> dict[str, Any]:
        target = Path(destination).expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=".partial-", dir=target.parent)
        os.close(fd)
        temp_path = Path(temp_name)
        timeout = _bulk_transfer_timeout(expected_size or 0)

        try:
            for attempt in range(2):
                headers = await self._csrf_headers()
                async with self.http.stream(
                    "GET",
                    self._core_api_url("download_work_dir_file"),
                    params={"path": remote_path},
                    headers=headers,
                    timeout=timeout,
                ) as response:
                    if response.status_code == 403 and attempt == 0:
                        self._csrf_token = None
                        continue
                    if self._is_login_redirect(response):
                        raise A0ProtocolError(
                            "Download requires an authenticated Agent Zero session."
                        )
                    if response.status_code >= 400:
                        raise A0ProtocolError(
                            f"Download failed: {self._response_message(response)}"
                        )

                    expected_digest = response.headers.get("X-Content-SHA256", "").lower()
                    if len(expected_digest) != 64:
                        raise A0ProtocolError(
                            "Download response did not include a valid X-Content-SHA256 header."
                        )
                    digest = hashlib.sha256()
                    size = 0
                    with temp_path.open("wb") as handle:
                        async for chunk in response.aiter_bytes(_FILE_CHUNK_BYTES):
                            if not chunk:
                                continue
                            handle.write(chunk)
                            digest.update(chunk)
                            size += len(chunk)
                        handle.flush()
                        os.fsync(handle.fileno())

                    actual_digest = digest.hexdigest()
                    if actual_digest != expected_digest:
                        raise A0ProtocolError(
                            f"Download SHA-256 mismatch: expected {expected_digest}, "
                            f"received {actual_digest}."
                        )
                    content_length = response.headers.get("Content-Length")
                    if content_length:
                        try:
                            declared_size = int(content_length)
                        except ValueError as exc:
                            raise A0ProtocolError(
                                "Download response included an invalid Content-Length header."
                            ) from exc
                        if declared_size != size:
                            raise A0ProtocolError(
                                f"Download size mismatch: expected {declared_size} bytes, "
                                f"received {size}."
                            )

                    os.replace(temp_path, target)
                    _fsync_directory(target.parent)
                    return {
                        "path": str(target),
                        "size": size,
                        "sha256": actual_digest,
                    }
            raise A0ProtocolError("Download failed after refreshing the CSRF token.")
        finally:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass

    async def goal_action(
        self,
        action: str,
        context_id: str,
        **payload: Any,
    ) -> dict[str, Any]:
        body = {
            "action": action,
            "context_id": context_id,
            **payload,
        }
        response = await self.http.post(
            f"{self.base_url}/api/plugins/_goal/goal",
            json=body,
            headers=await self._csrf_headers(),
        )
        if response.status_code == 403:
            self._csrf_token = None
            response = await self.http.post(
                f"{self.base_url}/api/plugins/_goal/goal",
                json=body,
                headers=await self._csrf_headers(),
            )
        if self._is_login_redirect(response):
            return {
                "ok": False,
                "message": "Goal API requires an authenticated Agent Zero session.",
                "status_code": 401,
            }
        if response.status_code >= 400:
            return {
                "ok": False,
                "message": self._response_message(response),
                "status_code": response.status_code,
            }

        data = self._json(response)
        if "ok" not in data:
            data["ok"] = True
        return data

    async def create_chat(self, *, current_context_id: str | None = None) -> str:
        payload = {}
        if current_context_id:
            payload["current_context"] = current_context_id

        response = await self._post("chat_create", payload)
        response.raise_for_status()
        data = self._json(response)
        return data.get("context_id") or data.get("ctxid", "")

    async def list_chats(self) -> list[dict[str, Any]]:
        response = await self._post("chats_list")
        response.raise_for_status()
        data = self._json(response)
        return data.get("contexts", data.get("chats", []))

    async def get_chat(self, context_id: str) -> dict[str, Any]:
        response = await self._post(
            "chat_get",
            {"context_id": context_id},
        )
        response.raise_for_status()
        return self._json(response)

    async def reset_chat(self, context_id: str) -> dict[str, Any]:
        response = await self._post(
            "chat_reset",
            {"context_id": context_id},
        )
        response.raise_for_status()
        data = self._json(response)
        if "ok" not in data:
            data["ok"] = True
        normalized_context_id = self._transfer_context_id(context_id)
        if normalized_context_id:
            await self._abort_active_transfers(
                context_id=normalized_context_id,
                reason="chat reset during transfer",
            )
        return data

    async def get_projects(self, context_id: str) -> dict[str, Any]:
        response = await self._post(
            "projects",
            {"action": "list", "context_id": context_id},
        )
        response.raise_for_status()
        return self._json(response)

    async def list_commands(self, context_id: str) -> list[dict[str, Any]]:
        response = await self.http.post(
            f"{self.base_url}/api/plugins/_commands/commands",
            json={"action": "list_effective", "context_id": context_id},
            headers=await self._csrf_headers(),
        )
        if response.status_code == 403:
            self._csrf_token = None
            response = await self.http.post(
                f"{self.base_url}/api/plugins/_commands/commands",
                json={"action": "list_effective", "context_id": context_id},
                headers=await self._csrf_headers(),
            )
        if response.status_code == 404:
            return []
        response.raise_for_status()
        commands = self._json(response).get("commands", [])
        return commands if isinstance(commands, list) else []

    async def list_skills(
        self,
        *,
        context_id: str | None = None,
        project_name: str = "",
        agent_profile: str = "",
    ) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {}
        if context_id:
            payload["context_id"] = context_id
        if project_name:
            payload["project_name"] = project_name
        if agent_profile:
            payload["agent_profile"] = agent_profile

        response = await self._post("skills_list", payload)
        response.raise_for_status()
        data = self._json(response)
        skills = data.get("data", data.get("skills", []))
        return skills if isinstance(skills, list) else []

    async def list_installed_plugins(self) -> list[dict[str, Any]]:
        response = await self._post("installed_plugins", {"action": "list"})
        response.raise_for_status()
        data = self._json(response)
        plugins = data.get("plugins", data.get("data", []))
        return plugins if isinstance(plugins, list) else []

    async def set_installed_plugin_enabled(
        self,
        plugin_name: str,
        enabled: bool,
    ) -> dict[str, Any]:
        response = await self._post(
            "installed_plugins",
            {
                "action": "set_enabled",
                "plugin_name": plugin_name,
                "enabled": enabled,
            },
        )
        if response.status_code >= 400:
            return {
                "ok": False,
                "message": self._response_message(response),
                "status_code": response.status_code,
            }

        data = self._json(response)
        if "ok" not in data:
            data["ok"] = True
        return data

    async def activate_skill(
        self,
        context_id: str,
        skill: Mapping[str, Any],
    ) -> dict[str, Any]:
        response = await self._post(
            "skills_activate",
            {
                "context_id": context_id,
                "skill": {
                    "name": str(skill.get("name") or "").strip(),
                    "path": str(skill.get("path") or "").strip(),
                },
            },
        )
        if response.status_code >= 400:
            return {
                "ok": False,
                "message": self._response_message(response),
                "status_code": response.status_code,
            }

        data = self._json(response)
        if "ok" not in data:
            data["ok"] = True
        return data

    async def get_browser_runtime(self, context_id: str | None) -> dict[str, Any]:
        response = await self._post(
            "browser_runtime",
            {"action": "get", "context_id": context_id or ""},
        )
        response.raise_for_status()
        return self._json(response)

    async def set_browser_runtime(
        self,
        context_id: str | None,
        runtime_backend: str,
        *,
        host_browser_selection: str | None = None,
        profile_mode: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "action": "set",
            "context_id": context_id or "",
            "runtime_backend": runtime_backend,
        }
        if host_browser_selection is not None:
            payload["host_browser_selection"] = host_browser_selection
        if profile_mode is not None:
            payload["profile_mode"] = profile_mode
        response = await self._post(
            "browser_runtime",
            payload,
        )
        response.raise_for_status()
        return self._json(response)

    async def activate_project(self, context_id: str, name: str) -> dict[str, Any]:
        response = await self._post(
            "projects",
            {
                "action": "activate",
                "context_id": context_id,
                "name": name,
            },
        )
        response.raise_for_status()
        return self._json(response)

    async def deactivate_project(self, context_id: str) -> dict[str, Any]:
        response = await self._post(
            "projects",
            {
                "action": "deactivate",
                "context_id": context_id,
            },
        )
        response.raise_for_status()
        return self._json(response)

    async def load_project(self, name: str) -> dict[str, Any]:
        response = await self._post(
            "projects",
            {
                "action": "load",
                "name": name,
            },
        )
        response.raise_for_status()
        return self._json(response)

    async def update_project(self, project: dict[str, Any]) -> dict[str, Any]:
        response = await self._post(
            "projects",
            {
                "action": "update",
                "project": project,
            },
        )
        response.raise_for_status()
        return self._json(response)

    async def pause_agent(
        self,
        context_id: str | None,
        *,
        paused: bool = True,
    ) -> dict[str, Any]:
        response = await self._post(
            "pause",
            {"context_id": context_id or "", "paused": paused},
        )
        if response.status_code >= 400:
            return {
                "ok": False,
                "message": self._response_message(response),
                "status_code": response.status_code,
            }

        data = self._json(response)
        if "ok" not in data:
            data["ok"] = True
        normalized_context_id = self._transfer_context_id(context_id)
        if paused and normalized_context_id:
            await self._abort_active_transfers(
                context_id=normalized_context_id,
                reason="chat paused during transfer",
            )
        return data

    async def nudge_agent(self, context_id: str | None) -> dict[str, Any]:
        response = await self._post(
            "nudge",
            {"context_id": context_id or ""},
        )
        if response.status_code >= 400:
            return {
                "ok": False,
                "message": self._response_message(response),
                "status_code": response.status_code,
            }

        data = self._json(response)
        if "ok" not in data:
            data["ok"] = True
        return data

    async def get_settings(self) -> dict[str, Any]:
        response = await self._post("settings_get")
        response.raise_for_status()
        return self._json(response)

    async def set_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        response = await self._post(
            "settings_set",
            {"settings": settings},
        )
        response.raise_for_status()
        return self._json(response)

    async def set_agent_profile(self, context_id: str, profile_key: str) -> dict[str, Any]:
        response = await self._post(
            "agent_profile_set",
            {"context_id": context_id, "agent_profile": profile_key},
        )
        if response.status_code >= 400:
            return {
                "ok": False,
                "message": self._response_message(response),
                "status_code": response.status_code,
            }

        data = self._json(response)
        if "ok" not in data:
            data["ok"] = True
        return data

    async def get_model_presets(self) -> list[dict[str, Any]]:
        response = await self._post("model_presets")
        response.raise_for_status()
        data = self._json(response)
        presets = data.get("presets", data.get("data", []))
        return presets if isinstance(presets, list) else []

    async def save_model_presets(self, presets: list[dict[str, Any]]) -> dict[str, Any]:
        response = await self._post(
            "model_presets",
            {"action": "save", "presets": presets},
        )
        if response.status_code >= 400:
            return {
                "ok": False,
                "message": self._response_message(response),
                "status_code": response.status_code,
            }

        data = self._json(response)
        if "ok" not in data:
            data["ok"] = True
        return data

    async def get_model_switcher(self, context_id: str) -> dict[str, Any]:
        response = await self._post(
            "model_switcher",
            {"action": "get", "context_id": context_id},
        )
        response.raise_for_status()
        return self._json(response)

    async def set_model_preset(self, context_id: str, preset_name: str | None) -> dict[str, Any]:
        payload: dict[str, Any] = {"context_id": context_id}
        if preset_name:
            payload["action"] = "set_preset"
            payload["preset_name"] = preset_name
        else:
            payload["action"] = "clear"
        response = await self._post("model_switcher", payload)
        response.raise_for_status()
        return self._json(response)

    async def set_model_override(
        self,
        context_id: str,
        *,
        main_model: dict[str, Any] | None = None,
        utility_model: dict[str, Any] | None = None,
        embedding_model: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "action": "set_override",
            "context_id": context_id,
            "main_model": main_model or {},
            "utility_model": utility_model or {},
            "embedding_model": embedding_model or {},
        }
        response = await self._post("model_switcher", payload)
        if response.status_code >= 400:
            return {
                "ok": False,
                "message": self._response_message(response),
                "status_code": response.status_code,
            }

        data = self._json(response)
        if "ok" not in data:
            data["ok"] = True
        return data

    async def get_compaction_stats(self, context_id: str) -> dict[str, Any]:
        response = await self._post(
            "compact_chat",
            {"context_id": context_id, "action": "stats"},
        )
        if response.status_code >= 400:
            return {
                "ok": False,
                "message": self._response_message(response),
                "status_code": response.status_code,
            }

        data = self._json(response)
        if "ok" not in data:
            data["ok"] = True
        return data

    async def get_token_status(self, context_id: str) -> dict[str, Any]:
        response = await self._post(
            "token_status",
            {"context_id": context_id},
        )
        if response.status_code >= 400:
            return {
                "ok": False,
                "message": self._response_message(response),
                "status_code": response.status_code,
            }

        data = self._json(response)
        if "ok" not in data:
            data["ok"] = True
        return data

    async def compact_chat(
        self,
        context_id: str,
        *,
        use_chat_model: bool,
        preset_name: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "context_id": context_id,
            "action": "compact",
            "use_chat_model": use_chat_model,
        }
        if preset_name:
            payload["preset_name"] = preset_name

        response = await self._post("compact_chat", payload)
        if response.status_code >= 400:
            return {
                "ok": False,
                "message": self._response_message(response),
                "status_code": response.status_code,
            }

        data = self._json(response)
        if "ok" not in data:
            data["ok"] = True
        return data

    async def disconnect(self, *, close_http: bool = True, notify: bool = True) -> None:
        previous_suppression = self._suppress_disconnect_callback
        if not notify:
            self._suppress_disconnect_callback = True
        try:
            if self.sio.connected:
                await self._abort_active_transfers(
                    reason="connector disconnected during transfer",
                )
                await self.sio.disconnect()
        finally:
            self._suppress_disconnect_callback = previous_suppression
            self.connected = False
            self._clear_transfers("connector disconnected during transfer")
        if close_http:
            await self.http.aclose()

    async def logout(self) -> None:
        await self.http.get(self._logout_url(), follow_redirects=False)
        self._csrf_token = None

    def clear_session(self) -> None:
        self.http.cookies.clear()
        self._csrf_token = None
