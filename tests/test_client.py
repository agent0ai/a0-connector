import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CLI_SRC = REPO_ROOT / "cli" / "src"
if CLI_SRC.as_posix() not in sys.path:
    sys.path.insert(0, CLI_SRC.as_posix())

from agent_zero_cli.client import A0Client


class FakeResponse:
    def __init__(self, status_code: int = 200, json_data: dict | None = None, headers: dict | None = None) -> None:
        self.status_code = status_code
        self._json_data = json_data or {}
        self.headers = headers or {}

    def json(self) -> dict:
        return self._json_data

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("GET", "http://example.com")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError("error", request=request, response=response)


pytestmark = pytest.mark.anyio


async def test_check_health_returns_false_on_connect_error() -> None:
    client = A0Client("http://localhost:5080")
    request = httpx.Request("GET", "http://localhost:5080/health")
    client.http = Mock()
    client.http.get = AsyncMock(side_effect=httpx.ConnectError("boom", request=request))

    result = await client.check_health()

    assert result is False


async def test_check_health_returns_false_on_timeout() -> None:
    client = A0Client("http://localhost:5080")
    request = httpx.Request("GET", "http://localhost:5080/health")
    client.http = Mock()
    client.http.get = AsyncMock(side_effect=httpx.TimeoutException("boom", request=request))

    result = await client.check_health()

    assert result is False


async def test_check_health_returns_true_on_200() -> None:
    client = A0Client("http://localhost:5080")
    client.http = Mock()
    client.http.get = AsyncMock(return_value=FakeResponse(status_code=200))

    result = await client.check_health()

    assert result is True


async def test_needs_auth_200_sets_csrf_and_runtime_id() -> None:
    client = A0Client("http://localhost:5080")
    client.http = Mock()
    client.http.get = AsyncMock(
        return_value=FakeResponse(
            status_code=200,
            json_data={"token": "csrf-123", "runtime_id": "runtime-1"},
        )
    )

    result = await client.needs_auth()

    assert result is False
    assert client.csrf_token == "csrf-123"
    assert client.runtime_id == "runtime-1"


async def test_needs_auth_302_to_login_returns_true() -> None:
    client = A0Client("http://localhost:5080")
    client.http = Mock()
    client.http.get = AsyncMock(
        return_value=FakeResponse(status_code=302, headers={"location": "/login"})
    )

    result = await client.needs_auth()

    assert result is True


async def test_fetch_csrf_token_stores_token_and_runtime_id() -> None:
    client = A0Client("http://localhost:5080")
    client.http = Mock()
    client.http.get = AsyncMock(
        return_value=FakeResponse(
            status_code=200,
            json_data={"token": "csrf-xyz", "runtime_id": "runtime-9"},
        )
    )

    token = await client._fetch_csrf_token()

    assert token == "csrf-xyz"
    assert client.csrf_token == "csrf-xyz"
    assert client.runtime_id == "runtime-9"


async def test_build_cookie_header_includes_session_and_csrf_cookie() -> None:
    client = A0Client("http://localhost:5080")
    cookies = httpx.Cookies()
    cookies.set("session", "abc", domain="localhost", path="/")
    client.http = SimpleNamespace(cookies=cookies)
    client.csrf_token = "csrf-777"
    client.runtime_id = "runtime-77"

    header = client._build_cookie_header()

    assert "session=abc" in header
    assert "csrf_token_runtime-77=csrf-777" in header


async def test_request_state_unwraps_results_data() -> None:
    client = A0Client("http://localhost:5080")
    client.sio = Mock()
    client.sio.call = AsyncMock(
        return_value={"results": [{"data": {"runtime_epoch": 5, "seq_base": 10}}]}
    )

    result = await client.request_state("ctx-1")

    assert result == {"runtime_epoch": 5, "seq_base": 10}


async def test_api_call_retries_once_on_403() -> None:
    client = A0Client("http://localhost:5080")
    client.csrf_token = "csrf-old"
    client._ensure_csrf = AsyncMock()
    client._fetch_csrf_token = AsyncMock()
    client.http = Mock()
    client.http.request = AsyncMock(
        side_effect=[FakeResponse(status_code=403), FakeResponse(status_code=200)]
    )

    response = await client._api_call("GET", "/health")

    assert response.status_code == 200
    assert client.http.request.await_count == 2
    client._fetch_csrf_token.assert_awaited_once()


async def test_create_chat_returns_ctxid() -> None:
    client = A0Client("http://localhost:5080")
    client._api_call = AsyncMock(
        return_value=FakeResponse(status_code=200, json_data={"ctxid": "ctx-123"})
    )

    ctxid = await client.create_chat()

    assert ctxid == "ctx-123"
