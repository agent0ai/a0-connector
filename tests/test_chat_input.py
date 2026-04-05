from __future__ import annotations

import pytest
from textual.app import App, ComposeResult

from agent_zero_cli.widgets.chat_input import ChatInput


pytestmark = pytest.mark.anyio


class ChatInputHarness(App[None]):
    def __init__(self) -> None:
        super().__init__()
        self.value_changes: list[str] = []
        self.submissions: list[str] = []

    def compose(self) -> ComposeResult:
        yield ChatInput(id="message-input")

    def on_mount(self) -> None:
        self.query_one("#message-input", ChatInput).focus()

    def on_chat_input_value_changed(self, event: ChatInput.ValueChanged) -> None:
        self.value_changes.append(event.value)

    def on_chat_input_submitted(self, event: ChatInput.Submitted) -> None:
        self.submissions.append(event.value)


async def test_live_typing_posts_chat_input_value_changed() -> None:
    app = ChatInputHarness()

    async with app.run_test() as pilot:
        await pilot.press("h")
        await pilot.press("i")

        input_widget = app.query_one("#message-input", ChatInput)
        assert input_widget.text == "hi"
        assert app.value_changes[-1] == "hi"


async def test_enter_submits_and_clears_real_chat_input() -> None:
    app = ChatInputHarness()

    async with app.run_test() as pilot:
        await pilot.press("h")
        await pilot.press("i")
        await pilot.press("enter")

        input_widget = app.query_one("#message-input", ChatInput)
        assert app.submissions == ["hi"]
        assert input_widget.text == ""
