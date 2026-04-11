"""A0Client for the current Agent Zero connector API over HTTP + `/ws`."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any, Callable
from urllib.parse import urlparse

import aiohttp
import httpx
import socketio

from agent_zero_cli import __version__

_PLUGIN_API = "/api/plugins/_a0_connector/v1"
DEFAULT_HOST = "http://127.0.0.1:5080"
PROTOCOL_VERSION = "a0-connector.v1"
_SOCKET_IO_PATH = "/socket.io"
WS_NAMESPACE = "/ws"
WS_HANDLER = "plugins/_a0_connector/ws_connector"

_EVENT_HELLO = "connector_hello"
_EVENT_SUBSCRIBE = "connector_subscribe_context"
_EVENT_UNSUBSCRIBE = "connector_unsubscribe_context"
_EVENT_SEND_MESSAGE = "connector_send_message"
_EVENT_CONTEXT_SNAPSHOT = "connector_context_snapshot"
_EVENT_CONTEXT_EVENT = "connector_context_event"
_EVENT_CONTEXT_COMPLETE = "connector_context_complete"
_EVENT_FILE_OP = "connector_file_op"
_EVENT_FILE_OP_RESULT = "connector_file_op_result"
_EVENT_EXEC_OP = "connector_exec_op"
_EVENT_EXEC_OP_RESULT = "connector_exec_op_result"
_EVENT_REMOTE_TREE_UPDATE = "connector_remote_tree_update"
_EVENT_ERROR = "connector_error"

_SOCKET_IO_PROBE_QUERY = {"transport": "polling", "EIO": "4"}
_BLANK_SOCKET_IO_REJECTION = "server rejected the Socket.IO connection without an error message"


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


class A0Client:
    """Client for communicating with a running Agent Zero instance."""

    def __init__(self, base_url: str) -> None:
        _ensure_aiohttp_ws_timeout_compat()
        self.base_url = base_url.rstrip("/")
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(10.0))
        self.sio = socketio.AsyncClient()
        self.connected = False
        self._events_registered = False
        self._last_connect_error: Any = None

        self.on_connect: Callable[[], None] | None = None
        self.on_disconnect: Callable[[], None] | None = None
        self.on_context_event: Callable[[dict[str, Any]], None] | None = None
        self.on_context_snapshot: Callable[[dict[str, Any]], None] | None = None
        self.on_context_complete: Callable[[dict[str, Any]], None] | None = None
        self.on_error: Callable[[dict[str, Any]], None] | None = None
        self.on_file_op: Callable[[dict[str, Any]], Any] | None = None
        self.on_exec_op: Callable[[dict[str, Any]], Any] | None = None

    def _api_url(self, endpoint: str) -> str:
        return f"{self.base_url}{_PLUGIN_API}/{endpoint}"

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

        @self.sio.on(_EVENT_ERROR, namespace=WS_NAMESPACE)
        async def _on_error(payload: dict[str, Any]) -> None:
            callback = self.on_error
            if callback is not None:
                callback(self._unwrap_envelope(payload))

        @self.sio.on(_EVENT_FILE_OP, namespace=WS_NAMESPACE)
        async def _on_file_op(payload: dict[str, Any]) -> None:
            request = self._unwrap_envelope(payload)
            result = await self._handle_file_op(request)
            await self.sio.emit(
                _EVENT_FILE_OP_RESULT,
                result,
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

        self._events_registered = True

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

    async def fetch_capabilities(self) -> dict[str, Any]:
        response = await self._post("capabilities")
        if response.status_code == 404:
            raise A0ConnectorPluginMissingError(
                "HTTP 404 — the builtin _a0_connector plugin is not available on this Agent Zero server.\n"
                "\n"
                "The web UI can work while this endpoint is missing: the CLI needs the plugin.\n"
                "For this workspace, the intended builtin plugin path is:\n"
                "  /home/eclypso/agentdocker/plugins/_a0_connector\n"
                "Ensure Agent Zero Core includes that builtin plugin at /a0/plugins/_a0_connector,\n"
                "then restart Agent Zero. On a remote host, update Agent Zero Core before retrying."
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

    async def connect_websocket(self) -> None:
        self._register_event_handlers()
        self._last_connect_error = None
        await self._probe_socketio_transport()
        try:
            await self.sio.connect(
                self.base_url,
                namespaces=[WS_NAMESPACE],
                headers=self._ws_headers(),
                auth=self._ws_auth(),
            )
        except Exception as exc:
            raise A0WebSocketConnectionError(self._format_namespace_rejection_error(exc)) from exc

    async def send_hello(self) -> dict[str, Any]:
        return await self._call(
            _EVENT_HELLO,
            {
                "protocol": PROTOCOL_VERSION,
                "client": "a0",
                "client_version": __version__,
            },
        )

    async def subscribe_context(self, context_id: str, from_seq: int = 0) -> dict[str, Any]:
        return await self._call(
            _EVENT_SUBSCRIBE,
            {"context_id": context_id, "from": from_seq},
        )

    async def send_remote_tree_update(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._call(_EVENT_REMOTE_TREE_UPDATE, payload)

    async def unsubscribe_context(self, context_id: str) -> dict[str, Any]:
        return await self._call(
            _EVENT_UNSUBSCRIBE,
            {"context_id": context_id},
        )

    async def send_message(self, text: str, context_id: str) -> dict[str, Any]:
        return await self._call(
            _EVENT_SEND_MESSAGE,
            {
                "context_id": context_id,
                "message": text,
                "client_message_id": str(uuid.uuid4()),
            },
        )

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

    async def get_projects(self, context_id: str) -> dict[str, Any]:
        response = await self._post(
            "projects",
            {"action": "list", "context_id": context_id},
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

    async def get_model_presets(self) -> list[dict[str, Any]]:
        response = await self._post("model_presets")
        response.raise_for_status()
        data = self._json(response)
        presets = data.get("presets", data.get("data", []))
        return presets if isinstance(presets, list) else []

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
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "action": "set_override",
            "context_id": context_id,
            "main_model": main_model or {},
            "utility_model": utility_model or {},
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

    async def disconnect(self, *, close_http: bool = True) -> None:
        if self.sio.connected:
            await self.sio.disconnect()
        if close_http:
            await self.http.aclose()

    async def logout(self) -> None:
        await self.http.get(self._logout_url(), follow_redirects=False)

    def clear_session(self) -> None:
        self.http.cookies.clear()
