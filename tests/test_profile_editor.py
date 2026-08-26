from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Static

from agent_zero_cli.screens.profile_editor import ProfileEditorResult, ProfileEditorScreen


pytestmark = pytest.mark.anyio


class ProfileEditorHarness(App[None]):
    def __init__(self) -> None:
        super().__init__()
        self.result: ProfileEditorResult | None | str = "pending"

    def compose(self) -> ComposeResult:
        yield Static("base")

    async def on_mount(self) -> None:
        self.push_screen(
            ProfileEditorScreen(
                creating=True,
                tools=[
                    {"id": "local:browser", "label": "Browser", "description": "Browse pages"},
                    {"id": "local:memory", "label": "Memory", "description": "Load memory"},
                ],
                allowed_tools=["local:browser"],
            ),
            self._capture,
        )

    def _capture(self, result: ProfileEditorResult | None) -> None:
        self.result = result


async def test_profile_editor_collects_details_and_tool_choices() -> None:
    app = ProfileEditorHarness()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        screen = app.screen
        screen.query_one("#profile-editor-name").value = "Source Scout"
        screen.query_one("#profile-editor-instructions").text = "Verify every claim."
        screen.action_next()
        screen.query_one("#profile-editor-tool-list").select("local:memory")
        screen.action_save()
        await pilot.pause()

    assert app.result == ProfileEditorResult(
        title="Source Scout",
        instructions="Verify every claim.",
        allowed_tools=("local:browser", "local:memory"),
    )
