from __future__ import annotations

import base64

import acp
import pytest
from acp.schema import AuthCapabilities, ClientCapabilities

from agent_zero_cli import acp as acp_mod
from agent_zero_cli.acp import AcpOptions, AgentZeroACPAgent, _container_command, _login_for_acp, _prompt_parts
from agent_zero_cli.config import CLIConfig


class _Text:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class _Binary:
    type = "image"
    mime_type = "image/png"

    def __init__(self, data: str) -> None:
        self.data = data


def test_acp_prompt_and_container_translation() -> None:
    text, uploads = _prompt_parts(
        [_Text("  inspect this  "), _Binary(base64.b64encode(b"png").decode())]
    )

    assert text == "inspect this"
    assert [(upload.filename, upload.content, upload.mime_type) for upload in uploads] == [
        ("acp-1", b"png", "image/png")
    ]
    assert _container_command(
        AcpOptions(debug=True),
        {"container_id": "agent-zero", "container_workdir": "/a0", "container_python": "python"},
    ) == ["docker", "exec", "-i", "-w", "/a0", "agent-zero", "python", "-m", "usr.plugins.a0_acp", "--debug"]


@pytest.mark.anyio
async def test_acp_initialize_returns_schema_valid_close_capabilities() -> None:
    response = await AgentZeroACPAgent(AcpOptions(host="http://agent.test"), CLIConfig()).initialize()

    assert response.protocol_version == acp.PROTOCOL_VERSION
    assert response.agent_capabilities.auth is None
    assert response.agent_capabilities.session_capabilities.close is not None
    assert response.auth_methods == []


@pytest.mark.anyio
async def test_acp_initialize_advertises_terminal_login_only_when_supported() -> None:
    response = await AgentZeroACPAgent(AcpOptions(host="http://agent.test"), CLIConfig()).initialize(
        client_capabilities=ClientCapabilities(auth=AuthCapabilities(terminal=True))
    )

    assert len(response.auth_methods) == 1
    method = response.auth_methods[0]
    assert method.id == "a0-web-login"
    assert method.type == "terminal"
    assert method.args == ["--login"]


class _LoginClient:
    auth_required = True
    restored = False
    session_valid = False
    login_result = True
    instances: list["_LoginClient"] = []

    def __init__(self, host: str) -> None:
        self.host = host
        self.login_calls: list[tuple[str, str]] = []
        self.persisted_hosts: list[str] = []
        self.cleared_hosts: list[str] = []
        self.disconnected = False
        self.__class__.instances.append(self)

    async def fetch_capabilities(self) -> dict[str, bool]:
        return {"auth_required": self.auth_required}

    def restore_session(self, host: str) -> bool:
        assert host == self.host
        return self.restored

    async def verify_session(self) -> bool:
        return self.session_valid

    def clear_persisted_session(self, host: str) -> None:
        self.cleared_hosts.append(host)

    async def login(self, username: str, password: str) -> bool:
        self.login_calls.append((username, password))
        return self.login_result

    def persist_session(self, host: str) -> None:
        self.persisted_hosts.append(host)

    async def disconnect(self, **_: object) -> None:
        self.disconnected = True


@pytest.mark.anyio
async def test_acp_login_reuses_verified_saved_session(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    _LoginClient.instances = []
    _LoginClient.restored = True
    _LoginClient.session_valid = True
    monkeypatch.setattr(acp_mod, "A0Client", _LoginClient)

    exit_code = await _login_for_acp(AcpOptions(host="http://agent.test"), CLIConfig())

    client = _LoginClient.instances[0]
    assert exit_code == 0
    assert client.login_calls == []
    assert client.persisted_hosts == []
    assert client.disconnected is True
    assert "already authenticated" in capsys.readouterr().out


@pytest.mark.anyio
async def test_acp_login_persists_only_verified_session(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    _LoginClient.instances = []
    _LoginClient.restored = False
    _LoginClient.session_valid = False
    _LoginClient.login_result = True
    monkeypatch.setattr(acp_mod, "A0Client", _LoginClient)
    monkeypatch.setenv("A0_USERNAME", "agent-zero-user")
    monkeypatch.setenv("A0_PASSWORD", "ephemeral-password")

    exit_code = await _login_for_acp(AcpOptions(host="http://agent.test"), CLIConfig())

    client = _LoginClient.instances[0]
    assert exit_code == 0
    assert client.login_calls == [("agent-zero-user", "ephemeral-password")]
    assert client.cleared_hosts == ["http://agent.test"]
    assert client.persisted_hosts == ["http://agent.test"]
    assert client.disconnected is True
    assert "login succeeded" in capsys.readouterr().out


@pytest.mark.anyio
async def test_acp_login_does_not_persist_failed_credentials(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _LoginClient.instances = []
    _LoginClient.restored = False
    _LoginClient.session_valid = False
    _LoginClient.login_result = False
    monkeypatch.setattr(acp_mod, "A0Client", _LoginClient)
    monkeypatch.setenv("A0_USERNAME", "agent-zero-user")
    monkeypatch.setenv("A0_PASSWORD", "wrong-password")

    exit_code = await _login_for_acp(AcpOptions(host="http://agent.test"), CLIConfig())

    client = _LoginClient.instances[0]
    assert exit_code == 1
    assert client.persisted_hosts == []
    assert client.disconnected is True
    assert "login failed" in capsys.readouterr().err
