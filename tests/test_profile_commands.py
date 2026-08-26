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


def test_profile_query_preserves_an_existing_multiword_label() -> None:
    assert profile_commands._exact_profile_selection(
        [{"key": "tiny-local", "label": "Tiny Local"}],
        "Tiny Local",
    ) == {"key": "tiny-local", "label": "Tiny Local"}


def test_custom_tool_policy_uses_the_smaller_exception_list() -> None:
    assert profile_commands._tool_policy(
        ["local:a", "local:b", "local:c"],
        ["local:a", "local:b"],
    ) == {
        "mode": "custom",
        "default": "allow",
        "allowed": [],
        "blocked": ["local:c"],
    }


async def test_apply_profile_selection_sets_current_context_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = DummyAgentZeroCLI()
    app.connected = True
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


async def test_profile_command_quick_create_activates_a_fresh_profile_chat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = DummyAgentZeroCLI()
    app.connected = True
    app.current_context = "ctx-1"
    app.current_project = {"name": "Demo", "title": "Demo"}
    app.connector_features = {"settings_get", "agent_profile_set", "agent_editor", "chat_get"}
    editor_calls: list[tuple[str, dict[str, object]]] = []
    chat_calls: list[dict[str, object]] = []
    profile_calls: list[tuple[str, str]] = []
    switched: list[tuple[str, bool]] = []
    notices: list[tuple[str, bool]] = []

    async def fake_get_settings() -> dict[str, object]:
        return {
            "settings": {"agent_profile": "default"},
            "additional": {"agent_subdirs": [{"value": "default", "label": "Default"}]},
        }

    async def fake_get_chat(_context_id: str) -> dict[str, object]:
        return {"agent_profile": "default"}

    async def fake_agent_editor(action: str, **payload: object) -> dict[str, object]:
        editor_calls.append((action, payload))
        return {"ok": True, "profile_id": "source-scout"}

    async def fake_create_chat(**payload: object) -> str:
        chat_calls.append(payload)
        return "ctx-2"

    async def fake_set_agent_profile(context_id: str, profile_id: str) -> dict[str, object]:
        profile_calls.append((context_id, profile_id))
        return {"ok": True, "agent_profile": profile_id}

    async def fake_switch(context_id: str, *, has_messages_hint: bool) -> None:
        switched.append((context_id, has_messages_hint))

    monkeypatch.setattr(app.client, "get_settings", fake_get_settings)
    monkeypatch.setattr(app.client, "get_chat", fake_get_chat)
    monkeypatch.setattr(app.client, "agent_editor", fake_agent_editor)
    monkeypatch.setattr(app.client, "create_chat", fake_create_chat)
    monkeypatch.setattr(app.client, "set_agent_profile", fake_set_agent_profile)
    monkeypatch.setattr(app, "_switch_context", fake_switch)
    monkeypatch.setattr(app, "_show_notice", lambda message, *, error=False: notices.append((message, error)))

    await profile_commands.cmd_profile(app, query='"Source Scout" "Verify every claim"')

    assert editor_calls == [
        (
            "quick_create",
            {
                "context_id": "ctx-1",
                "title": "Source Scout",
                "instructions": "Verify every claim",
            },
        )
    ]
    assert chat_calls == [
        {
            "current_context_id": "ctx-1",
            "project_name": "Demo",
        }
    ]
    assert profile_calls == [("ctx-2", "source-scout")]
    assert switched == [("ctx-2", False)]
    assert notices == [("Created and activated Source Scout.", False)]
