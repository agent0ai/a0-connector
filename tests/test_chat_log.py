from __future__ import annotations

import asyncio
from unittest.mock import Mock

import pytest
from rich.panel import Panel
from textual.app import App, ComposeResult
from textual.geometry import Size

from agent_zero_cli.widgets import chat_log
from agent_zero_cli.widgets.chat_log import ChatLog, SelectableStatic, StatusEntry


class TranscriptApp(App[None]):
    CSS_PATH = "../src/agent_zero_cli/styles/app.tcss"

    def compose(self) -> ComposeResult:
        yield ChatLog(id="chat-log")


async def after_refresh(log: ChatLog) -> None:
    done = asyncio.get_running_loop().create_future()
    log.call_after_refresh(done.set_result, None)
    await asyncio.wait_for(done, timeout=5)


def test_transcript_measurement_uses_supplied_width_without_querying_geometry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_geometry(widget):
        pytest.fail("Measuring a transcript entry must not query the compositor")

    monkeypatch.setattr(SelectableStatic, "region", property(unexpected_geometry))
    widget = SelectableStatic(Panel("A wrapped message " * 10))
    container = Size(80, 24)

    wide_height = widget.get_content_height(container, container, 79)
    wide_content = widget.render()
    assert widget.render() is wide_content
    assert max(map(len, wide_content.plain.splitlines())) == 79

    narrow_height = widget.get_content_height(container, container, 39)
    narrow_content = widget.render()
    assert narrow_height > wide_height
    assert narrow_content is not wide_content
    assert max(map(len, narrow_content.plain.splitlines())) == 39
    assert widget.get_content_height(container, container, 0) == 0


@pytest.mark.anyio
async def test_transcript_shimmer_preserves_details_without_rebuilding_or_relayout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = TranscriptApp()
    build_body = Mock(wraps=chat_log._build_status_body)
    monkeypatch.setattr(chat_log, "_build_status_body", build_body)

    async with app.run_test(size=(80, 24)) as pilot:
        log = app.query_one(ChatLog)
        log.set_active_status(1, "Thinking", "Working", {"thoughts": ["Keep this detail"]})
        await pilot.pause()
        widget = log._seq_to_widget[1].primary
        assert isinstance(widget, StatusEntry)
        widget.action_toggle()
        await pilot.pause()
        initial_text = widget.render().plain
        initial_height = log.virtual_size.height
        refresh = Mock(wraps=widget.refresh)
        monkeypatch.setattr(widget, "refresh", refresh)
        layouts: list[object] = []
        app.screen.screen_layout_refresh_signal.subscribe(log, layouts.append)

        for _ in range(3):
            log.advance_shimmer()
            await pilot.pause()

        assert build_body.call_count == 1
        assert refresh.call_count == 3
        assert all(call.kwargs["layout"] is False for call in refresh.call_args_list)
        assert layouts == []
        assert widget.render().plain == initial_text
        assert "Keep this detail" in widget.copy_text()
        assert log.virtual_size.height == initial_height

        log.set_active_status(1, "Thinking", "Updated", {"thoughts": ["New detail"]})
        await pilot.pause()
        assert build_body.call_count == 2
        assert "New detail" in widget.copy_text()
        assert "Keep this detail" not in widget.copy_text()

        log.dim_active_status()
        await pilot.pause()
        assert not widget._active
        assert "New detail" in widget.copy_text()


@pytest.mark.anyio
async def test_transcript_batches_scroll_requests_and_respects_paused_follow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = TranscriptApp()

    async with app.run_test(size=(80, 24)) as pilot:
        log = app.query_one(ChatLog)
        scroll_end = Mock(wraps=log.scroll_end)
        monkeypatch.setattr(log, "scroll_end", scroll_end)
        for sequence in range(40):
            log.append_or_update(sequence, Panel(f"Message {sequence}"))
        await after_refresh(log)
        await pilot.pause()

        assert scroll_end.call_count == 1
        assert log.is_at_bottom()

        log.scroll_home(animate=False, immediate=True)
        log._auto_follow = False
        await pilot.pause()
        scroll_end.reset_mock()
        log.append_or_update(41, Panel("New message while reading history"))
        await pilot.pause()
        assert scroll_end.call_count == 0
        assert log.scroll_target_y == 0

        log._auto_follow = True
        log._schedule_scroll_end()
        log._auto_follow = False
        await after_refresh(log)
        await pilot.pause()
        assert scroll_end.call_count == 0

        log.clear()
        log.append_or_update(50, Panel("New chat"))
        await after_refresh(log)
        await pilot.pause()
        assert scroll_end.call_count == 1


@pytest.mark.anyio
async def test_transcript_rewraps_on_resize_without_clipping_panel_borders() -> None:
    app = TranscriptApp()

    async with app.run_test(size=(100, 30)) as pilot:
        log = app.query_one(ChatLog)
        for sequence in range(20):
            log.append_or_update(sequence, Panel("A long message that wraps " * 8))
        await pilot.pause()
        widget = log._seq_to_widget[19].primary
        assert isinstance(widget, SelectableStatic)
        wide_height = widget.size.height

        for width in (48, 100):
            await pilot.resize_terminal(width, 30)
            await pilot.pause()
            lines = widget.render().plain.splitlines()
            assert max(map(len, lines)) == width - 1
            assert lines[0].endswith("╮")
            assert lines[-1].endswith("╯")
            if width == 48:
                assert widget.size.height > wide_height
            else:
                assert widget.size.height == wide_height
