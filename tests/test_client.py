from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import ssl
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, Mock, call

from aiohttp import web
import aiohttp
import httpx
import pytest
import socketio

from agent_zero_cli.attachments import AttachmentUpload
from agent_zero_cli.client import (
    A0_WS_MAX_PAYLOAD_BYTES,
    A0Client,
    A0ConnectorPluginMissingError,
    A0ProtocolError,
    A0WebSocketConnectionError,
    TRANSFER_PROTOCOL_VERSION,
    _bulk_transfer_timeout,
    _ensure_aiohttp_ws_timeout_compat,
    _socketio_event_size,
    _socketio_client_kwargs,
)
from agent_zero_cli.config import (
    load_config,
    normalize_computer_use_trust_mode,
    save_computer_use_enabled,
    save_computer_use_restore_token,
    save_computer_use_trust_mode,
    save_env,
    save_last_context,
    save_remember_host,
)


pytestmark = pytest.mark.anyio

_FIXTURES_DIR = Path(__file__).with_name("fixtures")
_SELF_SIGNED_CERT = _FIXTURES_DIR / "localhost-selfsigned.crt"
_SELF_SIGNED_KEY = _FIXTURES_DIR / "localhost-selfsigned.key"


@asynccontextmanager
async def self_signed_connector_server():
    sio_server = socketio.AsyncServer(async_mode="aiohttp", cors_allowed_origins="*")
    app = web.Application()
    sio_server.attach(app)

    async def socketio_probe_alias(_request: web.Request) -> web.Response:
        return web.Response(
            text='0{"sid":"probe-sid","upgrades":["websocket"],"pingInterval":25000,"pingTimeout":20000}',
            content_type="text/plain",
        )

    async def capabilities(_request: web.Request) -> web.Response:
        return web.json_response(
            {
                "protocol": "a0-connector.v1",
                "websocket_namespace": "/ws",
                "websocket_handlers": ["plugins/_a0_connector/ws_connector"],
                "auth": ["session"],
                "auth_required": False,
                "features": [],
            }
        )

    async def chats_list(_request: web.Request) -> web.Response:
        return web.json_response({"contexts": []})

    app.router.add_get("/socket.io", socketio_probe_alias)
    app.router.add_post("/api/plugins/_a0_connector/v1/capabilities", capabilities)
    app.router.add_post("/api/plugins/_a0_connector/v1/chats_list", chats_list)

    @sio_server.event(namespace="/ws")
    async def connect(_sid: str, _environ: dict, auth: dict | None) -> bool:
        return auth == {"handlers": ["plugins/_a0_connector/ws_connector"]}

    ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ssl_context.load_cert_chain(_SELF_SIGNED_CERT, _SELF_SIGNED_KEY)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=ssl_context)
    await site.start()
    port = runner.addresses[0][1]

    try:
        yield f"https://127.0.0.1:{port}"
    finally:
        await runner.cleanup()


class FakeResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        json_data: dict | None = None,
        headers: dict | None = None,
        text: str = "",
        content: bytes = b"",
        chunks: list[bytes] | None = None,
        iter_error: httpx.TransportError | None = None,
    ) -> None:
        self.status_code = status_code
        self._json_data = json_data or {}
        self.headers = headers or {}
        self.text = text
        self.content = content
        self.chunks = chunks
        self.iter_error = iter_error
        self.read_chunks = 0

    async def __aenter__(self) -> "FakeResponse":
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None

    async def aiter_bytes(self):
        for chunk in self.chunks if self.chunks is not None else (self.content,):
            self.read_chunks += 1
            yield chunk
        if self.iter_error is not None:
            raise self.iter_error

    def json(self) -> dict:
        return self._json_data

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("POST", "http://example.test")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError("error", request=request, response=response)


async def test_acp_session_posts_to_the_bundled_plugin_endpoint() -> None:
    client = A0Client("http://agent.test")
    client.http = Mock()
    client.http.post = AsyncMock(return_value=FakeResponse(json_data={"ok": True}))

    assert await client.acp_session("configure", context_id="ctx-1", cwd="/workspace") == {"ok": True}
    client.http.post.assert_awaited_once_with(
        "http://agent.test/api/plugins/_a0_acp/session",
        json={"action": "configure", "context_id": "ctx-1", "cwd": "/workspace"},
        follow_redirects=False,
    )


async def test_fetch_image_uses_authenticated_core_endpoint() -> None:
    client = A0Client("http://agent.test")
    client.http = Mock()
    client.http.get = AsyncMock(
        return_value=FakeResponse(json_data={"ok": True, "token": "csrf-1"})
    )
    client.http.stream = Mock(
        return_value=FakeResponse(
            content=b"png-bytes",
            headers={"content-type": "image/png"},
        )
    )

    content, mime = await client.fetch_image("/a0/usr/uploads/scan.png")

    assert (content, mime) == (b"png-bytes", "image/png")
    client.http.get.assert_awaited_once_with(
        "http://agent.test/api/csrf_token",
        headers={"Origin": "http://agent.test", "Referer": "http://agent.test/"},
    )
    client.http.stream.assert_called_once_with(
        "GET",
        "http://agent.test/api/image_get",
        params={"path": "/a0/usr/uploads/scan.png"},
        headers={
            "Origin": "http://agent.test",
            "Referer": "http://agent.test/",
            "X-CSRF-Token": "csrf-1",
        },
        follow_redirects=False,
    )


async def test_fetch_image_refreshes_csrf_after_forbidden_response() -> None:
    client = A0Client("http://agent.test")
    client.http = Mock()
    client.http.get = AsyncMock(
        side_effect=[
            FakeResponse(json_data={"ok": True, "token": "csrf-old"}),
            FakeResponse(json_data={"ok": True, "token": "csrf-new"}),
        ]
    )
    client.http.stream = Mock(
        side_effect=[
            FakeResponse(status_code=403, text="CSRF token missing or invalid"),
            FakeResponse(content=b"png-bytes", headers={"content-type": "image/png"}),
        ]
    )

    assert await client.fetch_image("/a0/usr/uploads/scan.png") == (
        b"png-bytes",
        "image/png",
    )
    assert client.http.get.await_count == 2
    assert client.http.stream.call_count == 2
    assert client.http.stream.call_args_list[0].kwargs["headers"]["X-CSRF-Token"] == "csrf-old"
    assert client.http.stream.call_args_list[1].kwargs["headers"]["X-CSRF-Token"] == "csrf-new"


async def test_fetch_image_refreshes_csrf_at_most_once() -> None:
    client = A0Client("http://agent.test")
    client.http = Mock()
    client.http.get = AsyncMock(
        side_effect=[
            FakeResponse(json_data={"ok": True, "token": "csrf-old"}),
            FakeResponse(json_data={"ok": True, "token": "csrf-new"}),
        ]
    )
    client.http.stream = Mock(
        side_effect=[
            FakeResponse(status_code=403, text="first forbidden response"),
            FakeResponse(status_code=403, text="second forbidden response"),
        ]
    )

    with pytest.raises(A0ProtocolError, match="HTTP 403") as exc_info:
        await client.fetch_image("/a0/usr/uploads/scan.png")

    assert "forbidden response" not in str(exc_info.value)
    assert client.http.get.await_count == 2
    assert client.http.stream.call_count == 2


async def test_fetch_image_normalizes_jpg_mime_type() -> None:
    client = A0Client("http://agent.test")
    client._csrf_token = "csrf-test"
    client.http = Mock()
    client.http.stream = Mock(
        return_value=FakeResponse(content=b"jpg-bytes", headers={"content-type": "image/jpg"})
    )

    assert await client.fetch_image("/a0/usr/uploads/scan.jpg") == (b"jpg-bytes", "image/jpeg")


@pytest.mark.parametrize(
    ("first_response", "expected_calls"),
    [
        (httpx.ConnectError("offline"), 2),
        (
            FakeResponse(
                headers={"content-type": "image/png"},
                iter_error=httpx.ReadError("read failed"),
            ),
            2,
        ),
        (FakeResponse(status_code=503), 2),
    ],
    ids=["stream-open", "stream-read", "status"],
)
async def test_fetch_image_retries_one_transient_failure(
    first_response: FakeResponse | httpx.ConnectError,
    expected_calls: int,
) -> None:
    client = A0Client("http://agent.test")
    client._csrf_token = "csrf-test"
    client.http = Mock()
    client.http.stream = Mock(
        side_effect=[
            first_response,
            FakeResponse(content=b"png", headers={"content-type": "image/png"}),
        ]
    )

    assert await client.fetch_image("/a0/usr/uploads/scan.png") == (b"png", "image/png")
    assert client.http.stream.call_count == expected_calls


@pytest.mark.parametrize(
    "responses",
    [
        [httpx.ConnectError("first"), httpx.ConnectError("second")],
        [
            FakeResponse(
                headers={"content-type": "image/png"},
                iter_error=httpx.ReadError("first"),
            ),
            FakeResponse(
                headers={"content-type": "image/png"},
                iter_error=httpx.ReadError("second"),
            ),
        ],
        *[
            [FakeResponse(status_code=status), FakeResponse(status_code=status)]
            for status in (502, 503, 504)
        ],
        [FakeResponse(status_code=302, headers={"location": "/login"})],
        [FakeResponse(status_code=404)],
        [FakeResponse(content=b"not image", headers={"content-type": "text/plain"})],
        [
            FakeResponse(
                headers={
                    "content-type": "image/png",
                    "content-length": str(25 * 1024 * 1024 + 1),
                },
            )
        ],
    ],
)
async def test_fetch_image_rejects_unsafe_or_invalid_responses(
    responses: list[FakeResponse | httpx.ConnectError],
) -> None:
    client = A0Client("http://agent.test")
    client._csrf_token = "csrf-test"
    client.http = Mock()
    client.http.stream = Mock(side_effect=responses)

    with pytest.raises(A0ProtocolError) as exc_info:
        await client.fetch_image("/a0/usr/uploads/scan.png")

    assert "not image" not in str(exc_info.value)
    assert "x" * 100 not in str(exc_info.value)


async def test_fetch_image_stops_streaming_above_limit() -> None:
    response = FakeResponse(
        headers={"content-type": "image/png"},
        chunks=[b"a" * (25 * 1024 * 1024), b"b", b"not-consumed"],
    )
    client = A0Client("http://agent.test")
    client._csrf_token = "csrf-test"
    client.http = Mock()
    client.http.stream = Mock(return_value=response)

    with pytest.raises(A0ProtocolError, match="size limit"):
        await client.fetch_image("/a0/usr/uploads/scan.png")

    assert response.read_chunks == 2


@pytest.mark.parametrize(
    "path",
    [
        "",
        "uploads/scan.png",
        "/other/scan.png",
        "//a0/usr/uploads/scan.png",
        "/a0/usr/../secret.png",
        "/a0/usr/%2e%2e/secret.png",
        "/a0/usr/%252e%252e/secret.png",
        "/a0/usr/%2525252e%2525252e/secret.png",
        "/a0/usr/uploads/scan.png%2525253fraw=1",
        "/a0/usr/uploads/scan.png%25252523frame",
        "/a0/usr/uploads%2525255csecret.png",
        "/a0/usr/uploads/scan.png?raw=1",
        "/a0/usr/uploads/scan.png#frame",
    ],
)
async def test_fetch_image_rejects_unsafe_agent_zero_paths(path: str) -> None:
    client = A0Client("http://agent.test")
    client.http = Mock()
    client.http.stream = Mock()

    with pytest.raises(A0ProtocolError):
        await client.fetch_image(path)

    client.http.stream.assert_not_called()
