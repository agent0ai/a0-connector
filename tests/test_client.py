import os
from pathlib import Path
from unittest.mock import AsyncMock, Mock

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
from agent_zero_cli.config import CLIConfig, load_config, save_env, _ENV_FILE


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
        connect_error_payload: object | None = None,
        connect_exception: Exception | None = None,
    ) -> None:
        self.handlers: dict[tuple[str | None, str], object] = {}
        self.connect_calls: list[tuple[str, dict]] = []
        self.call_calls: list[tuple[str, dict, str | None]] = []
        self.emit_calls: list[tuple[str, dict, str | None]] = []
        self.call_response = call_response or {"results": [{"ok": True, "data": {}}]}
        self.connect_error_payload = connect_error_payload
        self.connect_exception = connect_exception
        self.connected = False

    def on(self, event: str, namespace: str | None = None):
        def decorator(func):
            self.handlers[(namespace, event)] = func
            return func

        return decorator

    async def connect(self, url: str, **kwargs) -> None:
        self.connect_calls.append((url, kwargs))
        if self.connect_error_payload is not None:
            for namespace in (None, "/ws"):
                handler = self.handlers.get((namespace, "connect_error"))
                if handler is not None:
                    await handler(self.connect_error_payload)
        if self.connect_exception is not None:
            raise self.connect_exception
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


async def test_fetch_capabilities_raises_plugin_missing_on_404() -> None:
    client = A0Client("http://localhost:5080")
    client.http = Mock()
    client.http.post = AsyncMock(return_value=FakeResponse(status_code=404))

    with pytest.raises(A0ConnectorPluginMissingError):
        await client.fetch_capabilities()


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
    client.http = Mock()
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
                    "Origin": "http://127.0.0.1:50001",
                    "Referer": "http://127.0.0.1:50001/",
                },
                "auth": {
                    "api_key": "dev-a0-connector",
                    "handlers": ["plugins/a0_connector/ws_connector"],
                },
            },
        )
    ]


async def test_connect_websocket_surfaces_connect_error_payload() -> None:
    client = A0Client("http://127.0.0.1:50001", api_key="dev-a0-connector")
    client.http = Mock()
    client.http.get = AsyncMock(
        return_value=FakeResponse(
            status_code=200,
            text='0{"sid":"sid-1","upgrades":["websocket"],"pingInterval":25000,"pingTimeout":20000}',
        )
    )
    fake_sio = FakeSocketIOClient(
        connect_error_payload={
            "code": "origin_rejected",
            "message": "Origin not allowed",
            "details": "expected https://agent-zero.example",
        },
        connect_exception=socketio.exceptions.ConnectionError(""),
    )
    client.sio = fake_sio

    with pytest.raises(
        A0WebSocketConnectionError,
        match=r"Socket\.IO transport probe succeeded, but the /ws namespace connection was rejected: "
        r"origin_rejected: Origin not allowed \(expected https://agent-zero\.example\)\. "
        r"This usually means an Origin/Referer or proxy host mismatch\.",
    ):
        await client.connect_websocket()


async def test_connect_websocket_reports_missing_socketio_forwarding() -> None:
    client = A0Client("http://127.0.0.1:50001", api_key="dev-a0-connector")
    client.http = Mock()
    client.http.get = AsyncMock(return_value=FakeResponse(status_code=404, text="Not Found"))
    fake_sio = FakeSocketIOClient()
    client.sio = fake_sio

    with pytest.raises(
        A0WebSocketConnectionError,
        match=r"Socket\.IO transport probe failed: GET http://127\.0\.0\.1:50001/socket\.io\?transport=polling&EIO=4 returned HTTP 404\. "
        r"Ensure Agent Zero is running and any reverse proxy forwards /socket\.io unchanged \(not just /api/plugins/\)\.",
    ):
        await client.connect_websocket()

    assert fake_sio.connect_calls == []


async def test_connect_websocket_reports_blank_namespace_rejection_after_probe() -> None:
    client = A0Client("http://127.0.0.1:50001", api_key="dev-a0-connector")
    client.http = Mock()
    client.http.get = AsyncMock(
        return_value=FakeResponse(
            status_code=200,
            text='0{"sid":"sid-1","upgrades":["websocket"],"pingInterval":25000,"pingTimeout":20000}',
        )
    )
    fake_sio = FakeSocketIOClient(
        connect_exception=socketio.exceptions.ConnectionError(""),
    )
    client.sio = fake_sio

    with pytest.raises(
        A0WebSocketConnectionError,
        match=r"Socket\.IO transport probe succeeded, but the /ws namespace connection was rejected\. "
        r"This usually means an Origin/Referer or proxy host mismatch\. Check that AGENT_ZERO_HOST exactly matches the Agent Zero URL",
    ):
        await client.connect_websocket()


