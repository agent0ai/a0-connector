from __future__ import annotations

from typing import Any, Callable

import httpx
import socketio


class A0Client:
    """Client for communicating with a running Agent Zero instance."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(10.0))
        self.sio = socketio.AsyncClient()
        self.csrf_token: str | None = None
        self.runtime_id: str | None = None
        self.connected = False
        self.authenticated = False
        self.contexts: list[dict[str, Any]] = []

        self.on_state_push: Callable[[dict[str, Any]], None] | None = None
        self.on_connect: Callable[[], None] | None = None
        self.on_disconnect: Callable[[], None] | None = None

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    async def check_health(self) -> bool:
        try:
            response = await self.http.get(self._url("/health"), timeout=5.0)
        except (httpx.ConnectError, httpx.TimeoutException):
            return False
        return response.status_code == 200

    async def needs_auth(self) -> bool:
        response = await self.http.get(self._url("/csrf_token"), follow_redirects=False)
        if response.status_code == 200:
            data = response.json()
            token = data.get("token") or data.get("csrf_token")
            if token:
                self.csrf_token = token
                self.runtime_id = data.get("runtime_id")
            return False
        if response.status_code == 302 and response.headers.get("location") == "/login":
            return True
        return response.status_code in {401, 403}

    async def login(self, username: str, password: str) -> bool:
        response = await self.http.post(
            self._url("/login"),
            data={"username": username, "password": password},
            follow_redirects=False,
        )
        if response.status_code in {200, 302}:
            self.authenticated = bool(self.http.cookies)
            return self.authenticated
        return False

    async def _fetch_csrf_token(self) -> str:
        response = await self.http.get(self._url("/csrf_token"), follow_redirects=False)
        if response.status_code != 200:
            raise RuntimeError("Failed to fetch CSRF token")
        data = response.json()
        token = data.get("token") or data.get("csrf_token")
        if not token:
            raise RuntimeError("CSRF token missing from response")
        self.csrf_token = token
        self.runtime_id = data.get("runtime_id")
        return token

    async def _ensure_csrf(self) -> None:
        if not self.csrf_token:
            await self._fetch_csrf_token()

    def _get_headers(self) -> dict[str, str]:
        if self.csrf_token:
            return {"X-CSRF-Token": self.csrf_token}
        return {}

    def _build_cookie_header(self) -> str:
        parts: list[str] = []
        existing_names: set[str] = set()
        for cookie in self.http.cookies.jar:
            parts.append(f"{cookie.name}={cookie.value}")
            existing_names.add(cookie.name)
        if self.runtime_id and self.csrf_token:
            csrf_cookie = f"csrf_token_{self.runtime_id}"
            if csrf_cookie not in existing_names:
                parts.append(f"{csrf_cookie}={self.csrf_token}")
        return "; ".join(parts)

    async def connect_websocket(self) -> None:
        await self._ensure_csrf()
        cookie_header = self._build_cookie_header()
        headers = {
            "Cookie": cookie_header,
            "Origin": self.base_url,
            "Referer": f"{self.base_url}/",
        }

        async def _on_state_push(data: dict[str, Any]) -> None:
            self._handle_state_push(data)

        async def _on_connect() -> None:
            self.connected = True
            callback = self.on_connect
            if callback is not None:
                callback()

        async def _on_disconnect() -> None:
            self.connected = False
            callback = self.on_disconnect
            if callback is not None:
                callback()

        self.sio.on("state_push", handler=_on_state_push, namespace="/state_sync")
        self.sio.on("connect", handler=_on_connect, namespace="/state_sync")
        self.sio.on("disconnect", handler=_on_disconnect, namespace="/state_sync")

        await self.sio.connect(
            self.base_url,
            namespaces=["/state_sync"],
            auth={"csrf_token": self.csrf_token},
            headers=headers,
            transports=["websocket"],
        )

    def _handle_state_push(self, data: dict[str, Any]) -> None:
        payload = data.get("data", data)
        snapshot = payload.get("snapshot", payload)
        contexts = snapshot.get("contexts")
        if isinstance(contexts, list):
            self.contexts = contexts
        callback = self.on_state_push
        if callback is not None:
            callback(data)

    async def request_state(self, context_id: str | None, log_from: int = 0) -> dict[str, Any]:
        payload = {
            "context": context_id,
            "log_from": log_from,
            "notifications_from": 0,
            "timezone": "UTC",
        }
        response = await self.sio.call("state_request", payload, namespace="/state_sync")
        if isinstance(response, dict) and "results" in response:
            results = response.get("results") or []
            if results and isinstance(results[0], dict):
                data = results[0].get("data")
                return data if isinstance(data, dict) else {}
        return response if isinstance(response, dict) else {}

    async def _api_call(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        await self._ensure_csrf()
        extra_headers = kwargs.pop("headers", {})
        headers = {**self._get_headers(), **extra_headers}
        response = await self.http.request(method, self._url(path), headers=headers, **kwargs)
        if response.status_code == 403:
            await self._fetch_csrf_token()
            headers = {**self._get_headers(), **extra_headers}
            response = await self.http.request(method, self._url(path), headers=headers, **kwargs)
        return response

    async def create_chat(self) -> str:
        response = await self._api_call("POST", "/chat_create", json={})
        response.raise_for_status()
        data = response.json()
        return data.get("ctxid", "")

    async def list_chats(self) -> list[dict[str, Any]]:
        return self.contexts

    async def remove_chat(self, context_id: str) -> None:
        response = await self._api_call("POST", "/chat_remove", json={"context": context_id})
        response.raise_for_status()

    async def send_message(self, text: str, context_id: str) -> dict[str, Any]:
        response = await self._api_call(
            "POST",
            "/message_async",
            json={"text": text, "context": context_id},
        )
        response.raise_for_status()
        return response.json()

    async def disconnect(self) -> None:
        if self.sio.connected:
            await self.sio.disconnect()
        await self.http.aclose()
