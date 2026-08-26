from __future__ import annotations

from typing import Any

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Static

from agent_zero_cli import permissions_commands, profile_commands
from agent_zero_cli.screens.permissions import PermissionsResult, PermissionsScreen
from tests.test_app import DummyAgentZeroCLI


pytestmark = pytest.mark.anyio


def test_permissions_command_is_a_local_connector_command() -> None:
    app = DummyAgentZeroCLI()
    spec = next(
        item for item in app._command_registry if item.canonical_name == "/permissions"
    )

    assert spec.description == (
        "Edit Tools, MCP, and Skill permissions for the current agent."
    )


def _state(*, tool_default: str = "block") -> dict[str, Any]:
    return {
        "profile": {
            "id": "developer",
            "metadata": {"title": {"effective": "Developer"}},
        },
        "tools": {
            "policy": {"mode": "inherit"},
            "effective_policy": {
                "mode": "custom",
                "default": tool_default,
                "mcp_default": "allow",
                "allowed": [],
                "blocked": [],
            },
            "has_override": False,
            "catalog": [
                {
                    "id": "local:browser",
                    "label": "Browser",
                    "description": "Browse the web.",
                    "available": True,
                },
                {
                    "id": "mcp:docs:read",
                    "label": "Read docs",
                    "description": "Read documentation.",
                    "available": True,
                },
            ],
        },
        "skills": {
            "policy": {"mode": "inherit"},
            "effective_policy": {
                "mode": "inherit",
                "default": "allow",
                "allowed": [],
                "blocked": [],
            },
            "has_override": False,
            "catalog": [
                {
                    "name": "Research",
                    "description": "Research carefully.",
                    "available": True,
                }
            ],
        },
    }


class PermissionsHarness(App[None]):
    def __init__(self, state: dict[str, Any]) -> None:
        super().__init__()
        self.permissions = PermissionsScreen(state)

    def compose(self) -> ComposeResult:
        yield Static("base")

    def on_mount(self) -> None:
        self.push_screen(self.permissions)


async def test_permissions_screen_cycles_default_on_off_and_back_to_inherit() -> None:
    app = PermissionsHarness(_state())

    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause(0.2)
        await pilot.press("space")
        await pilot.pause(0.2)
        assert app.permissions._tool_policy == {
            "mode": "custom",
            "default": "block",
            "mcp_default": "allow",
            "allowed": ["local:browser"],
            "blocked": [],
        }

        await pilot.press("space")
        await pilot.pause(0.2)
        assert app.permissions._tool_policy["blocked"] == ["local:browser"]

        await pilot.press("space")
        await pilot.pause(0.2)
        assert app.permissions._tool_policy["mode"] == "inherit"


async def test_permissions_screen_keeps_tool_and_mcp_defaults_independent() -> None:
    app = PermissionsHarness(_state(tool_default="allow"))

    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause(0.2)
        await pilot.click("#permissions-default")
        await pilot.pause(0.2)
        assert app.permissions._tool_policy["default"] == "block"
        assert app.permissions._tool_policy["mcp_default"] == "allow"

        await pilot.click("#permissions-category-mcps")
        await pilot.pause(0.2)
        await pilot.click("#permissions-default")
        await pilot.pause(0.2)
        assert app.permissions._tool_policy["default"] == "block"
        assert app.permissions._tool_policy["mcp_default"] == "block"


async def test_permissions_screen_keeps_keyboard_buttons_and_rows_operable() -> None:
    app = PermissionsHarness(_state())

    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause(0.2)
        app.permissions.query_one("#permissions-category-mcps").focus()
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert app.permissions._category == "mcps"
        assert app.permissions.query_one("#permissions-category-mcps").has_class(
            "is-active"
        )
        assert not app.permissions.query_one("#permissions-category-tools").has_class(
            "is-active"
        )

        app.permissions.query_one("#permissions-list").focus()
        await pilot.press("r", "e", "a", "d")
        await pilot.pause(0.2)
        assert app.permissions.query_one("#permissions-search").value == "read"

        app.permissions.query_one("#permissions-list").focus()
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert app.permissions._tool_policy["allowed"] == ["mcp:docs:read"]


async def test_permissions_command_saves_only_changed_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = DummyAgentZeroCLI()
    app.connected = True
    app.current_context = "ctx-1"
    calls: list[tuple[str, dict[str, object]]] = []
    notices: list[tuple[str, bool]] = []

    async def fake_menu_state(*args, **kwargs):
        del args, kwargs
        return "developer", [{"key": "developer", "label": "Developer"}]

    async def fake_load_state(*args, **kwargs):
        del args, kwargs
        return _state()

    async def fake_push_screen(_screen):
        return PermissionsResult(
            tool_policy={
                "mode": "custom",
                "default": "block",
                "mcp_default": "allow",
                "allowed": ["local:browser"],
                "blocked": [],
            },
            skill_policy={"mode": "inherit"},
            tool_changed=True,
            skill_changed=False,
        )

    async def fake_agent_editor(action: str, **payload: object):
        calls.append((action, payload))
        return {"ok": True}

    monkeypatch.setattr(profile_commands, "load_profile_menu_state", fake_menu_state)
    monkeypatch.setattr(profile_commands, "_load_editor_state", fake_load_state)
    monkeypatch.setattr(app, "push_screen_wait", fake_push_screen)
    monkeypatch.setattr(app.client, "agent_editor", fake_agent_editor)
    monkeypatch.setattr(
        app,
        "_show_notice",
        lambda message, *, error=False: notices.append((message, error)),
    )

    await permissions_commands.cmd_permissions(app)

    assert calls == [
        (
            "save",
            {
                "context_id": "ctx-1",
                "patch": {
                    "profile_id": "developer",
                    "creating": False,
                    "editor_mode": "easy",
                    "tool_policy": {
                        "mode": "custom",
                        "default": "block",
                        "mcp_default": "allow",
                        "allowed": ["local:browser"],
                        "blocked": [],
                    },
                },
            },
        )
    ]
    assert notices == [("Saved permissions for Developer.", False)]


async def test_permissions_command_refuses_internal_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = DummyAgentZeroCLI()
    notices: list[tuple[str, bool]] = []

    async def fake_menu_state(*args, **kwargs):
        del args, kwargs
        return "default", []

    monkeypatch.setattr(profile_commands, "load_profile_menu_state", fake_menu_state)
    monkeypatch.setattr(
        app,
        "_show_notice",
        lambda message, *, error=False: notices.append((message, error)),
    )

    await permissions_commands.cmd_permissions(app)

    assert notices == [
        ("The Default utility profile has no editable permissions.", True)
    ]
