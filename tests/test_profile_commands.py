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


def test_profile_menu_state_can_use_current_chat_profile() -> None:
    current_profile, options = profile_commands.profile_menu_state_from_settings(
        {
            "settings": {"agent_profile": "agent0"},
            "additional": {
                "agent_subdirs": [
                    {"value": "agent0", "label": "Agent 0"},
                    {"value": "developer", "label": "Developer"},
                ]
            },
        },
        current_profile="developer",
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


async def test_apply_profile_selection_sets_current_context_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = DummyAgentZeroCLI()
    app.current_context = "ctx-1"
    notices: list[tuple[str, bool]] = []
    calls: list[tuple[str, str]] = []

    async def fake_set_agent_profile(context_id: str, profile_key: str) -> dict[str, object]:
        calls.append((context_id, profile_key))
        return {
            "ok": True,
            "agent_profile": "developer",
            "agent_profile_label": "Developer",
        }

    async def async_noop(*args, **kwargs) -> None:
        del args, kwargs

    monkeypatch.setattr(app.client, "set_agent_profile", fake_set_agent_profile)
    monkeypatch.setattr(app, "_show_notice", lambda message, *, error=False: notices.append((message, error)))
    monkeypatch.setattr(app, "_refresh_model_switcher", async_noop)
    monkeypatch.setattr(app, "_refresh_token_usage", async_noop)

    ok = await profile_commands.apply_profile_selection(
        app,
        "developer",
        options=[{"key": "developer", "label": "Developer"}],
    )

    assert ok is True
    assert calls == [("ctx-1", "developer")]
    assert notices == [("Agent profile set to Developer.", False)]
