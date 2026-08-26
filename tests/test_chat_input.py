from __future__ import annotations

import pytest
from textual import events
from textual.app import App, ComposeResult

from agent_zero_cli.widgets import ChatInput


pytestmark = pytest.mark.anyio


class ChatInputHarness(App[None]):
    CSS = """
    #message-input {
        width: 40;
    }
    """

    def compose(self) -> ComposeResult:
        yield ChatInput(id="message-input")


async def test_chat_input_ctrl_j_inserts_newline_and_grows() -> None:
    input_widget = ChatInput()
    input_widget.value = "one"

    await input_widget._on_key(events.Key("ctrl+j", None))
    input_widget.insert("two")
    input_widget._update_height()

    assert input_widget.value == "one\ntwo"
    assert input_widget.styles.height.cells == 4


async def test_chat_input_soft_wrapped_text_grows_to_four_rows() -> None:
    app = ChatInputHarness()

    async with app.run_test(size=(80, 20)) as pilot:
        input_widget = app.query_one("#message-input", ChatInput)
        await pilot.pause()

        input_widget.value = (
            "This is a long draft typed into the Agent Zero CLI composer to verify "
            "whether soft wrapped text makes the input box grow to three or four "
            "visible rows instead of staying constrained to a single line."
        )
        await pilot.pause()

        assert input_widget.wrapped_document.height > 4
        assert input_widget.styles.height.cells == 6


async def test_chat_input_shift_enter_inserts_newline_and_grows() -> None:
    input_widget = ChatInput()
    input_widget.value = "one"

    await input_widget._on_key(events.Key("shift+enter", None))
    input_widget.insert("two")
    input_widget._update_height()

    assert input_widget.value == "one\ntwo"
    assert input_widget.styles.height.cells == 4


async def test_chat_input_ctrl_a_selects_all_composer_text() -> None:
    app = ChatInputHarness()

    async with app.run_test(size=(80, 20)) as pilot:
        input_widget = app.query_one("#message-input", ChatInput)
        input_widget.value = "Select this entire\ncomposer draft"
        input_widget.focus()

        await pilot.press("ctrl+a")

        assert input_widget.selection.start == (0, 0)
        assert input_widget.selection.end == (1, len("composer draft"))


def test_chat_input_replaces_reference_trigger_and_keeps_following_text() -> None:
    input_widget = ChatInput()
    input_widget.value = "Compare @src/app with this"

    result = input_widget.replace("@[./src/app.py]", (0, 8), (0, 16))
    input_widget.move_cursor(result.end_location)

    assert input_widget.value == "Compare @[./src/app.py] with this"
    assert input_widget.selection.start == result.end_location


async def test_chat_input_history_recalls_at_text_boundaries() -> None:
    input_widget = ChatInput()
    input_widget.set_history_context("ctx-1")
    input_widget._push_history("first")
    input_widget._push_history("second")
    input_widget.value = "draft"
    input_widget.move_cursor((0, 0))

    await input_widget._on_key(events.Key("up", None))
    assert input_widget.value == "second"
    assert input_widget.selection.start == (0, 0)
    assert input_widget.selection.end == (0, 0)

    await input_widget._on_key(events.Key("up", None))
    assert input_widget.value == "first"

    input_widget.move_cursor(input_widget._document_end())
    await input_widget._on_key(events.Key("down", None))
    assert input_widget.value == "second"
    assert input_widget.selection.start == input_widget._document_end()
    assert input_widget.selection.end == input_widget._document_end()

    await input_widget._on_key(events.Key("down", None))
    assert input_widget.value == "draft"


def test_chat_input_history_leaves_multiline_navigation_alone() -> None:
    input_widget = ChatInput()
    input_widget._push_history("older")
    input_widget.value = "one\ntwo"

    input_widget.move_cursor((0, 1))
    assert input_widget._history_previous() is False

    input_widget.move_cursor((1, 0))
    assert input_widget._history_next() is False


def test_chat_input_history_is_scoped_by_context() -> None:
    input_widget = ChatInput()

    input_widget.set_history_context("ctx-1")
    input_widget.seed_history(["from one"])
    input_widget.set_history_context("ctx-2")
    input_widget.seed_history(["from two"])

    input_widget.value = ""
    input_widget.move_cursor((0, 0))
    assert input_widget._history_previous() is True
    assert input_widget.value == "from two"

    input_widget.set_history_context("ctx-1")
    input_widget.value = ""
    input_widget.move_cursor((0, 0))
    assert input_widget._history_previous() is True
    assert input_widget.value == "from one"


def test_chat_input_queue_placeholder_overrides_progress_when_empty() -> None:
    input_widget = ChatInput()

    input_widget.set_activity("Thinking")
    input_widget.set_queue_active(True)

    assert input_widget.placeholder == "Press Enter to send queued messages"

    input_widget.set_queue_active(False)
    assert input_widget.placeholder == "|>  Thinking"
