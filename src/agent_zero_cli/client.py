"""A0Client for the current Agent Zero connector API over HTTP + `/ws`."""

from __future__ import annotations

import asyncio
import base64
from http.cookiejar import Cookie
import json
import posixpath
import time
import uuid
from typing import Any, Callable, Mapping
from urllib.parse import unquote, urlparse

import aiohttp
import httpx
import socketio

from agent_zero_cli import __version__
from agent_zero_cli.attachments import AttachmentRef, AttachmentUpload, remote_upload_path

_PLUGIN_API = "/api/plugins/_a0_connector/v1"
_ACP_PLUGIN_API = "/api/plugins/_a0_acp"
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
_FILE_OP_RESULT_CHUNK_BYTES = 64 * 1024
_FILE_OP_RESULT_CHUNK_ENCODING = "json+base64"

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


class A0ProtocolError(RuntimeError):
    """Raised when the connector returns an application-level error."""


class A0ConnectorPluginMissingError(RuntimeError):
    """HTTP 404 on the connector API — the _a0_connector plugin is not loaded on Agent Zero."""


class A0WebSocketConnectionError(RuntimeError):
    """WebSocket/Socket.IO connection failed with a user-facing message."""


def _container_reference_path(root: str, directory: str = "") -> str:
    normalized_root = posixpath.normpath(str(root or "").replace("\\", "/"))
    if not normalized_root.startswith("/"):
        raise ValueError("Container workspace path must be absolute.")

    normalized_directory = str(directory or "").replace("\\", "/").strip("/")
    if ".." in normalized_directory.split("/"):
        raise ValueError("Container reference path is outside the active workspace.")

    target = posixpath.normpath(posixpath.join(normalized_root, normalized_directory))
    try:
        contained = posixpath.commonpath((normalized_root, target)) == normalized_root
    except ValueError:
        contained = False
    if not contained:
        raise ValueError("Container reference path is outside the active workspace.")
    return target


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
    if not _VERIFY_TLS_CERTIFICATES:
        # Some python-engineio/aiohttp combinations still let the WebSocket
        # upgrade fall back to aiohttp's default SSL context. Make the intent
        # explicit for ws_connect too, not only for the Engine.IO HTTP probe.
        kwargs["websocket_extra_options"] = {"ssl": False}
    return kwargs


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

    async def fetch_image(
        self,
        path: str,
        *,
        max_bytes: int = 25 * 1024 * 1024,
    ) -> tuple[bytes, str]:
        """Load one same-origin image through Agent Zero's authenticated session."""
        self._validate_image_path(path)

        transient_retry_available = True
        csrf_retry_available = True
        while True:
            try:
                async with self.http.stream(
                    "GET",
                    self._core_api_url("image_get"),
                    params={"path": path},
                    headers=await self._csrf_headers(),
                    follow_redirects=False,
                ) as response:
                    if response.status_code == 403 and csrf_retry_available:
                        csrf_retry_available = False
                        self._csrf_token = None
                        continue
                    if response.status_code in {502, 503, 504}:
                        if transient_retry_available:
                            transient_retry_available = False
                            continue
                        raise A0ProtocolError(
                            f"Image request failed with HTTP {response.status_code}."
                        )
                    return await self._read_image_response(response, max_bytes=max_bytes)
            except httpx.TransportError as exc:
                if transient_retry_available:
                    transient_retry_available = False
                    continue
                raise A0ProtocolError("Image request failed.") from exc

    def _validate_image_path(self, path: str) -> None:
        if (
            not isinstance(path, str)
            or not path.startswith("/a0/")
            or path.startswith("//")
            or "?" in path
            or "#" in path
        ):
            raise A0ProtocolError("Image path must be a safe Agent Zero path.")

        decoded = path
        decode_pass_limit = max(1, len(path) // 2 + 1)
        for _ in range(decode_pass_limit):
            expanded = unquote(decoded)
            if expanded == decoded:
                break
            decoded = expanded
        else:
            if unquote(decoded) != decoded:
                raise A0ProtocolError("Image path must be a safe Agent Zero path.")
        segments = decoded.split("/")
        if (
            not decoded.startswith("/a0/")
            or "\\" in decoded
            or "?" in decoded
            or "#" in decoded
            or any(segment in {".", ".."} for segment in segments)
            or any(ord(character) < 32 for character in decoded)
        ):
            raise A0ProtocolError("Image path must be a safe Agent Zero path.")

    async def _read_image_response(
        self,
        response: httpx.Response,
        *,
        max_bytes: int,
    ) -> tuple[bytes, str]:
        if self._is_login_redirect(response):
            raise A0ProtocolError("Image request requires an authenticated Agent Zero session.")
        if 300 <= response.status_code < 400:
            raise A0ProtocolError("Image request returned an unexpected redirect.")
        if response.status_code >= 400:
            raise A0ProtocolError(f"Image request failed with HTTP {response.status_code}.")

        content_length = response.headers.get("content-length")
        if content_length:
            try:
                declared_length = int(content_length)
            except (TypeError, ValueError):
                raise A0ProtocolError("Image response included an invalid Content-Length.") from None
            if declared_length < 0 or declared_length > max_bytes:
                raise A0ProtocolError("Image response exceeds the size limit.")

        mime = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if mime == "image/jpg":
            mime = "image/jpeg"
        if not mime.startswith("image/"):
            raise A0ProtocolError("Image response did not include an image MIME type.")

        chunks: list[bytes] = []
        total_bytes = 0
        async for chunk in response.aiter_bytes():
            total_bytes += len(chunk)
            if total_bytes > max_bytes:
                raise A0ProtocolError("Image response exceeds the size limit.")
            chunks.append(chunk)
        return b"".join(chunks), mime

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

    async def acp_session(self, action: str, **payload: Any) -> dict[str, Any]:
        response = await self.http.post(
            f"{self.base_url}{_ACP_PLUGIN_API}/session",
            json={"action": action, **payload},
            follow_redirects=False,
        )
        if self._is_login_redirect(response):
            raise A0ProtocolError("ACP configuration requires an authenticated Agent Zero session.")
        if response.status_code >= 400:
            raise A0ProtocolError(f"ACP {action} failed: {self._response_message(response)}")
        return self._json(response)

    async def _call(self, event: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        response = await self.sio.call(
            event,
            payload or {},
            namespace=WS_NAMESPACE,
        )
        return self._raise_for_results(response, event)

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
            request = self._unwrap_envelope(payload)
            result = await self._handle_file_op(request)
            for result_payload in self._file_op_result_payloads(result):
                await self.sio.emit(
                    _EVENT_FILE_OP_RESULT,
                    result_payload,
                    namespace=WS_NAMESPACE,
                )

        @self.sio.on(_EVENT_EXEC_OP, namespace=WS_NAMESPACE)
        async def _on_exec_op(payload: dict[str, Any]) -> None:
            request = self._unwrap_envelope(payload)
            result = await self._handle_exec_op(request)
            await self.sio.emit(
                _EVENT_EXEC_OP_RESULT,
                result,
                namespace=WS_NAMESPACE,
            )

        @self.sio.on(_EVENT_COMPUTER_USE_OP, namespace=WS_NAMESPACE)
        async def _on_computer_use_op(payload: dict[str, Any]) -> None:
            request = self._unwrap_envelope(payload)
            result = await self._handle_computer_use_op(request)
            await self.sio.emit(
                _EVENT_COMPUTER_USE_OP_RESULT,
                result,
                namespace=WS_NAMESPACE,
            )
            self._notify_op_result_sent(
                self.on_computer_use_op_result_sent,
                request,
                result,
            )

        @self.sio.on(_EVENT_BROWSER_OP, namespace=WS_NAMESPACE)
        async def _on_browser_op(payload: dict[str, Any]) -> None:
            request = self._unwrap_envelope(payload)
            result = await self._handle_browser_op(request)
            await self.sio.emit(
                _EVENT_BROWSER_OP_RESULT,
                result,
                namespace=WS_NAMESPACE,
            )
            self._notify_op_result_sent(
                self.on_browser_op_result_sent,
                request,
                result,
            )

        @self.sio.on(_EVENT_GATEWAY_CONTROL, namespace=WS_NAMESPACE)
        async def _on_gateway_control(payload: dict[str, Any]) -> None:
            request = self._unwrap_envelope(payload)
            result = await self._handle_gateway_control(request)
            await self.sio.emit(
                _EVENT_GATEWAY_CONTROL_RESULT,
                result,
                namespace=WS_NAMESPACE,
            )
            self._notify_op_result_sent(
                self.on_gateway_control_result_sent,
                request,
                result,
            )

        self._events_registered = True

    def _file_op_result_payloads(self, result: dict[str, Any]) -> list[dict[str, Any]]:
        raw = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(raw) <= _FILE_OP_RESULT_CHUNK_BYTES:
            return [result]

        op_id = str(result.get("op_id") or "")
        chunks = [
            raw[index : index + _FILE_OP_RESULT_CHUNK_BYTES]
            for index in range(0, len(raw), _FILE_OP_RESULT_CHUNK_BYTES)
        ]
        chunk_count = len(chunks)
        return [
            {
                "op_id": op_id,
                "chunked": True,
                "chunk_index": index,
                "chunk_count": chunk_count,
                "encoding": _FILE_OP_RESULT_CHUNK_ENCODING,
                "data": base64.b64encode(chunk).decode("ascii"),
            }
            for index, chunk in enumerate(chunks)
        ]

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
        return self._json(response)

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
        return await self._call(_EVENT_HELLO, payload)

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

        files = [
            ("file", (upload.filename, upload.content, upload.mime_type))
            for upload in uploads
        ]
        response = await self.http.post(
            self._core_api_url("upload"),
            files=files,
            headers=await self._csrf_headers(),
        )
        if response.status_code == 403:
            self._csrf_token = None
            response = await self.http.post(
                self._core_api_url("upload"),
                files=files,
                headers=await self._csrf_headers(),
            )
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

    async def create_chat(
        self,
        *,
        current_context_id: str | None = None,
        agent_profile: str | None = None,
        project_name: str | None = None,
    ) -> str:
        payload = {}
        if current_context_id:
            payload["current_context"] = current_context_id
        if agent_profile:
            payload["agent_profile"] = agent_profile
        if project_name:
            payload["project_name"] = project_name

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

    async def get_chat_files_path(self, context_id: str) -> str:
        response = await self.http.post(
            self._core_api_url("chat_files_path_get"),
            json={"ctxid": context_id},
            headers=await self._csrf_headers(),
        )
        if response.status_code == 403:
            self._csrf_token = None
            response = await self.http.post(
                self._core_api_url("chat_files_path_get"),
                json={"ctxid": context_id},
                headers=await self._csrf_headers(),
            )
        if self._is_login_redirect(response):
            raise A0ProtocolError("Container workspace lookup requires an authenticated Agent Zero session.")
        response.raise_for_status()
        return str(self._json(response).get("path") or "").strip()

    async def list_container_reference_entries(
        self,
        root: str,
        directory: str = "",
    ) -> list[dict[str, Any]]:
        target = _container_reference_path(root, directory)
        response = await self.http.get(
            self._core_api_url("get_work_dir_files"),
            params={"path": target},
            headers=await self._csrf_headers(),
        )
        if response.status_code == 403:
            self._csrf_token = None
            response = await self.http.get(
                self._core_api_url("get_work_dir_files"),
                params={"path": target},
                headers=await self._csrf_headers(),
            )
        if self._is_login_redirect(response):
            raise A0ProtocolError("Container workspace lookup requires an authenticated Agent Zero session.")
        response.raise_for_status()

        data = self._json(response).get("data", {})
        entries = data.get("entries", []) if isinstance(data, Mapping) else []
        result: list[dict[str, Any]] = []
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, Mapping) or entry.get("is_symlink"):
                continue
            normalized_root = _container_reference_path(root)
            path = posixpath.normpath("/" + str(entry.get("path") or "").replace("\\", "/").lstrip("/"))
            try:
                contained = posixpath.commonpath((normalized_root, path)) == normalized_root
            except ValueError:
                contained = False
            if not contained or posixpath.dirname(path) != target:
                continue
            name = str(entry.get("name") or "").strip()
            if not name or posixpath.basename(path) != name:
                continue
            result.append({"name": name, "path": path, "is_dir": bool(entry.get("is_dir"))})
        return result

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

    async def agent_editor(self, action: str, **payload: Any) -> dict[str, Any]:
        response = await self._post("agent_editor", {"action": action, **payload})
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
                await self.sio.disconnect()
        finally:
            self._suppress_disconnect_callback = previous_suppression
            self.connected = False
        if close_http:
            await self.http.aclose()

    async def logout(self) -> None:
        await self.http.get(self._logout_url(), follow_redirects=False)
        self._csrf_token = None

    def clear_session(self) -> None:
        self.http.cookies.clear()
        self._csrf_token = None
