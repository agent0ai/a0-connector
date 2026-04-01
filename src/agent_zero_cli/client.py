"""A0Client for the current Agent Zero connector API over HTTP + `/ws`."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any, Callable

import httpx
import socketio


_PLUGIN_API = "/api/plugins/a0_connector/v1"
_PROTOCOL_VERSION = "a0-connector.v1"
_WS_NAMESPACE = "/ws"
_WS_HANDLER = "plugins/a0_connector/ws_connector"

_EVENT_HELLO = "connector_hello"
_EVENT_SUBSCRIBE = "connector_subscribe_context"
_EVENT_UNSUBSCRIBE = "connector_unsubscribe_context"
_EVENT_SEND_MESSAGE = "connector_send_message"
_EVENT_CONTEXT_SNAPSHOT = "connector_context_snapshot"
_EVENT_CONTEXT_EVENT = "connector_context_event"
_EVENT_CONTEXT_COMPLETE = "connector_context_complete"
_EVENT_FILE_OP = "connector_file_op"
_EVENT_FILE_OP_RESULT = "connector_file_op_result"
_EVENT_ERROR = "connector_error"


class A0ProtocolError(RuntimeError):
    """Raised when the connector returns an application-level error."""


class A0Client:
    """Client for communicating with a running Agent Zero instance."""

    def __init__(self, base_url: str, *, api_key: str = "") -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(10.0))
        self.sio = socketio.AsyncClient()
        self.connected = False
        self._events_registered = False

        self.on_connect: Callable[[], None] | None = None
        self.on_disconnect: Callable[[], None] | None = None
        self.on_context_event: Callable[[dict[str, Any]], None] | None = None
        self.on_context_snapshot: Callable[[dict[str, Any]], None] | None = None
        self.on_context_complete: Callable[[dict[str, Any]], None] | None = None
        self.on_error: Callable[[dict[str, Any]], None] | None = None
        self.on_file_op: Callable[[dict[str, Any]], Any] | None = None

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def _api_url(self, endpoint: str) -> str:
        return f"{self.base_url}{_PLUGIN_API}/{endpoint}"

    def _api_headers(self, *, require_api_key: bool = False) -> dict[str, str]:
        headers: dict[str, str] = {}
        if require_api_key and self.api_key:
            headers["X-API-KEY"] = self.api_key
        return headers

    def _ws_auth(self) -> dict[str, Any]:
        auth: dict[str, Any] = {"handlers": [_WS_HANDLER]}
        if self.api_key:
            auth["api_key"] = self.api_key
        return auth

    def _ws_headers(self) -> dict[str, str]:
        return {
            "Origin": self.base_url,
            "Referer": f"{self.base_url}/",
        }

    def _unwrap_envelope(self, payload: dict[str, Any] | None) -> dict[str, Any]:
        if not isinstance(payload, dict):
            return {}
        nested = payload.get("data")
        if isinstance(nested, dict):
            return nested
        return payload

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

    async def _post(
        self,
        endpoint: str,
        payload: dict[str, Any] | None = None,
        *,
        require_api_key: bool = True,
    ) -> httpx.Response:
        return await self.http.post(
            self._api_url(endpoint),
            json=payload or {},
            headers=self._api_headers(require_api_key=require_api_key),
        )

    async def _call(self, event: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        response = await self.sio.call(
            event,
            payload or {},
            namespace=_WS_NAMESPACE,
        )
        return self._raise_for_results(response, event)

    def _register_event_handlers(self) -> None:
        if self._events_registered:
            return

        @self.sio.on("connect", namespace=_WS_NAMESPACE)
        async def _on_connect() -> None:
            self.connected = True
            callback = self.on_connect
            if callback is not None:
                callback()

        @self.sio.on("disconnect", namespace=_WS_NAMESPACE)
        async def _on_disconnect() -> None:
            self.connected = False
            callback = self.on_disconnect
            if callback is not None:
                callback()

        @self.sio.on(_EVENT_CONTEXT_SNAPSHOT, namespace=_WS_NAMESPACE)
        async def _on_context_snapshot(payload: dict[str, Any]) -> None:
            callback = self.on_context_snapshot
            if callback is not None:
                callback(self._unwrap_envelope(payload))

        @self.sio.on(_EVENT_CONTEXT_EVENT, namespace=_WS_NAMESPACE)
        async def _on_context_event(payload: dict[str, Any]) -> None:
            callback = self.on_context_event
            if callback is not None:
                callback(self._unwrap_envelope(payload))

        @self.sio.on(_EVENT_CONTEXT_COMPLETE, namespace=_WS_NAMESPACE)
        async def _on_context_complete(payload: dict[str, Any]) -> None:
            callback = self.on_context_complete
            if callback is not None:
                callback(self._unwrap_envelope(payload))

        @self.sio.on(_EVENT_ERROR, namespace=_WS_NAMESPACE)
        async def _on_error(payload: dict[str, Any]) -> None:
            callback = self.on_error
            if callback is not None:
                callback(self._unwrap_envelope(payload))

        @self.sio.on(_EVENT_FILE_OP, namespace=_WS_NAMESPACE)
        async def _on_file_op(payload: dict[str, Any]) -> None:
            request = self._unwrap_envelope(payload)
            result = await self._handle_file_op(request)
            await self.sio.emit(
                _EVENT_FILE_OP_RESULT,
                result,
                namespace=_WS_NAMESPACE,
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

    async def fetch_capabilities(self) -> dict[str, Any]:
        response = await self._post("capabilities", require_api_key=False)
        response.raise_for_status()
        return response.json()

    async def check_health(self) -> bool:
        try:
            response = await self._post("capabilities", require_api_key=False)
        except (httpx.ConnectError, httpx.TimeoutException):
            return False
        except Exception:
            return False
        return response.status_code == 200

    async def verify_api_key(self) -> bool:
        response = await self._post("chats_list")
        if response.status_code == 200:
            return True
        if response.status_code in {401, 403}:
            return False
        response.raise_for_status()
        return False

    async def connect_websocket(self) -> None:
        self._register_event_handlers()
        await self.sio.connect(
            self.base_url,
            namespaces=[_WS_NAMESPACE],
            headers=self._ws_headers(),
            auth=self._ws_auth(),
            transports=["websocket"],
        )

    async def send_hello(self) -> dict[str, Any]:
        return await self._call(
            _EVENT_HELLO,
            {
                "protocol": _PROTOCOL_VERSION,
                "client": "agent-zero-cli",
                "client_version": "0.1.0",
            },
        )

    async def subscribe_context(self, context_id: str, from_seq: int = 0) -> dict[str, Any]:
        return await self._call(
            _EVENT_SUBSCRIBE,
            {"context_id": context_id, "from": from_seq},
        )

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

    async def create_chat(self) -> str:
        response = await self._post("chat_create")
        response.raise_for_status()
        data = response.json()
        return data.get("context_id") or data.get("ctxid", "")

    async def list_chats(self) -> list[dict[str, Any]]:
        response = await self._post("chats_list")
        response.raise_for_status()
        data = response.json()
        return data.get("contexts", data.get("chats", []))

    async def remove_chat(self, context_id: str) -> None:
        response = await self._post(
            "chat_delete",
            {"context_id": context_id},
        )
        response.raise_for_status()

    async def list_projects(self) -> list[dict[str, Any]]:
        response = await self._post("projects_list")
        response.raise_for_status()
        data = response.json()
        return data.get("projects", [])

    async def disconnect(self) -> None:
        if self.sio.connected:
            await self.sio.disconnect()
        await self.http.aclose()
