import os
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from agent_zero_cli.client import A0Client
from agent_zero_cli.config import CLIConfig, load_config, save_env, _ENV_FILE


class FakeResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        json_data: dict | None = None,
        headers: dict | None = None,
    ) -> None:
        self.status_code = status_code
        self._json_data = json_data or {}
        self.headers = headers or {}

    def json(self) -> dict:
        return self._json_data

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("POST", "http://example.test")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError("error", request=request, response=response)


class FakeSocketIOClient:
    def __init__(self, *, call_response: dict | None = None) -> None:
        self.handlers: dict[tuple[str | None, str], object] = {}
        self.connect_calls: list[tuple[str, dict]] = []
        self.call_calls: list[tuple[str, dict, str | None]] = []
        self.emit_calls: list[tuple[str, dict, str | None]] = []
        self.call_response = call_response or {"results": [{"ok": True, "data": {}}]}
        self.connected = False

    def on(self, event: str, namespace: str | None = None):
        def decorator(func):
            self.handlers[(namespace, event)] = func
            return func

        return decorator

    async def connect(self, url: str, **kwargs) -> None:
        self.connect_calls.append((url, kwargs))
        self.connected = True

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

    async def disconnect(self) -> None:
        self.connected = False


pytestmark = pytest.mark.anyio


# ------------------------------------------------------------------
# Config: env var loading
# ------------------------------------------------------------------


def test_load_config_reads_from_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_ZERO_HOST", "http://10.0.0.1:9000")
    monkeypatch.setenv("AGENT_ZERO_API_KEY", "env-secret")

    config = load_config()

    assert config.instance_url == "http://10.0.0.1:9000"
    assert config.api_key == "env-secret"


def test_load_config_falls_back_to_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENT_ZERO_HOST", raising=False)
    monkeypatch.delenv("AGENT_ZERO_API_KEY", raising=False)

    env_dir = tmp_path / ".agent-zero"
    env_dir.mkdir()
    env_file = env_dir / ".env"
    env_file.write_text(
        "AGENT_ZERO_HOST=http://192.168.1.5:5080\nAGENT_ZERO_API_KEY=dotenv-key\n",
        encoding="utf-8",
    )

    import agent_zero_cli.config as config_mod
    monkeypatch.setattr(config_mod, "_ENV_FILE", env_file)

    config = load_config()

    assert config.instance_url == "http://192.168.1.5:5080"
    assert config.api_key == "dotenv-key"


def test_load_config_env_overrides_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_ZERO_HOST", "http://env-host:1234")
    monkeypatch.setenv("AGENT_ZERO_API_KEY", "env-key")

    env_dir = tmp_path / ".agent-zero"
    env_dir.mkdir()
    env_file = env_dir / ".env"
    env_file.write_text(
        "AGENT_ZERO_HOST=http://dotenv-host:5080\nAGENT_ZERO_API_KEY=dotenv-key\n",
        encoding="utf-8",
    )

    import agent_zero_cli.config as config_mod
    monkeypatch.setattr(config_mod, "_ENV_FILE", env_file)

    config = load_config()

    assert config.instance_url == "http://env-host:1234"
    assert config.api_key == "env-key"


