from __future__ import annotations

from pathlib import Path
from typing import Literal

import pytest
from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.widgets import Static

from agent_zero_cli.screens.chat_list import ChatListScreen
from agent_zero_cli.screens.installed_plugins import InstalledPluginsScreen
from agent_zero_cli.screens.model_presets import ModelPresetsScreen


pytestmark = pytest.mark.anyio

PopoverKind = Literal["chat", "plugins", "model-presets"]
APP_CSS_PATH = Path(__file__).resolve().parents[1] / "src/agent_zero_cli/styles/app.tcss"


class PopoverBackdropHarness(App[None]):
    CSS_PATH = APP_CSS_PATH

    def __init__(self, kind: PopoverKind) -> None:
        super().__init__()
        self.kind = kind

    def compose(self) -> ComposeResult:
        with Vertical(id="background-root"):
            for index in range(30):
                yield Static(f"background-marker-{self.kind}-{index:02d}")

    async def on_mount(self) -> None:
        if self.kind == "chat":
            self.push_screen(
                ChatListScreen(
                    [
                        {
                            "id": "ctx-alpha",
                            "name": "Architecture",
                            "last_message": "Keep the base screen visible.",
                            "created_at": "2026-06-22T10:00:00",
                        }
                    ]
                )
            )
            return

        if self.kind == "plugins":
            self.push_screen(
                InstalledPluginsScreen(
                    [
                        {
                            "name": "_documents",
                            "display_name": "Documents",
                            "description": "Document tools",
                            "enabled": True,
                            "toggleable": True,
                        }
                    ]
                )
            )
            return

        self.push_screen(
            ModelPresetsScreen(
                [{"name": "deep", "label": "Deep", "description": "Reason carefully."}],
                current_preset="deep",
            )
        )


@pytest.mark.parametrize("kind", ["chat", "plugins", "model-presets"])
async def test_popover_style_screens_leave_background_visible(kind: PopoverKind) -> None:
    app = PopoverBackdropHarness(kind)

    async with app.run_test(size=(100, 36)) as pilot:
        await pilot.pause(0.2)
        screenshot = app.export_screenshot()

    assert f"background-marker-{kind}-25" in screenshot


async def test_chat_list_renders_bracketed_path_text_without_markup_error() -> None:
    """Server-provided chat names/previews containing [/path/like/text] must not
    be parsed as Rich/Textual markup closing tags (MarkupError crash)."""
    bracketed = "result: [/a0/usr/chats/Vo04TFdu/chat.json] listed"

    class BracketedHarness(App[None]):
        CSS_PATH = APP_CSS_PATH

        def compose(self) -> ComposeResult:
            yield Static("root")

        async def on_mount(self) -> None:
            self.push_screen(
                ChatListScreen(
                    [
                        {
                            "id": "ctx-bracket",
                            "name": "Debug [/a0/usr/chats/Vo04TFdu/chat.json]",
                            "last_message": bracketed,
                            "created_at": "2026-07-24T10:00:00",
                        }
                    ]
                )
            )

    app = BracketedHarness()
    async with app.run_test(size=(100, 36)) as pilot:
        await pilot.pause(0.2)
        screenshot = app.export_screenshot()

    assert "chat.json" in screenshot
