from __future__ import annotations

import ssl
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from aiohttp import web
import aiohttp
import httpx
import pytest
import socketio

from agent_zero_cli.client import (
    A0Client,
    A0ConnectorPluginMissingError,
    A0WebSocketConnectionError,
    _ensure_aiohttp_ws_timeout_compat,
)
from agent_zero_cli.config import (
    load_config,
    save_computer_use_enabled,
    save_computer_use_restore_token,
    save_computer_use_trust_mode,
    save_env,
    save_last_context,
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
    ) -> None:
        self.status_code = status_code
        self._json_data = json_data or {}
        self.headers = headers or {}
        self.text = text

    def json(self) -> dict:
        return self._json_data

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("POST", "http://example.test")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError("error", request=request, response=response)


class FakeSocketIOClient:
    def __init__(
        self,
        *,
        call_response: dict | None = None,
        connect_exception: Exception | None = None,
    ) -> None:
        self.handlers: dict[tuple[str | None, str], object] = {}
        self.connect_calls: list[tuple[str, dict]] = []
        self.call_calls: list[tuple[str, dict, str | None]] = []
        self.emit_calls: list[tuple[str, dict, str | None]] = []
        self.call_response = call_response or {"results": [{"ok": True, "data": {}}]}
        self.connect_exception = connect_exception

    def on(self, event: str, namespace: str | None = None):
        def decorator(func):
            self.handlers[(namespace, event)] = func
            return func

        return decorator

    async def connect(self, url: str, **kwargs) -> None:
        self.connect_calls.append((url, kwargs))
        if self.connect_exception is not None:
            raise self.connect_exception

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

    env_dir = tmp_path / ".agent-zero"
    env_dir.mkdir()
    env_file = env_dir / ".env"
    env_file.write_text(
        "\n".join(
            (
                "AGENT_ZERO_HOST=http://dotenv-host:5080",
                "AGENT_ZERO_LAST_CONTEXT_ID=ctx-dotenv",
                "AGENT_ZERO_LAST_CONTEXT_HOST=http://dotenv-host:5080",
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
                "AGENT_ZERO_COMPUTER_USE_TRUST_MODE=interactive",
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
    assert dotenv_config.computer_use_trust_mode == "interactive"
    assert dotenv_config.computer_use_restore_token == "dotenv-token"

    monkeypatch.setenv("AGENT_ZERO_COMPUTER_USE_ENABLED", "0")
    monkeypatch.setenv("AGENT_ZERO_COMPUTER_USE_TRUST_MODE", "free_run")
    monkeypatch.setenv("AGENT_ZERO_COMPUTER_USE_RESTORE_TOKEN", "env-token")

    env_config = load_config()
    assert env_config.computer_use_enabled is False
    assert env_config.computer_use_trust_mode == "free_run"
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
    save_computer_use_trust_mode("free_run")
    save_computer_use_restore_token("restore-token")
    save_computer_use_restore_token("")

    contents = env_file.read_text(encoding="utf-8").splitlines()
    assert "AGENT_ZERO_COMPUTER_USE_ENABLED=1" in contents
    assert "AGENT_ZERO_COMPUTER_USE_TRUST_MODE=free_run" in contents
    assert not any(line.startswith("AGENT_ZERO_COMPUTER_USE_RESTORE_TOKEN=") for line in contents)


async def test_fetch_capabilities_raises_plugin_missing_on_404() -> None:
    client = A0Client("http://localhost:5080")
    client.http = Mock()
    client.http.post = AsyncMock(return_value=FakeResponse(status_code=404))

    with pytest.raises(A0ConnectorPluginMissingError):
        await client.fetch_capabilities()


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


async def test_connect_websocket_accepts_self_signed_https_connector() -> None:
    async with self_signed_connector_server() as base_url:
        client = A0Client(base_url)
        try:
            await client.connect_websocket()

            assert client.connected is True
            assert client.sio.connected is True
        finally:
            await client.disconnect()


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


async def test_send_hello_returns_exec_config_payload() -> None:
    client = A0Client("http://127.0.0.1:50001")
    client.sio = FakeSocketIOClient(
        call_response={
            "results": [
                {
                    "ok": True,
                    "data": {
                        "protocol": "a0-connector.v1",
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
    event, payload, namespace = client.sio.call_calls[0]
    assert event == "connector_hello"
    assert namespace == "/ws"
    assert payload["protocol"] == "a0-connector.v1"


async def test_send_hello_includes_computer_use_metadata() -> None:
    client = A0Client("http://127.0.0.1:50001")
    client.sio = FakeSocketIOClient(
        call_response={"results": [{"ok": True, "data": {"protocol": "a0-connector.v1"}}]}
    )

    metadata = {
        "supported": True,
        "enabled": True,
        "trust_mode": "persistent",
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
