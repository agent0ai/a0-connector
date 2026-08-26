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
