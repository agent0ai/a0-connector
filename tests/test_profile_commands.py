from __future__ import annotations

import pytest

from agent_zero_cli import profile_commands
from tests.test_app import DummyAgentZeroCLI


pytestmark = pytest.mark.anyio


def test_profile_menu_state_uses_core_agent_subdirs() -> None:
    current_profile, options = profile_commands.profile_menu_state_from_settings(
        {
            "settings": {"agent_profile": "developer"},
            "additional": {
                "agent_subdirs": [
                    {"value": "agent0", "label": "Agent 0"},
                    {"value": "developer", "label": "Developer"},
                ]
            },
        }
    )

    assert current_profile == "developer"
    assert options == [
        {"key": "agent0", "label": "Agent 0"},
        {"key": "developer", "label": "Developer"},
    ]


def test_resolve_profile_selection_accepts_unique_prefix_and_label() -> None:
    option, error = profile_commands.resolve_profile_selection(
        [
            {"key": "agent0", "label": "Agent 0"},
            {"key": "developer", "label": "Developer"},
        ],
        "dev",
    )

    assert error is None
    assert option == {"key": "developer", "label": "Developer"}

    option, error = profile_commands.resolve_profile_selection(
        [
            {"key": "agent0", "label": "Agent 0"},
            {"key": "developer", "label": "Developer"},
        ],
        "agent 0",
    )

    assert error is None
    assert option == {"key": "agent0", "label": "Agent 0"}


async def test_apply_profile_selection_uses_partial_settings_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = DummyAgentZeroCLI()
    notices: list[tuple[str, bool]] = []
    calls: list[dict[str, str]] = []

    async def fake_set_settings(settings: dict[str, str]) -> dict[str, object]:
        calls.append(settings)
        return {
            "settings": {"agent_profile": "developer"},
            "additional": {
                "agent_subdirs": [
                    {"value": "agent0", "label": "Agent 0"},
                    {"value": "developer", "label": "Developer"},
                ]
            },
        }

    monkeypatch.setattr(app.client, "set_settings", fake_set_settings)
    monkeypatch.setattr(app, "_show_notice", lambda message, *, error=False: notices.append((message, error)))

    ok = await profile_commands.apply_profile_selection(
        app,
        "developer",
        options=[{"key": "developer", "label": "Developer"}],
    )

    assert ok is True
    assert calls == [{"agent_profile": "developer"}]
    assert notices == [("Agent profile set to Developer.", False)]