def test_ensure_aiohttp_ws_timeout_compat_returns_ws_close_on_old_aiohttp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = object()
    monkeypatch.delattr(aiohttp, "ClientWSTimeout", raising=False)

    _ensure_aiohttp_ws_timeout_compat()

    assert aiohttp.ClientWSTimeout(ws_close=12.5) == 12.5
    assert aiohttp.ClientWSTimeout(ws_close=None) is None
    assert aiohttp.ClientWSTimeout(ws_close=sentinel) is sentinel


async def test_connect_websocket_patches_old_aiohttp_before_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delattr(aiohttp, "ClientWSTimeout", raising=False)

    client = A0Client("http://127.0.0.1:50001", api_key="dev-a0-connector")
    client.http = Mock()
    client.http.get = AsyncMock(
        return_value=FakeResponse(
            status_code=200,
            text='0{"sid":"sid-1","upgrades":["websocket"],"pingInterval":25000,"pingTimeout":20000}',
        )
    )
    fake_sio = FakeSocketIOClient()
    client.sio = fake_sio

    await client.connect_websocket()

    assert aiohttp.ClientWSTimeout(ws_close=7.0) == 7.0
    assert fake_sio.connect_calls[0][1]["auth"] == {
        "api_key": "dev-a0-connector",
        "handlers": ["plugins/a0_connector/ws_connector"],
    }


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


async def test_get_settings_posts_to_connector_endpoint() -> None:
    client = A0Client("http://localhost:5080", api_key="secret")
    client.http = Mock()
    client.http.post = AsyncMock(
        return_value=FakeResponse(
            status_code=200,
            json_data={"settings": {"agent_profile": "default"}},
        )
    )

    result = await client.get_settings()

    assert result == {"settings": {"agent_profile": "default"}}
    client.http.post.assert_awaited_once_with(
        "http://localhost:5080/api/plugins/a0_connector/v1/settings_get",
        json={},
        headers={"X-API-KEY": "secret"},
    )


async def test_set_settings_posts_curated_payload() -> None:
    client = A0Client("http://localhost:5080", api_key="secret")
    client.http = Mock()
    client.http.post = AsyncMock(
        return_value=FakeResponse(
            status_code=200,
            json_data={"settings": {"agent_profile": "researcher"}},
        )
    )

    result = await client.set_settings({"agent_profile": "researcher"})

    assert result == {"settings": {"agent_profile": "researcher"}}
    client.http.post.assert_awaited_once_with(
        "http://localhost:5080/api/plugins/a0_connector/v1/settings_set",
        json={"settings": {"agent_profile": "researcher"}},
        headers={"X-API-KEY": "secret"},
    )


async def test_get_chat_uses_context_id_payload() -> None:
    client = A0Client("http://localhost:5080", api_key="secret")
    client.http = Mock()
    client.http.post = AsyncMock(
        return_value=FakeResponse(status_code=200, json_data={"context_id": "ctx-1"})
    )

    result = await client.get_chat("ctx-1")

    assert result == {"context_id": "ctx-1"}
    client.http.post.assert_awaited_once_with(
        "http://localhost:5080/api/plugins/a0_connector/v1/chat_get",
        json={"context_id": "ctx-1"},
        headers={"X-API-KEY": "secret"},
    )


async def test_pause_agent_posts_pause_request_and_normalizes_success() -> None:
    client = A0Client("http://localhost:5080", api_key="secret")
    client.http = Mock()
    client.http.post = AsyncMock(
        return_value=FakeResponse(
            status_code=200,
            json_data={"ok": True, "paused": True, "message": "Agent paused."},
        )
    )

    result = await client.pause_agent("ctx-1")

    assert result == {"ok": True, "paused": True, "message": "Agent paused."}
    client.http.post.assert_awaited_once_with(
        "http://localhost:5080/api/plugins/a0_connector/v1/pause",
        json={"context_id": "ctx-1", "paused": True},
        headers={"X-API-KEY": "secret"},
    )


async def test_pause_agent_can_resume_with_paused_false() -> None:
    client = A0Client("http://localhost:5080", api_key="secret")
    client.http = Mock()
    client.http.post = AsyncMock(
        return_value=FakeResponse(
            status_code=200,
            json_data={"ok": True, "paused": False, "message": "Agent unpaused."},
        )
    )

    result = await client.pause_agent("ctx-1", paused=False)

    assert result == {"ok": True, "paused": False, "message": "Agent unpaused."}
    client.http.post.assert_awaited_once_with(
        "http://localhost:5080/api/plugins/a0_connector/v1/pause",
        json={"context_id": "ctx-1", "paused": False},
        headers={"X-API-KEY": "secret"},
    )