def test_load_config_returns_empty_defaults(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("AGENT_ZERO_HOST", raising=False)
    monkeypatch.delenv("AGENT_ZERO_API_KEY", raising=False)

    import agent_zero_cli.config as config_mod
    monkeypatch.setattr(config_mod, "_ENV_FILE", tmp_path / "nonexistent" / ".env")

    config = load_config()

    assert config.instance_url == ""
    assert config.api_key == ""


def test_save_env_creates_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_dir = tmp_path / ".agent-zero"
    env_file = env_dir / ".env"

    import agent_zero_cli.config as config_mod
    monkeypatch.setattr(config_mod, "_ENV_DIR", env_dir)
    monkeypatch.setattr(config_mod, "_ENV_FILE", env_file)

    save_env("AGENT_ZERO_HOST", "http://myhost:5080")

    assert env_file.exists()
    content = env_file.read_text(encoding="utf-8")
    assert "AGENT_ZERO_HOST=http://myhost:5080" in content


def test_save_env_updates_existing_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_dir = tmp_path / ".agent-zero"
    env_dir.mkdir()
    env_file = env_dir / ".env"
    env_file.write_text("AGENT_ZERO_HOST=http://old:5080\n", encoding="utf-8")

    import agent_zero_cli.config as config_mod
    monkeypatch.setattr(config_mod, "_ENV_DIR", env_dir)
    monkeypatch.setattr(config_mod, "_ENV_FILE", env_file)

    save_env("AGENT_ZERO_HOST", "http://new:9090")

    content = env_file.read_text(encoding="utf-8")
    assert "AGENT_ZERO_HOST=http://new:9090" in content
    assert "http://old:5080" not in content


# ------------------------------------------------------------------
# Client: login
# ------------------------------------------------------------------


async def test_login_returns_api_key_on_success() -> None:
    client = A0Client("http://localhost:5080")
    client.http = Mock()
    client.http.post = AsyncMock(
        return_value=FakeResponse(status_code=200, json_data={"api_key": "abc123"})
    )

    result = await client.login("admin", "password")

    assert result == "abc123"
    assert client.api_key == "abc123"
    client.http.post.assert_awaited_once_with(
        "http://localhost:5080/api/plugins/a0_connector/v1/connector_login",
        json={"username": "admin", "password": "password"},
        headers={},
    )


async def test_login_returns_none_on_401() -> None:
    client = A0Client("http://localhost:5080")
    client.http = Mock()
    client.http.post = AsyncMock(
        return_value=FakeResponse(status_code=401)
    )

    result = await client.login("admin", "wrong")

    assert result is None
    assert client.api_key == ""


# ------------------------------------------------------------------
# Client: existing tests (unchanged behavior)
# ------------------------------------------------------------------


async def test_check_health_posts_capabilities() -> None:
    client = A0Client("http://localhost:5080", api_key="secret")
    client.http = Mock()
    client.http.post = AsyncMock(return_value=FakeResponse(status_code=200))

    result = await client.check_health()

    assert result is True
    client.http.post.assert_awaited_once_with(
        "http://localhost:5080/api/plugins/a0_connector/v1/capabilities",
        json={},
        headers={},
    )


async def test_check_health_returns_false_on_connect_error() -> None:
    client = A0Client("http://localhost:5080")
    request = httpx.Request(
        "POST", "http://localhost:5080/api/plugins/a0_connector/v1/capabilities"
    )
    client.http = Mock()
    client.http.post = AsyncMock(side_effect=httpx.ConnectError("boom", request=request))

    result = await client.check_health()

    assert result is False


async def test_verify_api_key_uses_x_api_key_header() -> None:
    client = A0Client("http://localhost:5080", api_key="dev-a0-connector")
    client.http = Mock()
    client.http.post = AsyncMock(return_value=FakeResponse(status_code=200))

    result = await client.verify_api_key()

    assert result is True
    client.http.post.assert_awaited_once_with(
        "http://localhost:5080/api/plugins/a0_connector/v1/chats_list",
        json={},
        headers={"X-API-KEY": "dev-a0-connector"},
    )


async def test_verify_api_key_returns_false_on_401() -> None:
    client = A0Client("http://localhost:5080", api_key="bad-key")
    client.http = Mock()
    client.http.post = AsyncMock(return_value=FakeResponse(status_code=401))

    result = await client.verify_api_key()

    assert result is False


async def test_connect_websocket_uses_ws_auth_payload() -> None:
    client = A0Client("http://127.0.0.1:50001", api_key="dev-a0-connector")
    fake_sio = FakeSocketIOClient()
    client.sio = fake_sio

    await client.connect_websocket()

    assert fake_sio.connect_calls == [
        (
            "http://127.0.0.1:50001",
            {
                "namespaces": ["/ws"],
                "headers": {
                    "Origin": "http://127.0.0.1:50001",
                    "Referer": "http://127.0.0.1:50001/",
                },
                "auth": {
                    "api_key": "dev-a0-connector",
                    "handlers": ["plugins/a0_connector/ws_connector"],
                },
                "transports": ["websocket"],
            },
        )
    ]


async def test_send_message_uses_prefixed_ws_event() -> None:
    client = A0Client("http://127.0.0.1:50001", api_key="dev-a0-connector")
    fake_sio = FakeSocketIOClient(
        call_response={
            "results": [
                {
                    "ok": True,
                    "data": {"context_id": "ctx-1", "status": "accepted"},
                }
            ]
        }
    )
    client.sio = fake_sio

    result = await client.send_message("hello", "ctx-1")

    assert result == {"context_id": "ctx-1", "status": "accepted"}
    event, payload, namespace = fake_sio.call_calls[0]
    assert event == "connector_send_message"
    assert namespace == "/ws"
    assert payload["context_id"] == "ctx-1"
    assert payload["message"] == "hello"
    assert "client_message_id" in payload


async def test_file_op_requests_are_returned_via_result_event() -> None:
    client = A0Client("http://127.0.0.1:50001", api_key="dev-a0-connector")
    fake_sio = FakeSocketIOClient()
    client.sio = fake_sio
    client.on_file_op = AsyncMock(
        return_value={
            "op_id": "op-1",
            "ok": True,
            "result": {"path": "/tmp/example.txt"},
        }
    )

    await client.connect_websocket()

    handler = fake_sio.handlers[("/ws", "connector_file_op")]
    await handler(
        {
            "handlerId": "plugins.a0_connector.api.ws_connector.WsConnector",
            "eventId": "evt-1",
            "correlationId": "corr-1",
            "ts": "2026-04-01T00:00:00Z",
            "data": {
                "op_id": "op-1",
                "op": "read",
                "path": "/tmp/example.txt",
            },
        }
    )

    client.on_file_op.assert_awaited_once()
    assert fake_sio.emit_calls == [
        (
            "connector_file_op_result",
            {
                "op_id": "op-1",
                "ok": True,
                "result": {"path": "/tmp/example.txt"},
            },
            "/ws",
        )
    ]
