from __future__ import annotations

import base64

import acp
import pytest

from agent_zero_cli.acp import AcpOptions, AgentZeroACPAgent, _container_command, _prompt_parts
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


class _FakeConnector:
    agent_active = False

    def __init__(self, state, profile_result=None):
        self.state = state
        self.calls = []
        self.profile_result = profile_result
        self.client = self

    async def send_message(self, text, attachments):
        self.calls.append(("send", text))
        self.state.completed.set()

    async def set_agent_profile(self, context_id, profile):
        self.calls.append(("profile", context_id, profile))
        if self.profile_result is not None:
            return self.profile_result
        return {"ok": True}


def _directive_agent(session_id="s1", profile_result=None):
    from agent_zero_cli.acp import _SessionState

    agent = AgentZeroACPAgent(AcpOptions(host="http://agent.test"), CLIConfig())
    state = _SessionState(connector=None, config={})
    state.connector = _FakeConnector(state, profile_result=profile_result)
    agent._sessions[session_id] = state
    return agent, state


@pytest.mark.anyio
async def test_acp_session_directive_sets_profile_and_strips_line() -> None:
    agent, state = _directive_agent()

    response = await agent.prompt([_Text("# a0:profile=researcher\nsummarize the repo")], "s1")

    assert state.connector.calls == [("profile", "s1", "researcher"), ("send", "summarize the repo")]
    assert state.directive_profile == "researcher"
    assert response.stop_reason == "end_turn"


@pytest.mark.anyio
async def test_acp_session_directive_dedups_and_remaps() -> None:
    agent, state = _directive_agent()

    await agent.prompt([_Text("# a0:profile=researcher\nfirst")], "s1")
    await agent.prompt([_Text("# a0:profile=researcher\nsecond")], "s1")
    await agent.prompt([_Text("# a0:profile=coder\nthird")], "s1")

    profile_calls = [call for call in state.connector.calls if call[0] == "profile"]
    assert profile_calls == [("profile", "s1", "researcher"), ("profile", "s1", "coder")]
    assert [call for call in state.connector.calls if call[0] == "send"] == [("send", "first"), ("send", "second"), ("send", "third")]


@pytest.mark.anyio
async def test_acp_session_directive_unknown_directive_left_in_place() -> None:
    agent, state = _directive_agent()

    await agent.prompt([_Text("# a0:unknown=1\nhello")], "s1")

    assert [call for call in state.connector.calls if call[0] == "profile"] == []
    assert state.connector.calls == [("send", "# a0:unknown=1\nhello")]


@pytest.mark.anyio
async def test_acp_session_directive_rejected_profile_retries() -> None:
    agent, state = _directive_agent(profile_result={"ok": False, "message": "busy"})

    await agent.prompt([_Text("# a0:profile=researcher\nfirst")], "s1")
    await agent.prompt([_Text("# a0:profile=researcher\nsecond")], "s1")

    profile_calls = [call for call in state.connector.calls if call[0] == "profile"]
    assert profile_calls == [("profile", "s1", "researcher"), ("profile", "s1", "researcher")]
    assert state.directive_profile == ""
    assert [call for call in state.connector.calls if call[0] == "send"] == [("send", "first"), ("send", "second")]


@pytest.mark.anyio
async def test_acp_session_directive_only_prompt_ends_turn() -> None:
    agent, state = _directive_agent()

    response = await agent.prompt([_Text("# a0:profile=researcher")], "s1")

    assert state.connector.calls == [("profile", "s1", "researcher")]
    assert response.stop_reason == "end_turn"