async def test_pause_agent_normalizes_http_failure() -> None:
    client = A0Client("http://localhost:5080", api_key="secret")
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


async def test_nudge_agent_posts_nudge_request_and_normalizes_success() -> None:
    client = A0Client("http://localhost:5080", api_key="secret")
    client.http = Mock()
    client.http.post = AsyncMock(
        return_value=FakeResponse(
            status_code=200,
            json_data={"ok": True, "status": "nudged", "message": "Process reset, agent nudged."},
        )
    )

    result = await client.nudge_agent("ctx-1")

    assert result == {"ok": True, "status": "nudged", "message": "Process reset, agent nudged."}
    client.http.post.assert_awaited_once_with(
        "http://localhost:5080/api/plugins/a0_connector/v1/nudge",
        json={"context_id": "ctx-1"},
        headers={"X-API-KEY": "secret"},
    )


async def test_nudge_agent_normalizes_http_failure() -> None:
    client = A0Client("http://localhost:5080", api_key="secret")
    client.http = Mock()
    client.http.post = AsyncMock(
        return_value=FakeResponse(status_code=409, text="Context is already running")
    )

    result = await client.nudge_agent("ctx-1")

    assert result == {
        "ok": False,
        "message": "Context is already running",
        "status_code": 409,
    }


async def test_list_projects_returns_project_array() -> None:
    client = A0Client("http://localhost:5080", api_key="secret")
    client.http = Mock()
    client.http.post = AsyncMock(
        return_value=FakeResponse(
            status_code=200,
            json_data={"projects": [{"name": "agent-zero"}]},
        )
    )

    result = await client.list_projects()

    assert result == [{"name": "agent-zero"}]
    client.http.post.assert_awaited_once_with(
        "http://localhost:5080/api/plugins/a0_connector/v1/projects_list",
        json={},
        headers={"X-API-KEY": "secret"},
    )


async def test_get_model_presets_returns_preset_list() -> None:
    client = A0Client("http://localhost:5080", api_key="secret")
    client.http = Mock()
    client.http.post = AsyncMock(
        return_value=FakeResponse(
            status_code=200,
            json_data={"ok": True, "presets": [{"name": "Balanced"}]},
        )
    )

    result = await client.get_model_presets()

    assert result == [{"name": "Balanced"}]
    client.http.post.assert_awaited_once_with(
        "http://localhost:5080/api/plugins/a0_connector/v1/model_presets",
        json={},
        headers={"X-API-KEY": "secret"},
    )


async def test_get_model_switcher_returns_current_models_and_override() -> None:
    client = A0Client("http://localhost:5080", api_key="secret")
    client.http = Mock()
    client.http.post = AsyncMock(
        return_value=FakeResponse(
            status_code=200,
            json_data={
                "ok": True,
                "allowed": True,
                "override": {"preset_name": "Balanced"},
                "main_model": {"provider": "anthropic", "name": "claude-haiku-4-5"},
                "utility_model": {"provider": "anthropic", "name": "claude-haiku-4-5"},
                "presets": [{"name": "Balanced"}],
            },
        )
    )

    result = await client.get_model_switcher("ctx-1")

    assert result["override"] == {"preset_name": "Balanced"}
    client.http.post.assert_awaited_once_with(
        "http://localhost:5080/api/plugins/a0_connector/v1/model_switcher",
        json={"action": "get", "context_id": "ctx-1"},
        headers={"X-API-KEY": "secret"},
    )


async def test_set_model_preset_posts_set_preset_action() -> None:
    client = A0Client("http://localhost:5080", api_key="secret")
    client.http = Mock()
    client.http.post = AsyncMock(
        return_value=FakeResponse(status_code=200, json_data={"ok": True, "override": {"preset_name": "Fast"}})
    )

    result = await client.set_model_preset("ctx-1", "Fast")

    assert result["override"] == {"preset_name": "Fast"}
    client.http.post.assert_awaited_once_with(
        "http://localhost:5080/api/plugins/a0_connector/v1/model_switcher",
        json={"context_id": "ctx-1", "action": "set_preset", "preset_name": "Fast"},
        headers={"X-API-KEY": "secret"},
    )