class FakeStreamResponse(FakeResponse):
    def __init__(self, *, chunks: list[bytes], **kwargs) -> None:
        super().__init__(**kwargs)
        self.chunks = chunks

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback

    async def aiter_bytes(self, _chunk_size: int):
        for chunk in self.chunks:
            yield chunk


class FakeSocketIOClient:
    def __init__(
        self,
        *,
        call_response: dict | None = None,
        connect_exception: Exception | None = None,
        connect_exceptions: list[Exception] | None = None,
        connected: bool = False,
    ) -> None:
        self.handlers: dict[tuple[str | None, str], object] = {}
        self.connect_calls: list[tuple[str, dict]] = []
        self.disconnect_calls = 0
        self.call_calls: list[tuple[str, dict, str | None]] = []
        self.emit_calls: list[tuple[str, dict, str | None]] = []
        self.call_response = call_response or {"results": [{"ok": True, "data": {}}]}
        self.connect_exceptions = list(connect_exceptions or [])
        if connect_exception is not None:
            self.connect_exceptions.append(connect_exception)
        self.connected = connected

    def on(self, event: str, namespace: str | None = None):
        def decorator(func):
            self.handlers[(namespace, event)] = func
            return func

        return decorator

    async def connect(self, url: str, **kwargs) -> None:
        self.connect_calls.append((url, kwargs))
        if self.connect_exceptions:
            raise self.connect_exceptions.pop(0)
        self.connected = True
        handler = self.handlers.get(("/ws", "connect"))
        if handler is not None:
            await handler()

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        was_connected = self.connected
        self.connected = False
        handler = self.handlers.get(("/ws", "disconnect"))
        if was_connected and handler is not None:
            await handler()

    async def call(
        self,
        event: str,
        data: dict,
        namespace: str | None = None,
    ) -> dict:
        self.call_calls.append((event, data, namespace))
        return self.call_response

    async def emit(
        self,
        event: str,
        data: dict,
        namespace: str | None = None,
    ) -> None:
        self.emit_calls.append((event, data, namespace))


def test_load_config_prefers_environment_over_dotenv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENT_ZERO_HOST", "http://env-host:1234")
    monkeypatch.setenv("AGENT_ZERO_LAST_CONTEXT_ID", "ctx-env")
    monkeypatch.setenv("AGENT_ZERO_LAST_CONTEXT_HOST", "http://env-host:1234")
    monkeypatch.setenv("AGENT_ZERO_DEFAULT_CONTEXT_ID", "ctx-default-env")
    monkeypatch.setenv("A0_REMOTE_EXEC", "1")

    env_dir = tmp_path / ".agent-zero"
    env_dir.mkdir()
    env_file = env_dir / ".env"
    env_file.write_text(
        "\n".join(
            (
                "AGENT_ZERO_HOST=http://dotenv-host:5080",
                "AGENT_ZERO_LAST_CONTEXT_ID=ctx-dotenv",
                "AGENT_ZERO_LAST_CONTEXT_HOST=http://dotenv-host:5080",
                "A0_DEFAULT_CHAT=ctx-default-dotenv",
                "AGENT_ZERO_REMOTE_EXEC_ENABLED=0",
            )
        )
        + "\n",
        encoding="utf-8",
    )

    import agent_zero_cli.config as config_mod

    monkeypatch.setattr(config_mod, "_ENV_FILE", env_file)
    config = load_config()

    assert config.instance_url == "http://env-host:1234"
    assert config.last_context_id == "ctx-env"
    assert config.last_context_host == "http://env-host:1234"
    assert config.default_context_id == "ctx-default-env"
    assert config.remote_exec_enabled is True


def test_load_config_reads_default_chat_and_remote_exec_from_dotenv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_dir = tmp_path / ".agent-zero"
    env_dir.mkdir()
    env_file = env_dir / ".env"
    env_file.write_text(
        "\n".join(
            (
                "A0_DEFAULT_CHAT=ctx-default",
                "A0_REMOTE_EXEC=true",
            )
        )
        + "\n",
        encoding="utf-8",
    )

    import agent_zero_cli.config as config_mod

    monkeypatch.setattr(config_mod, "_ENV_FILE", env_file)
    config = load_config()

    assert config.default_context_id == "ctx-default"
    assert config.remote_exec_enabled is True


def test_load_config_reads_remember_host_from_dotenv_and_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_dir = tmp_path / ".agent-zero"
    env_dir.mkdir()
    env_file = env_dir / ".env"
    env_file.write_text("AGENT_ZERO_REMEMBER_HOST=1\n", encoding="utf-8")

    import agent_zero_cli.config as config_mod

    monkeypatch.setattr(config_mod, "_ENV_DIR", env_dir)
    monkeypatch.setattr(config_mod, "_ENV_FILE", env_file)

    dotenv_config = load_config()
    assert dotenv_config.remember_host is True

    monkeypatch.setenv("AGENT_ZERO_REMEMBER_HOST", "0")
    env_config = load_config()
    assert env_config.remember_host is False


def test_save_env_updates_existing_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_dir = tmp_path / ".agent-zero"
    env_dir.mkdir()
    env_file = env_dir / ".env"
    env_file.write_text("AGENT_ZERO_HOST=http://old:5080\n", encoding="utf-8")

    import agent_zero_cli.config as config_mod

    monkeypatch.setattr(config_mod, "_ENV_DIR", env_dir)
    monkeypatch.setattr(config_mod, "_ENV_FILE", env_file)
    save_env("AGENT_ZERO_HOST", "http://new:9090")

    assert env_file.read_text(encoding="utf-8") == "AGENT_ZERO_HOST=http://new:9090\n"


def test_save_last_context_updates_host_and_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_dir = tmp_path / ".agent-zero"
    env_dir.mkdir()
    env_file = env_dir / ".env"
    env_file.write_text("AGENT_ZERO_HOST=http://old:5080\n", encoding="utf-8")

    import agent_zero_cli.config as config_mod

    monkeypatch.setattr(config_mod, "_ENV_DIR", env_dir)
    monkeypatch.setattr(config_mod, "_ENV_FILE", env_file)
    save_last_context("http://new:9090/", "ctx-9")

    contents = env_file.read_text(encoding="utf-8").splitlines()
    assert "AGENT_ZERO_HOST=http://old:5080" in contents
    assert "AGENT_ZERO_LAST_CONTEXT_HOST=http://new:9090" in contents
    assert "AGENT_ZERO_LAST_CONTEXT_ID=ctx-9" in contents


def test_load_config_reads_computer_use_defaults_and_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_dir = tmp_path / ".agent-zero"
    env_dir.mkdir()
    env_file = env_dir / ".env"
    env_file.write_text(
        "\n".join(
            (
                "AGENT_ZERO_COMPUTER_USE_ENABLED=1",
                "AGENT_ZERO_COMPUTER_USE_TRUST_MODE=unknown",
                "AGENT_ZERO_COMPUTER_USE_RESTORE_TOKEN=dotenv-token",
            )
        )
        + "\n",
        encoding="utf-8",
    )

    import agent_zero_cli.config as config_mod

    monkeypatch.setattr(config_mod, "_ENV_DIR", env_dir)
    monkeypatch.setattr(config_mod, "_ENV_FILE", env_file)

    dotenv_config = load_config()
    assert dotenv_config.computer_use_enabled is True
    assert dotenv_config.computer_use_trust_mode == "allow"
    assert dotenv_config.computer_use_restore_token == "dotenv-token"

    monkeypatch.setenv("AGENT_ZERO_COMPUTER_USE_ENABLED", "0")
    monkeypatch.setenv("AGENT_ZERO_COMPUTER_USE_TRUST_MODE", "allow")
    monkeypatch.setenv("AGENT_ZERO_COMPUTER_USE_RESTORE_TOKEN", "env-token")

    env_config = load_config()
    assert env_config.computer_use_enabled is False
    assert env_config.computer_use_trust_mode == "allow"
    assert env_config.computer_use_restore_token == "env-token"


def test_save_computer_use_settings_persist_to_dotenv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_dir = tmp_path / ".agent-zero"
    env_dir.mkdir()
    env_file = env_dir / ".env"

    import agent_zero_cli.config as config_mod

    monkeypatch.setattr(config_mod, "_ENV_DIR", env_dir)
    monkeypatch.setattr(config_mod, "_ENV_FILE", env_file)

    save_computer_use_enabled(True)
    save_computer_use_trust_mode("allow")
    save_computer_use_restore_token("restore-token")
    save_computer_use_restore_token("")

    contents = env_file.read_text(encoding="utf-8").splitlines()
    assert "AGENT_ZERO_COMPUTER_USE_ENABLED=1" in contents
    assert "AGENT_ZERO_COMPUTER_USE_TRUST_MODE=allow" in contents
    assert not any(line.startswith("AGENT_ZERO_COMPUTER_USE_RESTORE_TOKEN=") for line in contents)


def test_save_remember_host_persists_flag_to_dotenv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_dir = tmp_path / ".agent-zero"
    env_dir.mkdir()
    env_file = env_dir / ".env"

    import agent_zero_cli.config as config_mod

    monkeypatch.setattr(config_mod, "_ENV_DIR", env_dir)
    monkeypatch.setattr(config_mod, "_ENV_FILE", env_file)

    save_remember_host(True)
    assert env_file.read_text(encoding="utf-8") == "AGENT_ZERO_REMEMBER_HOST=1\n"

    save_remember_host(False)
    assert env_file.read_text(encoding="utf-8") == ""


def test_normalize_computer_use_trust_mode_defaults_unknown_labels_to_allow() -> None:
    assert normalize_computer_use_trust_mode("unknown") == "allow"
    assert normalize_computer_use_trust_mode("") == "allow"
    assert normalize_computer_use_trust_mode("persistent") == "persistent"
    assert normalize_computer_use_trust_mode("Allow") == "allow"


async def test_fetch_capabilities_raises_plugin_missing_on_404() -> None:
    client = A0Client("http://localhost:5080")
    client.http = Mock()
    client.http.post = AsyncMock(return_value=FakeResponse(status_code=404))

    with pytest.raises(A0ConnectorPluginMissingError):
        await client.fetch_capabilities()


async def test_fetch_capabilities_stores_server_receive_limit() -> None:
    client = A0Client("http://localhost:5080")
    client.http = Mock()
    client.http.post = AsyncMock(
        return_value=FakeResponse(
            json_data={
                "protocol": "a0-connector.v1",
                "capabilities": {"ws_max_payload_bytes": 16 * 1024 * 1024},
            }
        )
    )

    await client.fetch_capabilities()

    assert client.peer_ws_max_payload_bytes == 16 * 1024 * 1024


async def test_default_httpx_rejects_self_signed_https_connector_fixture() -> None:
    async with self_signed_connector_server() as base_url:
        async with httpx.AsyncClient(timeout=5.0) as client:
            with pytest.raises(httpx.ConnectError):
                await client.post(f"{base_url}/api/plugins/_a0_connector/v1/capabilities")


