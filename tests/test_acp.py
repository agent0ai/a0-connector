from __future__ import annotations

import base64
from typing import Any

import acp
import pytest

from agent_zero_cli.acp import AcpOptions, AgentZeroACPAgent, _SessionState, _container_command, _prompt_parts
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


class _StubConnector:
    agent_active = False


class _RecordingConn:
    def __init__(self) -> None:
        self.chunks: list[str] = []

    async def session_update(self, session_id: str, update: Any) -> None:
        self.chunks.append(update.content.text)


@pytest.mark.anyio
async def test_acp_assistant_message_streams_only_deltas() -> None:
    """Cumulative assistant_message snapshots must reduce to append-only deltas."""
    agent = AgentZeroACPAgent(AcpOptions(host="http://agent.test"), CLIConfig())
    state = _SessionState(connector=_StubConnector(), config={})
    agent._sessions["ctx-1"] = state
    conn = _RecordingConn()
    agent._conn = conn

    snapshots = ["Hello", "Hello, w", "Hello, world", "Hello, world!"]
    for text in snapshots:
        await agent._handle_event({"context_id": "ctx-1", "event": "assistant_message", "data": {"text": text}})

    assert conn.chunks == ["Hello", ", w", "orld", "!"]
    # concatenated streamed deltas equal the final text exactly
    assert "".join(conn.chunks) == snapshots[-1]
    assert state.final_text == snapshots[-1]

    # a new turn resets the delta cursor
    state.begin_turn()
    assert state.sent_text == ""


@pytest.mark.anyio
async def test_acp_assistant_message_rewrite_separates_new_text() -> None:
    """A mid-turn snapshot rewrite keeps every byte instead of gluing copies."""
    agent = AgentZeroACPAgent(AcpOptions(host="http://agent.test"), CLIConfig())
    state = _SessionState(connector=_StubConnector(), config={})
    agent._sessions["ctx-2"] = state
    conn = _RecordingConn()
    agent._conn = conn

    await agent._handle_event({"context_id": "ctx-2", "event": "assistant_message", "data": {"text": "first draft"}})
    await agent._handle_event({"context_id": "ctx-2", "event": "assistant_message", "data": {"text": "rewritten answer"}})

    assert conn.chunks == ["first draft", "\nrewritten answer"]
    joined = "".join(conn.chunks)
    assert "first draft" in joined and "rewritten answer" in joined
