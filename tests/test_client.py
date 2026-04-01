import json
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from agent_zero_cli.client import A0Client
from agent_zero_cli.config import load_config


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


def test_load_config_reads_api_key_from_cli_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = tmp_path / ".cli-config.json"
    config_path.write_text(
        json.dumps(
            {
                "instance_url": "http://127.0.0.1:50001",
                "api_key": "dev-a0-connector",
                "theme": "dark",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    config = load_config()

    assert config.instance_url == "http://127.0.0.1:50001"
    assert config.api_key == "dev-a0-connector"
    assert config.theme == "dark"


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