async def test_fetch_capabilities_accepts_self_signed_https_connector() -> None:
    async with self_signed_connector_server() as base_url:
        client = A0Client(base_url)
        try:
            capabilities = await client.fetch_capabilities()

            assert capabilities["protocol"] == "a0-connector.v1"
            assert await client.verify_session() is True
        finally:
            await client.disconnect()


async def test_connect_websocket_forwards_session_cookie_and_handler_auth() -> None:
    client = A0Client("http://127.0.0.1:50001")
    client.http = Mock()
    client.http.cookies = httpx.Cookies()
    client.http.cookies.set("session_test", "cookie-value", domain="127.0.0.1", path="/")
    client.http.get = AsyncMock(
        return_value=FakeResponse(
            status_code=200,
            text='0{"sid":"sid-1","upgrades":["websocket"],"pingInterval":25000,"pingTimeout":20000}',
        )
    )
    fake_sio = FakeSocketIOClient()
    client.sio = fake_sio

    await client.connect_websocket()

    client.http.get.assert_awaited_once_with(
        "http://127.0.0.1:50001/socket.io",
        params={"transport": "polling", "EIO": "4"},
        headers={
            "Cookie": "session_test=cookie-value",
            "Origin": "http://127.0.0.1:50001",
            "Referer": "http://127.0.0.1:50001/",
        },
    )
    assert fake_sio.connect_calls == [
        (
            "http://127.0.0.1:50001",
            {
                "namespaces": ["/ws"],
                "headers": {
                    "Cookie": "session_test=cookie-value",
                    "Origin": "http://127.0.0.1:50001",
                    "Referer": "http://127.0.0.1:50001/",
                },
                "auth": {"handlers": ["plugins/_a0_connector/ws_connector"]},
            },
        )
    ]