async def test_set_model_preset_can_clear_override() -> None:
    client = A0Client("http://localhost:5080", api_key="secret")
    client.http = Mock()
    client.http.post = AsyncMock(
        return_value=FakeResponse(status_code=200, json_data={"ok": True, "override": None})
    )

    result = await client.set_model_preset("ctx-1", None)

    assert result["override"] is None
    client.http.post.assert_awaited_once_with(
        "http://localhost:5080/api/plugins/a0_connector/v1/model_switcher",
        json={"context_id": "ctx-1", "action": "clear"},
        headers={"X-API-KEY": "secret"},
    )


async def test_get_compaction_stats_normalizes_http_failure() -> None:
    client = A0Client("http://localhost:5080", api_key="secret")
    client.http = Mock()
    client.http.post = AsyncMock(
        return_value=FakeResponse(status_code=409, text="Cannot compact while agent is running")
    )

    result = await client.get_compaction_stats("ctx-1")

    assert result == {
        "ok": False,
        "message": "Cannot compact while agent is running",
        "status_code": 409,
    }
    client.http.post.assert_awaited_once_with(
        "http://localhost:5080/api/plugins/a0_connector/v1/compact_chat",
        json={"context_id": "ctx-1", "action": "stats"},
        headers={"X-API-KEY": "secret"},
    )


async def test_compact_chat_posts_selected_model_and_preset() -> None:
    client = A0Client("http://localhost:5080", api_key="secret")
    client.http = Mock()
    client.http.post = AsyncMock(
        return_value=FakeResponse(status_code=200, json_data={"ok": True, "message": "Compaction started"})
    )

    result = await client.compact_chat(
        "ctx-1",
        use_chat_model=False,
        preset_name="Balanced",
    )

    assert result == {"ok": True, "message": "Compaction started"}
    client.http.post.assert_awaited_once_with(
        "http://localhost:5080/api/plugins/a0_connector/v1/compact_chat",
        json={
            "context_id": "ctx-1",
            "action": "compact",
            "use_chat_model": False,
            "preset_name": "Balanced",
        },
        headers={"X-API-KEY": "secret"},
    )


async def test_file_op_requests_are_returned_via_result_event() -> None:
    client = A0Client("http://127.0.0.1:50001", api_key="dev-a0-connector")
    client.http = Mock()
    client.http.get = AsyncMock(
        return_value=FakeResponse(
            status_code=200,
            text='0{"sid":"sid-1","upgrades":["websocket"],"pingInterval":25000,"pingTimeout":20000}',
        )
    )
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


async def test_exec_op_requests_are_returned_via_result_event() -> None:
    client = A0Client("http://127.0.0.1:50001", api_key="dev-a0-connector")
    client.http = Mock()
    client.http.get = AsyncMock(
        return_value=FakeResponse(
            status_code=200,
            text='0{"sid":"sid-1","upgrades":["websocket"],"pingInterval":25000,"pingTimeout":20000}',
        )
    )
    fake_sio = FakeSocketIOClient()
    client.sio = fake_sio
    client.on_exec_op = AsyncMock(
        return_value={
            "op_id": "exec-1",
            "ok": True,
            "result": {"message": "Session 0 completed.", "output": "42", "running": False},
        }
    )

    await client.connect_websocket()

    handler = fake_sio.handlers[("/ws", "connector_exec_op")]
    await handler(
        {
            "handlerId": "plugins.a0_connector.api.ws_connector.WsConnector",
            "eventId": "evt-2",
            "correlationId": "corr-2",
            "ts": "2026-04-01T00:00:00Z",
            "data": {
                "op_id": "exec-1",
                "runtime": "python",
                "session": 0,
                "code": "print(42)",
            },
        }
    )

    client.on_exec_op.assert_awaited_once()
    assert fake_sio.emit_calls == [
        (
            "connector_exec_op_result",
            {
                "op_id": "exec-1",
                "ok": True,
                "result": {"message": "Session 0 completed.", "output": "42", "running": False},
            },
            "/ws",
        )
    ]


async def test_send_remote_tree_update_uses_prefixed_ws_event() -> None:
    client = A0Client("http://127.0.0.1:50001", api_key="dev-a0-connector")
    fake_sio = FakeSocketIOClient(
        call_response={
            "results": [
                {
                    "ok": True,
                    "data": {"accepted": True},
                }
            ]
        }
    )
    client.sio = fake_sio

    payload = {
        "root_path": "/tmp/workspace",
        "tree": "/tmp/workspace/\n└── app.py",
        "tree_hash": "abc123",
        "generated_at": "2026-04-08T12:00:00Z",
    }
    result = await client.send_remote_tree_update(payload)

    assert result == {"accepted": True}
    event, sent_payload, namespace = fake_sio.call_calls[0]
    assert event == "connector_remote_tree_update"
    assert namespace == "/ws"
    assert sent_payload == payload