def test_persisted_session_round_trip_restores_cookie_header(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_dir = tmp_path / ".agent-zero"
    env_dir.mkdir()
    env_file = env_dir / ".env"
    session_file = env_dir / "session_cookies.json"

    import agent_zero_cli.config as config_mod

    monkeypatch.setattr(config_mod, "_ENV_DIR", env_dir)
    monkeypatch.setattr(config_mod, "_ENV_FILE", env_file)
    monkeypatch.setattr(config_mod, "_SESSION_FILE", session_file)

    source = A0Client("http://127.0.0.1:50001")
    source.http.cookies = httpx.Cookies()
    source.http.cookies.set("session_test", "cookie-value", domain="127.0.0.1", path="/")
    source.persist_session("http://127.0.0.1:50001")

    restored = A0Client("http://127.0.0.1:50001")
    assert restored.restore_session("http://127.0.0.1:50001") is True
    assert restored._cookie_header("http://127.0.0.1:50001/socket.io") == "session_test=cookie-value"

    restored.clear_persisted_session("http://127.0.0.1:50001")
    assert restored.restore_session("http://127.0.0.1:50001") is False
    assert not session_file.exists()


async def test_set_model_override_posts_complete_model_payload() -> None:
    client = A0Client("http://example.test")
    client.http = Mock()
    client.http.post = AsyncMock(return_value=FakeResponse(json_data={"ok": True}))

    main_model = {
        "provider": "openrouter",
        "name": "anthropic/claude-sonnet",
        "api_key": "sk-main",
        "api_base": "https://example.test/main/v1",
    }
    utility_model = {
        "provider": "openai",
        "name": "gpt-5.4-mini",
        "api_key": "sk-utility",
        "api_base": "https://example.test/utility/v1",
    }
    embedding_model = {
        "provider": "openai",
        "name": "text-embedding-3-large",
    }

    result = await client.set_model_override(
        "ctx-1",
        main_model=main_model,
        utility_model=utility_model,
        embedding_model=embedding_model,
    )

    assert result == {"ok": True}
    client.http.post.assert_awaited_once_with(
        "http://example.test/api/plugins/_a0_connector/v1/model_switcher",
        json={
            "action": "set_override",
            "context_id": "ctx-1",
            "main_model": main_model,
            "utility_model": utility_model,
            "embedding_model": embedding_model,
        },
    )


async def test_list_skills_posts_context_scoped_payload() -> None:
    client = A0Client("http://example.test")
    client.http = Mock()
    client.http.post = AsyncMock(
        return_value=FakeResponse(
            json_data={
                "ok": True,
                "data": [
                    {
                        "name": "a0-live-e2e-tester",
                        "path": "/a0/skills/a0-live-e2e-tester",
                    }
                ],
            }
        )
    )

    result = await client.list_skills(context_id="ctx-1", project_name="agent-zero")

    client.http.post.assert_awaited_once_with(
        "http://example.test/api/plugins/_a0_connector/v1/skills_list",
        json={"context_id": "ctx-1", "project_name": "agent-zero"},
    )
    assert result == [
        {
            "name": "a0-live-e2e-tester",
            "path": "/a0/skills/a0-live-e2e-tester",
        }
    ]


async def test_container_reference_entries_stay_in_the_active_chat_workspace() -> None:
    client = A0Client("http://agent.test")
    client._csrf_token = "csrf-test"
    client.http = Mock()
    client.http.post = AsyncMock(
        return_value=FakeResponse(json_data={"ok": True, "path": "/a0/usr/workdir/project"})
    )
    client.http.get = AsyncMock(
        return_value=FakeResponse(
            json_data={
                "data": {
                    "entries": [
                        {"name": "src", "path": "a0/usr/workdir/project/src", "is_dir": True},
                        {"name": "escape", "path": "tmp/escape", "is_dir": False},
                        {"name": "linked", "path": "a0/usr/workdir/project/linked", "is_symlink": True},
                        {"name": "nested", "path": "a0/usr/workdir/project/src/nested", "is_dir": False},
                    ]
                }
            }
        )
    )

    root = await client.get_chat_files_path("ctx-1")
    entries = await client.list_container_reference_entries(root)

    assert root == "/a0/usr/workdir/project"
    assert entries == [{"name": "src", "path": "/a0/usr/workdir/project/src", "is_dir": True}]
    client.http.post.assert_awaited_once_with(
        "http://agent.test/api/chat_files_path_get",
        json={"ctxid": "ctx-1"},
        headers={
            "Origin": "http://agent.test",
            "Referer": "http://agent.test/",
            "X-CSRF-Token": "csrf-test",
        },
    )
    client.http.get.assert_awaited_once_with(
        "http://agent.test/api/get_work_dir_files",
        params={"path": "/a0/usr/workdir/project"},
        headers={
            "Origin": "http://agent.test",
            "Referer": "http://agent.test/",
            "X-CSRF-Token": "csrf-test",
        },
    )

    with pytest.raises(ValueError, match="outside the active workspace"):
        await client.list_container_reference_entries(root, "../outside")


async def test_list_commands_posts_to_commands_plugin_api_with_csrf() -> None:
    client = A0Client("http://example.test")
    client.http = Mock()
    client.http.post = AsyncMock(
        return_value=FakeResponse(
            json_data={
                "ok": True,
                "commands": [
                    {
                        "name": "compress",
                        "description": "Compress this chat.",
                        "source_scope_label": "Plugin: compress_history",
                    }
                ],
            }
        )
    )
    client._csrf_headers = AsyncMock(return_value={"X-CSRF-Token": "csrf"})  # type: ignore[method-assign]

    result = await client.list_commands("ctx-1")

    client.http.post.assert_awaited_once_with(
        "http://example.test/api/plugins/_commands/commands",
        json={"action": "list_effective", "context_id": "ctx-1"},
        headers={"X-CSRF-Token": "csrf"},
    )
    assert result == [
        {
            "name": "compress",
            "description": "Compress this chat.",
            "source_scope_label": "Plugin: compress_history",
        }
    ]


async def test_activate_skill_posts_context_scoped_payload() -> None:
    client = A0Client("http://example.test")
    client.http = Mock()
    client.http.post = AsyncMock(
        return_value=FakeResponse(
            json_data={
                "ok": True,
                "skill": {
                    "name": "imagegen",
                    "path": "/a0/skills/imagegen",
                },
            }
        )
    )

    result = await client.activate_skill(
        "ctx-1",
        {"name": "imagegen", "path": "/a0/skills/imagegen", "extra": "ignored"},
    )

    client.http.post.assert_awaited_once_with(
        "http://example.test/api/plugins/_a0_connector/v1/skills_activate",
        json={
            "context_id": "ctx-1",
            "skill": {
                "name": "imagegen",
                "path": "/a0/skills/imagegen",
            },
        },
    )
    assert result == {
        "ok": True,
        "skill": {
            "name": "imagegen",
            "path": "/a0/skills/imagegen",
        },
    }


async def test_list_installed_plugins_posts_installed_only_endpoint() -> None:
    client = A0Client("http://example.test")
    client.http = Mock()
    client.http.post = AsyncMock(
        return_value=FakeResponse(
            json_data={
                "ok": True,
                "plugins": [
                    {
                        "name": "_browser",
                        "display_name": "Browser",
                        "enabled": True,
                    }
                ],
            }
        )
    )

    result = await client.list_installed_plugins()

    client.http.post.assert_awaited_once_with(
        "http://example.test/api/plugins/_a0_connector/v1/installed_plugins",
        json={"action": "list"},
    )
    assert result == [
        {
            "name": "_browser",
            "display_name": "Browser",
            "enabled": True,
        }
    ]


async def test_set_installed_plugin_enabled_posts_connector_toggle_payload() -> None:
    client = A0Client("http://example.test")
    client.http = Mock()
    client.http.post = AsyncMock(return_value=FakeResponse(json_data={"ok": True}))

    result = await client.set_installed_plugin_enabled("_browser", False)

    client.http.post.assert_awaited_once_with(
        "http://example.test/api/plugins/_a0_connector/v1/installed_plugins",
        json={
            "action": "set_enabled",
            "plugin_name": "_browser",
            "enabled": False,
        },
    )
    assert result == {"ok": True}


async def test_set_installed_plugin_enabled_returns_structured_error() -> None:
    client = A0Client("http://example.test")
    client.http = Mock()
    client.http.post = AsyncMock(
        return_value=FakeResponse(status_code=400, text="Plugin cannot be toggled")
    )

    result = await client.set_installed_plugin_enabled("_a0_connector", False)

    assert result == {
        "ok": False,
        "message": "Plugin cannot be toggled",
        "status_code": 400,
    }


async def test_connect_websocket_resets_existing_socket_without_disconnect_callback() -> None:
    client = A0Client("http://127.0.0.1:50001")
    client.http = Mock()
    client.http.cookies = httpx.Cookies()
    client.http.get = AsyncMock(
        return_value=FakeResponse(
            status_code=200,
            text='0{"sid":"sid-1","upgrades":["websocket"],"pingInterval":25000,"pingTimeout":20000}',
        )
    )
    fake_sio = FakeSocketIOClient(connected=True)
    client.sio = fake_sio
    disconnect_callbacks = 0

    def nonlocal_increment() -> None:
        nonlocal disconnect_callbacks
        disconnect_callbacks += 1

    client.on_disconnect = nonlocal_increment

    await client.connect_websocket()

    assert fake_sio.disconnect_calls == 1
    assert fake_sio.connected is True
    assert client.connected is True
    assert disconnect_callbacks == 0


async def test_connect_websocket_recovers_from_already_connected_race() -> None:
    client = A0Client("http://127.0.0.1:50001")
    client.http = Mock()
    client.http.cookies = httpx.Cookies()
    client.http.get = AsyncMock(
        return_value=FakeResponse(
            status_code=200,
            text='0{"sid":"sid-1","upgrades":["websocket"],"pingInterval":25000,"pingTimeout":20000}',
        )
    )
    fake_sio = FakeSocketIOClient(
        connect_exceptions=[socketio.exceptions.ConnectionError("Already connected")]
    )
    client.sio = fake_sio

    await client.connect_websocket()

    assert len(fake_sio.connect_calls) == 2
    assert fake_sio.connected is True
    assert client.connected is True


async def test_connect_websocket_accepts_self_signed_https_connector() -> None:
    async with self_signed_connector_server() as base_url:
        client = A0Client(base_url)
        try:
            await client.connect_websocket()

            assert client.connected is True
            assert client.sio.connected is True
        finally:
            await client.disconnect()


def test_socketio_client_disables_aiohttp_websocket_tls_verification() -> None:
    kwargs = _socketio_client_kwargs()

    assert kwargs["ssl_verify"] is False
    assert kwargs["websocket_extra_options"] == {
        "max_msg_size": A0_WS_MAX_PAYLOAD_BYTES + 1,
        "ssl": False,
    }

    client = A0Client("https://example.test")

    assert client.sio.eio.ssl_verify is False
    assert client.sio.eio.websocket_extra_options == {
        "max_msg_size": A0_WS_MAX_PAYLOAD_BYTES + 1,
        "ssl": False,
    }


@pytest.mark.parametrize(
    "limit",
    [4 * 1024 * 1024, 16 * 1024 * 1024, 50 * 1024 * 1024],
)
async def test_send_message_preflights_exact_serialized_payload_boundary(
    limit: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed_id = "00000000-0000-0000-0000-000000000000"
    monkeypatch.setattr("agent_zero_cli.client.uuid.uuid4", lambda: fixed_id)
    client = A0Client("http://127.0.0.1:50001")
    client.sio = FakeSocketIOClient()
    client.peer_ws_max_payload_bytes = limit

    base_payload = {
        "context_id": "ctx-1",
        "message": "",
        "client_message_id": fixed_id,
    }
    base_size = _socketio_event_size(
        "connector_send_message",
        base_payload,
        acknowledged=True,
    )
    padding = limit - base_size
    assert padding > 1

    await client.send_message("x" * (padding - 1), "ctx-1")
    await client.send_message("x" * padding, "ctx-1")
    with pytest.raises(A0ProtocolError, match="PAYLOAD_TOO_LARGE"):
        await client.send_message("x" * (padding + 1), "ctx-1")

    assert len(client.sio.call_calls) == 2


@pytest.mark.parametrize("size", [0, 1])
async def test_zero_and_one_byte_messages_keep_socket_usable(size: int) -> None:
    client = A0Client("http://127.0.0.1:50001")
    client.sio = FakeSocketIOClient()

    await client.send_message("x" * size, "ctx-1")

    assert len(client.sio.call_calls) == 1
    assert client.sio.disconnect_calls == 0


async def test_connect_websocket_reports_blank_namespace_rejection_after_probe() -> None:
    client = A0Client("http://127.0.0.1:50001")
    client.http = Mock()
    client.http.get = AsyncMock(
        return_value=FakeResponse(
            status_code=200,
            text='0{"sid":"sid-1","upgrades":["websocket"],"pingInterval":25000,"pingTimeout":20000}',
        )
    )
    client.sio = FakeSocketIOClient(
        connect_exception=socketio.exceptions.ConnectionError(""),
    )

    with pytest.raises(
        A0WebSocketConnectionError,
        match=r"Socket\.IO transport probe succeeded, but the /ws namespace connection was rejected\.",
    ):
        await client.connect_websocket()


async def test_connect_websocket_reports_tls_certificate_rejection_after_probe() -> None:
    client = A0Client("https://example.test")
    client.http = Mock()
    client.http.get = AsyncMock(
        return_value=FakeResponse(
            status_code=200,
            text='0{"sid":"sid-1","upgrades":["websocket"],"pingInterval":25000,"pingTimeout":20000}',
        )
    )
    client.sio = FakeSocketIOClient(
        connect_exception=socketio.exceptions.ConnectionError(
            "Cannot connect to host example.test:443 ssl:True "
            "[SSLCertVerificationError: unable to get local issuer certificate]"
        ),
    )

    with pytest.raises(A0WebSocketConnectionError) as exc_info:
        await client.connect_websocket()

    message = str(exc_info.value)
    assert "TLS certificate verification" in message
    assert "Origin/Referer" not in message


async def test_send_message_uses_prefixed_ws_event() -> None:
    client = A0Client("http://127.0.0.1:50001")
    client.sio = FakeSocketIOClient(
        call_response={
            "results": [{"ok": True, "data": {"context_id": "ctx-1", "status": "accepted"}}]
        }
    )

    result = await client.send_message("hello", "ctx-1")

    assert result == {"context_id": "ctx-1", "status": "accepted"}
    event, payload, namespace = client.sio.call_calls[0]
    assert event == "connector_send_message"
    assert namespace == "/ws"
    assert payload["context_id"] == "ctx-1"
    assert payload["message"] == "hello"
    assert payload["client_message_id"]


async def test_send_message_includes_attachment_refs() -> None:
    client = A0Client("http://127.0.0.1:50001")
    client.sio = FakeSocketIOClient(
        call_response={
            "results": [{"ok": True, "data": {"context_id": "ctx-1", "status": "accepted"}}]
        }
    )

    await client.send_message("see attached", "ctx-1", attachments=["/a0/usr/uploads/clipboard.png"])

    _event, payload, _namespace = client.sio.call_calls[0]
    assert payload["attachments"] == ["/a0/usr/uploads/clipboard.png"]


async def test_add_message_to_queue_uses_queue_ws_event() -> None:
    client = A0Client("http://127.0.0.1:50001")
    client.sio = FakeSocketIOClient(
        call_response={
            "results": [{"ok": True, "data": {"context_id": "ctx-1", "status": "queued"}}]
        }
    )

    result = await client.add_message_to_queue(
        "later",
        "ctx-1",
        attachments=["/a0/usr/uploads/clipboard.png"],
    )

    assert result == {"context_id": "ctx-1", "status": "queued"}
    event, payload, namespace = client.sio.call_calls[0]
    assert event == "connector_message_queue_add"
    assert namespace == "/ws"
    assert payload["context_id"] == "ctx-1"
    assert payload["message"] == "later"
    assert payload["attachments"] == ["/a0/usr/uploads/clipboard.png"]
    assert payload["client_message_id"]


async def test_send_message_queue_uses_queue_ws_event() -> None:
    client = A0Client("http://127.0.0.1:50001")
    client.sio = FakeSocketIOClient(
        call_response={
            "results": [{"ok": True, "data": {"context_id": "ctx-1", "sent_count": 2}}]
        }
    )

    result = await client.send_message_queue("ctx-1", send_all=True)

    assert result == {"context_id": "ctx-1", "sent_count": 2}
    event, payload, namespace = client.sio.call_calls[0]
    assert event == "connector_message_queue_send"
    assert namespace == "/ws"
    assert payload == {"context_id": "ctx-1", "send_all": True}


async def test_remove_message_from_queue_uses_queue_ws_event() -> None:
    client = A0Client("http://127.0.0.1:50001")
    client.sio = FakeSocketIOClient(
        call_response={
            "results": [{"ok": True, "data": {"context_id": "ctx-1", "remaining": 0}}]
        }
    )

    result = await client.remove_message_from_queue("ctx-1", item_id="item-1")

    assert result == {"context_id": "ctx-1", "remaining": 0}
    event, payload, namespace = client.sio.call_calls[0]
    assert event == "connector_message_queue_remove"
    assert namespace == "/ws"
    assert payload == {"context_id": "ctx-1", "item_id": "item-1"}


async def test_upload_attachments_posts_files_to_core_upload_endpoint() -> None:
    client = A0Client("http://localhost:5080")
    client.http = Mock()
    client.http.get = AsyncMock(
        return_value=FakeResponse(json_data={"ok": True, "token": "csrf-1"})
    )
    client.http.post = AsyncMock(
        return_value=FakeResponse(json_data={"filenames": ["stored-image.png"]})
    )

    refs = await client.upload_attachments(
        [
            AttachmentUpload(
                filename="local-image.png",
                content=b"png-bytes",
                mime_type="image/png",
            )
        ]
    )

    client.http.get.assert_awaited_once_with(
        "http://localhost:5080/api/csrf_token",
        headers={
            "Origin": "http://localhost:5080",
            "Referer": "http://localhost:5080/",
        },
    )
    client.http.post.assert_awaited_once()
    args, kwargs = client.http.post.await_args
    assert args == ("http://localhost:5080/api/upload",)
    assert kwargs["files"] == [
        ("file", ("local-image.png", b"png-bytes", "image/png"))
    ]
    assert kwargs["headers"] == {
        "Origin": "http://localhost:5080",
        "Referer": "http://localhost:5080/",
        "X-CSRF-Token": "csrf-1",
    }
    assert kwargs["timeout"].connect == 10.0
    assert kwargs["timeout"].read > 30.0
    assert kwargs["timeout"].write > 30.0
    assert refs[0].path == "/a0/usr/uploads/stored-image.png"
    assert refs[0].name == "stored-image.png"
    assert refs[0].mime_type == "image/png"


async def test_upload_attachments_refreshes_csrf_after_forbidden_response() -> None:
    client = A0Client("http://localhost:5080")
    client.http = Mock()
    client.http.get = AsyncMock(
        side_effect=[
            FakeResponse(json_data={"ok": True, "token": "csrf-old"}),
            FakeResponse(json_data={"ok": True, "token": "csrf-new"}),
        ]
    )
    client.http.post = AsyncMock(
        side_effect=[
            FakeResponse(status_code=403, text="CSRF token missing or invalid"),
            FakeResponse(json_data={"filenames": ["stored-image.png"]}),
        ]
    )

    refs = await client.upload_attachments(
        [
            AttachmentUpload(
                filename="local-image.png",
                content=b"png-bytes",
                mime_type="image/png",
            )
        ]
    )

    assert refs[0].path == "/a0/usr/uploads/stored-image.png"
    assert client.http.get.await_count == 2
    assert client.http.post.await_args_list[0].kwargs["headers"]["X-CSRF-Token"] == "csrf-old"
    assert client.http.post.await_args_list[1].kwargs["headers"]["X-CSRF-Token"] == "csrf-new"


async def test_goal_action_posts_to_goal_plugin_api_with_csrf() -> None:
    client = A0Client("http://localhost:5080")
    client.http = Mock()
    client.http.get = AsyncMock(
        return_value=FakeResponse(json_data={"ok": True, "token": "csrf-1"})
    )
    client.http.post = AsyncMock(
        return_value=FakeResponse(
            json_data={
                "ok": True,
                "goal": {"objective": "Ship CLI goal support", "status": "active"},
            }
        )
    )

    result = await client.goal_action("update", "ctx-1", objective="Ship CLI goal support")

    assert result["goal"]["objective"] == "Ship CLI goal support"
    client.http.post.assert_awaited_once_with(
        "http://localhost:5080/api/plugins/_goal/goal",
        json={
            "action": "update",
            "context_id": "ctx-1",
            "objective": "Ship CLI goal support",
        },
        headers={
            "Origin": "http://localhost:5080",
            "Referer": "http://localhost:5080/",
            "X-CSRF-Token": "csrf-1",
        },
    )


async def test_upload_attachments_rejects_invalid_response() -> None:
    client = A0Client("http://localhost:5080")
    client.http = Mock()
    client.http.get = AsyncMock(
        return_value=FakeResponse(json_data={"ok": True, "token": "csrf-1"})
    )
    client.http.post = AsyncMock(return_value=FakeResponse(json_data={"filenames": []}))

    with pytest.raises(A0ProtocolError, match="invalid attachment response"):
        await client.upload_attachments(
            [
                AttachmentUpload(
                    filename="local-image.png",
                    content=b"png-bytes",
                    mime_type="image/png",
                )
            ]
        )


async def test_upload_attachments_streams_disk_source_and_verifies_hash(
    tmp_path: Path,
) -> None:
    payload = b"disk-backed-upload" * 100_000
    source = tmp_path / "payload.png"
    source.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    client = A0Client("http://localhost:5080")
    client.http = Mock()
    client.http.get = AsyncMock(
        return_value=FakeResponse(json_data={"ok": True, "token": "csrf-1"})
    )

    async def post_upload(_url: str, **kwargs):
        uploaded = kwargs["files"][0][1][1]
        assert not isinstance(uploaded, bytes)
        assert uploaded.read() == payload
        return FakeResponse(
            json_data={
                "filenames": ["payload.png"],
                "files": [
                    {
                        "filename": "payload.png",
                        "size": len(payload),
                        "sha256": digest,
                    }
                ],
            }
        )

    client.http.post = AsyncMock(side_effect=post_upload)

    refs = await client.upload_attachments(
        [AttachmentUpload("payload.png", source, "image/png")]
    )

    assert refs[0].path == "/a0/usr/uploads/payload.png"


async def test_upload_attachments_rejects_integrity_mismatch() -> None:
    client = A0Client("http://localhost:5080")
    client.http = Mock()
    client.http.get = AsyncMock(
        return_value=FakeResponse(json_data={"ok": True, "token": "csrf-1"})
    )
    client.http.post = AsyncMock(
        return_value=FakeResponse(
            json_data={
                "filenames": ["payload.png"],
                "files": [
                    {
                        "filename": "payload.png",
                        "size": 3,
                        "sha256": "0" * 64,
                    }
                ],
            }
        )
    )

    with pytest.raises(A0ProtocolError, match="SHA-256 mismatch"):
        await client.upload_attachments(
            [AttachmentUpload("payload.png", b"abc", "image/png")]
        )


def test_bulk_timeout_scales_for_64_mib_transfer() -> None:
    timeout = _bulk_transfer_timeout(64 * 1024 * 1024)

    assert timeout.connect == 10.0
    assert timeout.pool == 10.0
    assert timeout.read == 94.0
    assert timeout.write == 94.0


async def test_download_file_streams_hashes_and_atomically_replaces(
    tmp_path: Path,
) -> None:
    payload = b"container-to-host" * 100_000
    digest = hashlib.sha256(payload).hexdigest()
    target = tmp_path / "download.bin"
    target.write_bytes(b"old")
    client = A0Client("http://localhost:5080")
    client.http = Mock()
    client.http.get = AsyncMock(
        return_value=FakeResponse(json_data={"ok": True, "token": "csrf-1"})
    )
    response = FakeStreamResponse(
        chunks=[payload[:1_000_000], payload[1_000_000:]],
        headers={
            "X-Content-SHA256": digest,
            "Content-Length": str(len(payload)),
        },
    )
    client.http.stream = Mock(return_value=response)

    result = await client.download_file("/a0/work/download.bin", target)

    assert result == {
        "path": str(target),
        "size": len(payload),
        "sha256": digest,
    }
    assert target.read_bytes() == payload
    assert list(tmp_path.glob(".partial-*")) == []
    args, kwargs = client.http.stream.call_args
    assert args == ("GET", "http://localhost:5080/api/download_work_dir_file")
    assert kwargs["params"] == {"path": "/a0/work/download.bin"}
    assert kwargs["timeout"].read == 30.0


async def test_download_hash_mismatch_preserves_existing_destination(
    tmp_path: Path,
) -> None:
    target = tmp_path / "download.bin"
    target.write_bytes(b"old")
    client = A0Client("http://localhost:5080")
    client.http = Mock()
    client.http.get = AsyncMock(
        return_value=FakeResponse(json_data={"ok": True, "token": "csrf-1"})
    )
    client.http.stream = Mock(
        return_value=FakeStreamResponse(
            chunks=[b"corrupt"],
            headers={
                "X-Content-SHA256": "0" * 64,
                "Content-Length": "7",
            },
        )
    )

    with pytest.raises(A0ProtocolError, match="SHA-256 mismatch"):
        await client.download_file("/a0/work/download.bin", target)

    assert target.read_bytes() == b"old"
    assert list(tmp_path.glob(".partial-*")) == []


@pytest.mark.parametrize(
    "size",
    [0, 1, 1024 * 1024 - 1, 1024 * 1024, 1024 * 1024 + 1],
)
async def test_http_upload_download_chunk_boundary_matrix(
    tmp_path: Path,
    size: int,
) -> None:
    payload = (b"\x00\xffA5" * ((size + 3) // 4))[:size]
    digest = hashlib.sha256(payload).hexdigest()
    source = tmp_path / f"source-{size}.bin"
    target = tmp_path / f"target-{size}.bin"
    source.write_bytes(payload)
    client = A0Client("http://localhost:5080")
    client.http = Mock()
    client.http.get = AsyncMock(
        return_value=FakeResponse(json_data={"ok": True, "token": "csrf-1"})
    )

    async def post_upload(_url: str, **kwargs):
        uploaded = kwargs["files"][0][1][1]
        assert uploaded.read() == payload
        return FakeResponse(
            json_data={
                "filenames": [source.name],
                "files": [
                    {"filename": source.name, "size": size, "sha256": digest}
                ],
            }
        )

    client.http.post = AsyncMock(side_effect=post_upload)
    refs = await client.upload_attachments(
        [AttachmentUpload(source.name, source, "application/octet-stream")]
    )
    client.http.stream = Mock(
        return_value=FakeStreamResponse(
            chunks=[payload[: 1024 * 1024], payload[1024 * 1024 :]],
            headers={"X-Content-SHA256": digest, "Content-Length": str(size)},
        )
    )

    receipt = await client.download_file(f"/a0/work/{source.name}", target)

    assert refs[0].path == f"/a0/usr/uploads/{source.name}"
    assert receipt["size"] == size
    assert receipt["sha256"] == digest
    assert target.read_bytes() == payload
    assert list(tmp_path.glob(".partial-*")) == []


async def test_send_hello_returns_exec_config_payload() -> None:
    client = A0Client("http://127.0.0.1:50001")
    client.sio = FakeSocketIOClient(
        call_response={
            "results": [
                {
                    "ok": True,
                    "data": {
                        "protocol": "a0-connector.v1",
                        "capabilities": {"ws_max_payload_bytes": 16 * 1024 * 1024},
                        "features": ["code_execution_remote"],
                        "exec_config": {
                            "version": 1,
                            "code_exec_timeouts": {"first_output_timeout": 30},
                        },
                    },
                }
            ]
        }
    )

    result = await client.send_hello()

    assert result["protocol"] == "a0-connector.v1"
    assert result["exec_config"]["version"] == 1
    assert client.peer_ws_max_payload_bytes == 16 * 1024 * 1024
    event, payload, namespace = client.sio.call_calls[0]
    assert event == "connector_hello"
    assert namespace == "/ws"
    assert payload["protocol"] == "a0-connector.v1"
    assert payload["capabilities"] == {
        "ws_max_payload_bytes": A0_WS_MAX_PAYLOAD_BYTES,
        "transfer_protocol": TRANSFER_PROTOCOL_VERSION,
    }


async def test_send_hello_includes_computer_use_metadata() -> None:
    client = A0Client("http://127.0.0.1:50001")
    client.sio = FakeSocketIOClient(
        call_response={"results": [{"ok": True, "data": {"protocol": "a0-connector.v1"}}]}
    )

    metadata = {
        "supported": True,
        "enabled": True,
        "trust_mode": "allow",
        "artifact_root": "/a0/tmp/_a0_connector/computer_use",
    }
    await client.send_hello(computer_use=metadata)

    event, payload, namespace = client.sio.call_calls[0]
    assert event == "connector_hello"
    assert namespace == "/ws"
    assert payload["computer_use"] == metadata


async def test_send_hello_includes_remote_file_and_exec_metadata() -> None:
    client = A0Client("http://127.0.0.1:50001")
    client.sio = FakeSocketIOClient(
        call_response={"results": [{"ok": True, "data": {"protocol": "a0-connector.v1"}}]}
    )

    remote_files = {
        "enabled": True,
        "write_enabled": False,
        "mode": "read_only",
    }
    remote_exec = {
        "enabled": True,
    }
    await client.send_hello(remote_files=remote_files, remote_exec=remote_exec)

    event, payload, namespace = client.sio.call_calls[0]
    assert event == "connector_hello"
    assert namespace == "/ws"
    assert payload["remote_files"] == remote_files
    assert payload["remote_exec"] == remote_exec


async def test_send_hello_includes_host_browser_metadata() -> None:
    client = A0Client("http://127.0.0.1:50001")
    client.sio = FakeSocketIOClient(
        call_response={"results": [{"ok": True, "data": {"protocol": "a0-connector.v1"}}]}
    )

    metadata = {
        "supported": True,
        "enabled": True,
        "status": "ready",
        "browser_family": "chrome",
        "profile_label": "Default",
        "features": ["open", "content"],
    }
    await client.send_hello(host_browser=metadata)

    event, payload, namespace = client.sio.call_calls[0]
    assert event == "connector_hello"
    assert namespace == "/ws"
    assert payload["host_browser"] == metadata


async def test_send_hello_includes_context_id_for_metadata_refresh() -> None:
    client = A0Client("http://127.0.0.1:50001")
    client.sio = FakeSocketIOClient(
        call_response={"results": [{"ok": True, "data": {"protocol": "a0-connector.v1"}}]}
    )

    await client.send_hello(context_id=" ctx-1 ", remote_exec={"enabled": True})

    event, payload, namespace = client.sio.call_calls[0]
    assert event == "connector_hello"
    assert namespace == "/ws"
    assert payload["context_id"] == "ctx-1"
    assert payload["remote_exec"] == {"enabled": True}


async def test_pause_agent_normalizes_http_failure() -> None:
    client = A0Client("http://localhost:5080")
    client.http = Mock()
    client.http.post = AsyncMock(
        return_value=FakeResponse(status_code=409, text="Context is not currently running")
    )

    result = await client.pause_agent("ctx-1")

    assert result == {
        "ok": False,
        "message": "Context is not currently running",
        "status_code": 409,
    }


async def test_set_agent_profile_posts_context_scoped_payload() -> None:
    client = A0Client("http://localhost:5080")
    client.http = Mock()
    client.http.post = AsyncMock(
        return_value=FakeResponse(
            json_data={
                "ok": True,
                "agent_profile": "developer",
                "agent_profile_label": "Developer",
            }
        )
    )

    result = await client.set_agent_profile("ctx-1", "developer")

    client.http.post.assert_awaited_once_with(
        "http://localhost:5080/api/plugins/_a0_connector/v1/agent_profile_set",
        json={"context_id": "ctx-1", "agent_profile": "developer"},
    )
    assert result == {
        "ok": True,
        "agent_profile": "developer",
        "agent_profile_label": "Developer",
    }


async def test_agent_editor_and_scoped_chat_creation_use_connector_endpoints() -> None:
    client = A0Client("http://localhost:5080")
    client.http = Mock()
    client.http.post = AsyncMock(
        side_effect=[
            FakeResponse(json_data={"ok": True, "profile_id": "source-scout"}),
            FakeResponse(json_data={"context_id": "ctx-2"}),
        ]
    )

    editor = await client.agent_editor(
        "quick_create",
        context_id="ctx-1",
        title="Source Scout",
        instructions="Verify every claim.",
    )
    context_id = await client.create_chat(
        current_context_id="ctx-1",
        agent_profile="source-scout",
        project_name="Demo",
    )

    assert editor == {"ok": True, "profile_id": "source-scout"}
    assert context_id == "ctx-2"
    assert client.http.post.await_args_list == [
        call(
            "http://localhost:5080/api/plugins/_a0_connector/v1/agent_editor",
            json={
                "action": "quick_create",
                "context_id": "ctx-1",
                "title": "Source Scout",
                "instructions": "Verify every claim.",
            },
        ),
        call(
            "http://localhost:5080/api/plugins/_a0_connector/v1/chat_create",
            json={
                "current_context": "ctx-1",
                "agent_profile": "source-scout",
                "project_name": "Demo",
            },
        ),
    ]


async def test_set_browser_runtime_posts_host_browser_selection() -> None:
    client = A0Client("http://localhost:5080")
    client.http = Mock()
    client.http.post = AsyncMock(return_value=FakeResponse(json_data={"ok": True}))

    result = await client.set_browser_runtime(
        "ctx-1",
        "host_required",
        host_browser_selection="chrome:default",
        profile_mode="existing",
    )

    client.http.post.assert_awaited_once_with(
        "http://localhost:5080/api/plugins/_a0_connector/v1/browser_runtime",
        json={
            "action": "set",
            "context_id": "ctx-1",
            "runtime_backend": "host_required",
            "host_browser_selection": "chrome:default",
            "profile_mode": "existing",
        },
    )
    assert result == {"ok": True}


async def test_file_op_requests_are_returned_via_result_event() -> None:
    client = A0Client("http://127.0.0.1:50001")
    client.http = Mock()
    client.http.get = AsyncMock(
        return_value=FakeResponse(
            status_code=200,
            text='0{"sid":"sid-1","upgrades":["websocket"],"pingInterval":25000,"pingTimeout":20000}',
        )
    )
    client.sio = FakeSocketIOClient()
    client.on_file_op = AsyncMock(
        return_value={"op_id": "op-1", "ok": True, "result": {"path": "/tmp/example.txt"}}
    )

    await client.connect_websocket()

    handler = client.sio.handlers[("/ws", "connector_file_op")]
    await handler({"data": {"op_id": "op-1", "op": "read", "path": "/tmp/example.txt"}})

    client.on_file_op.assert_awaited_once()
    assert client.sio.emit_calls == [
        (
            "connector_file_op_result",
            {"op_id": "op-1", "ok": True, "result": {"path": "/tmp/example.txt"}},
            "/ws",
        )
    ]


async def test_large_file_op_results_use_transfer_start_chunk_end() -> None:
    client = A0Client("http://127.0.0.1:50001")
    client.http = Mock()
    client.http.get = AsyncMock(
        return_value=FakeResponse(
            status_code=200,
            text='0{"sid":"sid-1","upgrades":["websocket"],"pingInterval":25000,"pingTimeout":20000}',
        )
    )
    client.sio = FakeSocketIOClient()
    expected_result = {
        "op_id": "op-large",
        "ok": True,
        "result": {
            "content": "x" * (32 * 1024 * 1024),
            "total_lines": 1,
            "line_from": 1,
            "line_to": 1,
        },
    }
    client.peer_ws_max_payload_bytes = A0_WS_MAX_PAYLOAD_BYTES
    client.peer_transfer_protocol = TRANSFER_PROTOCOL_VERSION
    client.on_file_op = AsyncMock(return_value=expected_result)

    await client.connect_websocket()

    handler = client.sio.handlers[("/ws", "connector_file_op")]
    await handler({"data": {"op_id": "op-large", "op": "read", "path": "/tmp/large.txt"}})

    assert len(client.sio.emit_calls) > 3
    assert client.sio.emit_calls[0][0] == "connector_transfer_start"
    assert client.sio.emit_calls[-1][0] == "connector_transfer_end"
    assert {call[2] for call in client.sio.emit_calls} == {"/ws"}
    start = client.sio.emit_calls[0][1]
    frames = [
        payload
        for event, payload, _namespace in client.sio.emit_calls
        if event == "connector_transfer_chunk"
    ]
    assert start["op_id"] == "op-large"
    assert start["kind"] == "connector_file_op_result"
    assert [frame["index"] for frame in frames] == list(range(len(frames)))
    assembled = b"".join(
        base64.b64decode(str(frame["data"]).encode("ascii")) for frame in frames
    )
    assert len(assembled) == start["total_bytes"]
    assert hashlib.sha256(assembled).hexdigest() == start["sha256"]
    assert json.loads(assembled.decode("utf-8")) == expected_result


async def test_core_to_cli_transfer_reassembles_32_mib_request_from_disk_spool() -> None:
    client = A0Client("http://127.0.0.1:50001")
    client.http = Mock()
    client.http.get = AsyncMock(
        return_value=FakeResponse(
            status_code=200,
            text='0{"sid":"sid-1","upgrades":["websocket"],"pingInterval":25000,"pingTimeout":20000}',
        )
    )
    client.sio = FakeSocketIOClient()
    client.on_browser_op = AsyncMock(
        return_value={"op_id": "browser-32", "ok": True, "result": {"status": "received"}}
    )
    await client.connect_websocket()

    request = {
        "op_id": "browser-32",
        "action": "synthetic_transfer_test",
        "padding": "x" * (32 * 1024 * 1024),
    }
    raw = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    transfer_id = "core-to-cli-32"
    start_handler = client.sio.handlers[("/ws", "connector_transfer_start")]
    chunk_handler = client.sio.handlers[("/ws", "connector_transfer_chunk")]
    end_handler = client.sio.handlers[("/ws", "connector_transfer_end")]

    await start_handler(
        {
            "data": {
                "transfer_id": transfer_id,
                "op_id": "browser-32",
                "kind": "connector_browser_op",
                "total_bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        }
    )
    spool_path = client._incoming_transfers[transfer_id].temp_path
    assert spool_path is not None and spool_path.exists()
    for index, offset in enumerate(range(0, len(raw), 64 * 1024)):
        await chunk_handler(
            {
                "data": {
                    "transfer_id": transfer_id,
                    "index": index,
                    "data": base64.b64encode(raw[offset : offset + 64 * 1024]).decode("ascii"),
                }
            }
        )
    await end_handler({"data": {"transfer_id": transfer_id}})

    client.on_browser_op.assert_awaited_once_with(request)
    assert transfer_id not in client._incoming_transfers
    assert not spool_path.exists()
    assert client.sio.disconnect_calls == 0
    assert client.sio.emit_calls[-1][0] == "connector_browser_op_result"
    await client.disconnect(close_http=False)


@pytest.mark.parametrize("fault", ["corrupt_hash", "short_chunk", "oversized"])
async def test_incoming_transfer_faults_abort_without_dispatch(
    fault: str,
) -> None:
    client = A0Client("http://127.0.0.1:50001")
    client.sio = FakeSocketIOClient(connected=True)
    client.on_browser_op = AsyncMock()
    client._register_event_handlers()
    request = {"op_id": f"browser-{fault}", "padding": "payload"}
    raw = json.dumps(request, separators=(",", ":")).encode("utf-8")
    total_bytes = len(raw) + (1 if fault == "short_chunk" else 0)
    if fault == "oversized":
        total_bytes = A0_WS_MAX_PAYLOAD_BYTES + 1
    digest = "0" * 64 if fault == "corrupt_hash" else hashlib.sha256(raw).hexdigest()
    transfer_id = f"transfer-{fault}"
    start = client.sio.handlers[("/ws", "connector_transfer_start")]
    chunk = client.sio.handlers[("/ws", "connector_transfer_chunk")]
    end = client.sio.handlers[("/ws", "connector_transfer_end")]

    await start(
        {
            "data": {
                "transfer_id": transfer_id,
                "op_id": request["op_id"],
                "kind": "connector_browser_op",
                "total_bytes": total_bytes,
                "sha256": digest,
            }
        }
    )
    if fault != "oversized":
        await chunk(
            {
                "data": {
                    "transfer_id": transfer_id,
                    "index": 0,
                    "data": base64.b64encode(raw).decode("ascii"),
                }
            }
        )
        if fault == "corrupt_hash":
            await end({"data": {"transfer_id": transfer_id}})

    client.on_browser_op.assert_not_awaited()
    assert transfer_id not in client._incoming_transfers
    aborts = [
        payload
        for event, payload, _namespace in client.sio.emit_calls
        if event == "connector_transfer_abort"
    ]
    assert len(aborts) == 1
    assert aborts[0]["reason"]
    assert client.sio.disconnect_calls == 0


async def test_incoming_transfer_concurrency_and_idle_timeout_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agent_zero_cli.client as client_module

    monkeypatch.setattr(client_module, "_TRANSFER_IDLE_TIMEOUT_SECONDS", 0.05)
    client = A0Client("http://127.0.0.1:50001")
    client.sio = FakeSocketIOClient(connected=True)
    client._register_event_handlers()
    start = client.sio.handlers[("/ws", "connector_transfer_start")]
    empty_digest = hashlib.sha256(b"").hexdigest()
    for index in range(4):
        await start(
            {
                "data": {
                    "transfer_id": f"idle-{index}",
                    "op_id": f"op-{index}",
                    "kind": "connector_browser_op",
                    "total_bytes": 0,
                    "sha256": empty_digest,
                }
            }
        )
    await start(
        {
            "data": {
                "transfer_id": "too-many",
                "op_id": "op-too-many",
                "kind": "connector_browser_op",
                "total_bytes": 0,
                "sha256": empty_digest,
            }
        }
    )
    assert len(client._incoming_transfers) == 4
    await asyncio.sleep(0.12)
    assert client._incoming_transfers == {}
    abort_reasons = [
        payload["reason"]
        for event, payload, _namespace in client.sio.emit_calls
        if event == "connector_transfer_abort"
    ]
    assert any("too many concurrent" in reason for reason in abort_reasons)
    assert sum("idle timeout" in reason for reason in abort_reasons) == 4
    result = {"op_id": "after-timeout", "ok": True, "result": {"status": "ready"}}
    assert await client._emit_op_result("connector_browser_op_result", result) == result
    assert client.sio.emit_calls[-1] == ("connector_browser_op_result", result, "/ws")
    assert client.sio.disconnect_calls == 0


async def test_pause_aborts_context_transfers_and_removes_spool_file() -> None:
    class BlockingTransferSocket(FakeSocketIOClient):
        def __init__(self) -> None:
            super().__init__(connected=True)
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def emit(
            self,
            event: str,
            data: dict,
            namespace: str | None = None,
        ) -> None:
            await super().emit(event, data, namespace)
            if event == "connector_transfer_start":
                self.started.set()
                await self.release.wait()

    client = A0Client("http://127.0.0.1:50001")
    client.http = Mock()
    client.http.post = AsyncMock(return_value=FakeResponse(json_data={"ok": True}))
    client.sio = BlockingTransferSocket()
    client.peer_ws_max_payload_bytes = A0_WS_MAX_PAYLOAD_BYTES
    client.peer_transfer_protocol = TRANSFER_PROTOCOL_VERSION
    client._register_event_handlers()

    await client.sio.handlers[("/ws", "connector_transfer_start")](
        {
            "data": {
                "transfer_id": "incoming-pause",
                "op_id": "browser-in",
                "kind": "connector_browser_op",
                "context_id": "ctx-1",
                "total_bytes": 2 * 1024 * 1024,
                "sha256": "0" * 64,
            }
        }
    )
    spool_path = client._incoming_transfers["incoming-pause"].temp_path
    assert spool_path is not None and spool_path.exists()

    send_task = asyncio.create_task(
        client._emit_op_result(
            "connector_browser_op_result",
            {"op_id": "browser-out", "ok": True, "result": {"data": "x" * (2 * 1024 * 1024)}},
            context_id="ctx-1",
        )
    )
    await client.sio.started.wait()

    assert (await client.pause_agent("ctx-1"))["ok"] is True
    assert client._incoming_transfers == {}
    assert client._outgoing_transfers == {}
    assert not spool_path.exists()
    aborts = [
        payload
        for event, payload, _namespace in client.sio.emit_calls
        if event == "connector_transfer_abort"
    ]
    assert {payload["transfer_id"] for payload in aborts} == {
        "incoming-pause",
        next(payload["transfer_id"] for event, payload, _ in client.sio.emit_calls if event == "connector_transfer_start"),
    }

    client.sio.release.set()
    cancelled = await send_task
    assert cancelled["code"] == "TRANSFER_ABORTED"
    assert not any(
        event in {"connector_transfer_chunk", "connector_transfer_end"}
        for event, _payload, _namespace in client.sio.emit_calls
    )


async def test_disconnect_emits_abort_before_closing_socket() -> None:
    class OrderedSocket(FakeSocketIOClient):
        def __init__(self) -> None:
            super().__init__(connected=True)
            self.actions: list[str] = []

        async def emit(
            self,
            event: str,
            data: dict,
            namespace: str | None = None,
        ) -> None:
            if event == "connector_transfer_abort":
                assert self.connected is True
                self.actions.append("abort")
            await super().emit(event, data, namespace)

        async def disconnect(self) -> None:
            self.actions.append("disconnect")
            await super().disconnect()

    client = A0Client("http://127.0.0.1:50001")
    client.sio = OrderedSocket()
    client._register_event_handlers()
    await client.sio.handlers[("/ws", "connector_transfer_start")](
        {
            "data": {
                "transfer_id": "incoming-disconnect",
                "op_id": "browser-disconnect",
                "kind": "connector_browser_op",
                "context_id": "ctx-1",
                "total_bytes": 2 * 1024 * 1024,
                "sha256": "0" * 64,
            }
        }
    )
    spool_path = client._incoming_transfers["incoming-disconnect"].temp_path

    await client.disconnect(close_http=False)

    assert client.sio.actions == ["abort", "disconnect"]
    assert client._incoming_transfers == {}
    assert spool_path is not None and not spool_path.exists()


async def test_dropped_socket_cleans_transfer_and_reconnects() -> None:
    client = A0Client("http://127.0.0.1:50001")
    client.http = Mock()
    client.http.get = AsyncMock(
        return_value=FakeResponse(
            status_code=200,
            text='0{"sid":"sid-1","upgrades":["websocket"],"pingInterval":25000,"pingTimeout":20000}',
        )
    )
    client.sio = FakeSocketIOClient(connected=True)
    client._register_event_handlers()
    await client.sio.handlers[("/ws", "connector_transfer_start")](
        {
            "data": {
                "transfer_id": "incoming-drop",
                "op_id": "browser-drop",
                "kind": "connector_browser_op",
                "context_id": "ctx-1",
                "total_bytes": 2 * 1024 * 1024,
                "sha256": "0" * 64,
            }
        }
    )
    spool_path = client._incoming_transfers["incoming-drop"].temp_path

    await client.sio.disconnect()
    await client.connect_websocket()
    await client.send_hello()

    assert client.connected is True
    assert client._incoming_transfers == {}
    assert spool_path is not None and not spool_path.exists()
    assert client.sio.call_calls[-1][0] == "connector_hello"


@pytest.mark.skipif(
    not Path("/proc/self/status").exists(),
    reason="current RSS regression uses Linux /proc",
)
def test_cancelled_32_mib_transfer_returns_to_idle_and_releases_rss() -> None:
    project_root = Path(__file__).resolve().parents[1]
    script = r'''
import asyncio
import gc
import json
import time
from agent_zero_cli.client import A0Client, A0_WS_MAX_PAYLOAD_BYTES, TRANSFER_PROTOCOL_VERSION

def rss_kib():
    with open("/proc/self/status", encoding="utf-8") as handle:
        for line in handle:
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    raise RuntimeError("VmRSS unavailable")

class BlockingSocket:
    def __init__(self):
        self.connected = True
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.events = []

    async def emit(self, event, data, namespace=None):
        self.events.append(event)
        if event == "connector_transfer_start":
            self.started.set()
            await self.release.wait()

async def main():
    client = A0Client("http://127.0.0.1:1")
    socket = BlockingSocket()
    client.sio = socket
    client.peer_ws_max_payload_bytes = A0_WS_MAX_PAYLOAD_BYTES
    client.peer_transfer_protocol = TRANSFER_PROTOCOL_VERSION
    baseline = rss_kib()
    result = {"op_id": "rss-cancel", "ok": True, "result": {"data": "x" * (32 * 1024 * 1024)}}
    task = asyncio.create_task(
        client._emit_op_result(
            "connector_browser_op_result",
            result,
            context_id="ctx-rss",
        )
    )
    del result
    await socket.started.wait()
    active = rss_kib()
    await client._abort_active_transfers(
        context_id="ctx-rss",
        reason="stress cancellation",
    )
    socket.release.set()
    outcome = await task
    del task, outcome
    gc.collect()
    cpu_start = time.process_time()
    samples = []
    deadline = time.monotonic() + 1.0
    while True:
        samples.append(rss_kib())
        if time.monotonic() >= deadline:
            break
        await asyncio.sleep(0.05)
    cpu_after = time.process_time() - cpu_start
    await client.http.aclose()
    print(json.dumps({
        "baseline_kib": baseline,
        "active_kib": active,
        "minimum_after_kib": min(samples),
        "cpu_after_seconds": cpu_after,
        "events": socket.events,
        "incoming": len(client._incoming_transfers),
        "outgoing": len(client._outgoing_transfers),
    }))

asyncio.run(main())
'''
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        part
        for part in (str(project_root / "src"), env.get("PYTHONPATH", ""))
        if part
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=project_root,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    measurement = json.loads(completed.stdout)

    assert measurement["active_kib"] - measurement["baseline_kib"] > 48 * 1024
    assert measurement["minimum_after_kib"] - measurement["baseline_kib"] < 8 * 1024
    assert measurement["cpu_after_seconds"] < 0.25
    assert measurement["events"] == [
        "connector_transfer_start",
        "connector_transfer_abort",
    ]
    assert measurement["incoming"] == 0
    assert measurement["outgoing"] == 0


async def test_new_cli_rejects_large_result_for_peer_without_transfer_protocol() -> None:
    client = A0Client("http://127.0.0.1:50001")
    client.sio = FakeSocketIOClient(connected=True)
    client.peer_ws_max_payload_bytes = A0_WS_MAX_PAYLOAD_BYTES
    client.peer_transfer_protocol = 0

    emitted = await client._emit_op_result(
        "connector_browser_op_result",
        {"op_id": "legacy-core", "ok": True, "result": {"data": "x" * (2 * 1024 * 1024)}},
    )

    assert emitted["code"] == "PAYLOAD_TOO_LARGE"
    assert client.sio.emit_calls == [
        ("connector_browser_op_result", emitted, "/ws")
    ]
    assert client.sio.disconnect_calls == 0


@pytest.mark.large_payload_soak
@pytest.mark.skipif(
    os.getenv("A0_RUN_LARGE_PAYLOAD_SOAK") != "1",
    reason="set A0_RUN_LARGE_PAYLOAD_SOAK=1 for the 16-64 MiB soak",
)
@pytest.mark.parametrize("size_mib", [16, 32, 64])
async def test_large_payload_soak_keeps_socket_usable(size_mib: int) -> None:
    client = A0Client("http://127.0.0.1:50001")
    client.sio = FakeSocketIOClient(connected=True)
    client.peer_ws_max_payload_bytes = A0_WS_MAX_PAYLOAD_BYTES
    client.peer_transfer_protocol = TRANSFER_PROTOCOL_VERSION
    result = {
        "op_id": f"soak-{size_mib}",
        "ok": True,
        "result": {"data": "x" * (size_mib * 1024 * 1024)},
    }

    emitted = await client._emit_op_result("connector_browser_op_result", result)

    if size_mib <= 32:
        chunks = [
            payload
            for event, payload, _namespace in client.sio.emit_calls
            if event == "connector_transfer_chunk"
        ]
        assembled = b"".join(base64.b64decode(chunk["data"]) for chunk in chunks)
        assert json.loads(assembled) == result
        assert client.sio.emit_calls[-1][0] == "connector_transfer_end"
    else:
        assert emitted["code"] == "PAYLOAD_TOO_LARGE"
        assert client.sio.emit_calls == [
            ("connector_browser_op_result", emitted, "/ws")
        ]
    assert client.sio.disconnect_calls == 0


async def test_oversized_file_chunk_emits_one_structured_error() -> None:
    client = A0Client("http://127.0.0.1:50001")
    client.http = Mock()
    client.http.get = AsyncMock(
        return_value=FakeResponse(
            status_code=200,
            text='0{"sid":"sid-1","upgrades":["websocket"],"pingInterval":25000,"pingTimeout":20000}',
        )
    )
    client.sio = FakeSocketIOClient()
    client.peer_ws_max_payload_bytes = 2048
    client.on_file_op = AsyncMock(
        return_value={
            "op_id": "op-large",
            "ok": True,
            "result": {"content": "x" * 200_000},
        }
    )

    await client.connect_websocket()
    handler = client.sio.handlers[("/ws", "connector_file_op")]
    await handler({"data": {"op_id": "op-large", "op": "read", "path": "/tmp/large"}})

    assert len(client.sio.emit_calls) == 1
    event, payload, namespace = client.sio.emit_calls[0]
    assert (event, namespace) == ("connector_file_op_result", "/ws")
    assert payload["code"] == "PAYLOAD_TOO_LARGE"
    assert payload["op_id"] == "op-large"


async def test_settings_updated_event_unwraps_payload() -> None:
    client = A0Client("http://127.0.0.1:50001")
    client.http = Mock()
    client.http.get = AsyncMock(
        return_value=FakeResponse(
            status_code=200,
            text='0{"sid":"sid-1","upgrades":["websocket"],"pingInterval":25000,"pingTimeout":20000}',
        )
    )
    client.sio = FakeSocketIOClient()
    seen: list[dict[str, object]] = []
    client.on_settings_updated = lambda payload: seen.append(payload)

    await client.connect_websocket()

    handler = client.sio.handlers[("/ws", "connector_settings_updated")]
    await handler({"data": {"settings": {"agent_profile": "developer"}}})

    assert seen == [{"settings": {"agent_profile": "developer"}}]


async def test_exec_op_requests_are_returned_via_result_event() -> None:
    client = A0Client("http://127.0.0.1:50001")
    client.http = Mock()
    client.http.get = AsyncMock(
        return_value=FakeResponse(
            status_code=200,
            text='0{"sid":"sid-1","upgrades":["websocket"],"pingInterval":25000,"pingTimeout":20000}',
        )
    )
    client.sio = FakeSocketIOClient()
    client.on_exec_op = AsyncMock(
        return_value={"op_id": "exec-1", "ok": True, "result": {"runtime": "python"}}
    )

    await client.connect_websocket()

    handler = client.sio.handlers[("/ws", "connector_exec_op")]
    await handler({"data": {"op_id": "exec-1", "runtime": "terminal", "code": "pwd"}})

    client.on_exec_op.assert_awaited_once()
    assert client.sio.emit_calls == [
        (
            "connector_exec_op_result",
            {"op_id": "exec-1", "ok": True, "result": {"runtime": "python"}},
            "/ws",
        )
    ]


async def test_oversized_exec_result_becomes_structured_error_without_disconnect() -> None:
    client = A0Client("http://127.0.0.1:50001")
    client.http = Mock()
    client.http.get = AsyncMock(
        return_value=FakeResponse(
            status_code=200,
            text='0{"sid":"sid-1","upgrades":["websocket"],"pingInterval":25000,"pingTimeout":20000}',
        )
    )
    client.sio = FakeSocketIOClient()
    client.peer_ws_max_payload_bytes = 2048
    client.on_exec_op = AsyncMock(
        return_value={
            "op_id": "exec-large",
            "ok": True,
            "result": {"output": "x" * 4096},
        }
    )

    await client.connect_websocket()
    handler = client.sio.handlers[("/ws", "connector_exec_op")]
    await handler({"data": {"op_id": "exec-large", "runtime": "terminal", "code": "pwd"}})

    assert client.connected is True
    assert client.sio.connected is True
    assert len(client.sio.emit_calls) == 1
    event, payload, namespace = client.sio.emit_calls[0]
    assert event == "connector_exec_op_result"
    assert namespace == "/ws"
    assert payload["op_id"] == "exec-large"
    assert payload["ok"] is False
    assert payload["code"] == "PAYLOAD_TOO_LARGE"
    assert payload["details"]["alternative"] == "http_bulk_transfer"


async def test_registers_computer_use_ws_handler_and_emits_result() -> None:
    client = A0Client("http://127.0.0.1:50001")
    client.http = Mock()
    client.http.get = AsyncMock(
        return_value=FakeResponse(
            status_code=200,
            text='0{"sid":"sid-1","upgrades":["websocket"],"pingInterval":25000,"pingTimeout":20000}',
        )
    )
    client.sio = FakeSocketIOClient()
    client.on_computer_use_op = AsyncMock(
        return_value={"op_id": "cu-1", "ok": True, "result": {"status": "active"}}
    )
    after_calls: list[tuple[dict, dict, list[tuple[str, dict, str | None]]]] = []

    def after_result_sent(request: dict, result: dict) -> None:
        after_calls.append((dict(request), dict(result), list(client.sio.emit_calls)))

    client.on_computer_use_op_result_sent = after_result_sent

    await client.connect_websocket()

    handler = client.sio.handlers[("/ws", "connector_computer_use_op")]
    await handler({"data": {"op_id": "cu-1", "action": "status", "context_id": "ctx-1"}})

    client.on_computer_use_op.assert_awaited_once()
    assert client.sio.emit_calls == [
        (
            "connector_computer_use_op_result",
            {"op_id": "cu-1", "ok": True, "result": {"status": "active"}},
            "/ws",
        )
    ]
    assert after_calls == [
        (
            {"op_id": "cu-1", "action": "status", "context_id": "ctx-1"},
            {"op_id": "cu-1", "ok": True, "result": {"status": "active"}},
            [
                (
                    "connector_computer_use_op_result",
                    {"op_id": "cu-1", "ok": True, "result": {"status": "active"}},
                    "/ws",
                )
            ],
        )
    ]


async def test_registers_browser_ws_handler_and_emits_result() -> None:
    client = A0Client("http://127.0.0.1:50001")
    client.http = Mock()
    client.http.get = AsyncMock(
        return_value=FakeResponse(
            status_code=200,
            text='0{"sid":"sid-1","upgrades":["websocket"],"pingInterval":25000,"pingTimeout":20000}',
        )
    )
    client.sio = FakeSocketIOClient()
    client.on_browser_op = AsyncMock(
        return_value={"op_id": "browser-1", "ok": True, "result": {"id": 1}}
    )
    after_calls: list[tuple[dict, dict, list[tuple[str, dict, str | None]]]] = []

    def after_result_sent(request: dict, result: dict) -> None:
        after_calls.append((dict(request), dict(result), list(client.sio.emit_calls)))

    client.on_browser_op_result_sent = after_result_sent

    await client.connect_websocket()

    handler = client.sio.handlers[("/ws", "connector_browser_op")]
    await handler({"data": {"op_id": "browser-1", "action": "open", "context_id": "ctx-1"}})

    client.on_browser_op.assert_awaited_once()
    assert client.sio.emit_calls == [
        (
            "connector_browser_op_result",
            {"op_id": "browser-1", "ok": True, "result": {"id": 1}},
            "/ws",
        )
    ]
    assert after_calls == [
        (
            {"op_id": "browser-1", "action": "open", "context_id": "ctx-1"},
            {"op_id": "browser-1", "ok": True, "result": {"id": 1}},
            [
                (
                    "connector_browser_op_result",
                    {"op_id": "browser-1", "ok": True, "result": {"id": 1}},
                    "/ws",
                )
            ],
        )
    ]


async def test_gateway_control_result_is_emitted_before_follow_up_callback() -> None:
    client = A0Client("http://127.0.0.1:50001")
    client.http = Mock()
    client.http.get = AsyncMock(
        return_value=FakeResponse(
            status_code=200,
            text='0{"sid":"sid-1","upgrades":["websocket"],"pingInterval":25000,"pingTimeout":20000}',
        )
    )
    client.sio = FakeSocketIOClient()
    result = {
        "request_id": "control-1",
        "ok": True,
        "gateway": {"kind": "launcher", "state": "paused"},
    }
    client.on_gateway_control = AsyncMock(return_value=result)
    after_calls: list[list[tuple[str, dict, str | None]]] = []
    client.on_gateway_control_result_sent = (
        lambda _request, _result: after_calls.append(list(client.sio.emit_calls))
    )

    await client.connect_websocket()
    handler = client.sio.handlers[("/ws", "connector_gateway_control")]
    await handler({"data": {"request_id": "control-1", "action": "set_master", "enabled": False}})

    assert client.sio.emit_calls == [
        ("connector_gateway_control_result", result, "/ws")
    ]
    assert after_calls == [[("connector_gateway_control_result", result, "/ws")]]


async def test_gateway_control_handler_does_not_wait_for_follow_up_callback() -> None:
    client = A0Client("http://127.0.0.1:50001")
    client.http = Mock()
    client.http.get = AsyncMock(
        return_value=FakeResponse(
            status_code=200,
            text='0{"sid":"sid-1","upgrades":["websocket"],"pingInterval":25000,"pingTimeout":20000}',
        )
    )
    client.sio = FakeSocketIOClient()
    result = {"request_id": "control-1", "ok": True}
    client.on_gateway_control = AsyncMock(return_value=result)
    callback_started = asyncio.Event()
    callback_release = asyncio.Event()

    async def after_result_sent(_request: dict, _result: dict) -> None:
        callback_started.set()
        await callback_release.wait()

    client.on_gateway_control_result_sent = after_result_sent

    await client.connect_websocket()
    handler = client.sio.handlers[("/ws", "connector_gateway_control")]
    handler_task = asyncio.create_task(
        handler({"data": {"request_id": "control-1", "action": "set_master"}})
    )

    try:
        await asyncio.wait_for(callback_started.wait(), timeout=1)
        assert handler_task.done()
    finally:
        callback_release.set()
        await handler_task


async def test_computer_use_handler_error_is_serialized() -> None:
    client = A0Client("http://127.0.0.1:50001")
    client.http = Mock()
    client.http.get = AsyncMock(
        return_value=FakeResponse(
            status_code=200,
            text='0{"sid":"sid-1","upgrades":["websocket"],"pingInterval":25000,"pingTimeout":20000}',
        )
    )
    client.sio = FakeSocketIOClient()

    async def failing_handler(payload: dict[str, object]) -> dict[str, object]:
        del payload
        raise RuntimeError("portal unavailable")

    client.on_computer_use_op = failing_handler

    await client.connect_websocket()

    handler = client.sio.handlers[("/ws", "connector_computer_use_op")]
    await handler({"data": {"op_id": "cu-2", "action": "status", "context_id": "ctx-1"}})

    assert client.sio.emit_calls == [
        (
            "connector_computer_use_op_result",
            {
                "op_id": "cu-2",
                "ok": False,
                "error": "portal unavailable",
                "code": "COMPUTER_USE_ERROR",
            },
            "/ws",
        )
    ]


def test_ensure_aiohttp_ws_timeout_compat_returns_ws_close_on_old_aiohttp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = object()
    monkeypatch.delattr(aiohttp, "ClientWSTimeout", raising=False)

    _ensure_aiohttp_ws_timeout_compat()

    assert aiohttp.ClientWSTimeout(ws_close=12.5) == 12.5
    assert aiohttp.ClientWSTimeout(ws_close=None) is None
    assert aiohttp.ClientWSTimeout(ws_close=sentinel) is sentinel


async def test_subscribe_context_sends_optional_history_hints() -> None:
    client = A0Client("http://127.0.0.1:50001")
    client._call = AsyncMock(return_value={})  # type: ignore[method-assign]

    await client.subscribe_context("ctx-1", history="tail")
    await client.subscribe_context("ctx-1", history_before=100)

    assert client._call.await_args_list == [
        call("connector_subscribe_context", {"context_id": "ctx-1", "from": 0, "history": "tail"}),
        call("connector_subscribe_context", {"context_id": "ctx-1", "from": 0, "history_before": 100}),
    ]
    await client.http.aclose()
