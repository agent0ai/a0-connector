from __future__ import annotations

import asyncio
from io import StringIO
from unittest.mock import AsyncMock, Mock

import pytest
from rich.console import Console
from rich.text import Text
from textual.widgets import Input, OptionList, Static

from agent_zero_cli import chat_commands
from agent_zero_cli.app import AgentZeroCLI
from agent_zero_cli.config import CLIConfig
from agent_zero_cli.screens.chat_list import ChatListScreen, _SearchExcerpt


pytestmark = pytest.mark.anyio


@pytest.fixture
def chat_app(monkeypatch: pytest.MonkeyPatch) -> AgentZeroCLI:
    app = AgentZeroCLI(
        config=CLIConfig(instance_url="http://127.0.0.1:19999"),
        auto_connect_single_instance=False,
        discover_instances=False,
    )
    monkeypatch.setattr(app, "_startup", AsyncMock())
    monkeypatch.setattr(app, "_start_cli_update_check", lambda: None)
    monkeypatch.setattr(app, "_switch_context", AsyncMock())
    monkeypatch.setattr(
        app.client,
        "list_chats",
        AsyncMock(return_value=[
            {
                "id": f"chat-{index}", "name": f"Conversation {index}",
                "last_message": "Preview", "updated_at": 10000 - index,
            }
            for index in range(30)
        ]),
    )
    return app


async def test_repeated_f6_keeps_one_selector_and_selection_closes_it(chat_app: AgentZeroCLI) -> None:
    async with chat_app.run_test() as pilot:
        await pilot.press("f6", "f6", "f6")
        assert len(chat_app.screen_stack) == 2
        chat_app.client.list_chats.assert_awaited_once()

        await pilot.press("enter")
        assert len(chat_app.screen_stack) == 1
        chat_app._switch_context.assert_awaited_once()


async def test_repeated_f6_does_not_restart_pending_chat_request(chat_app: AgentZeroCLI) -> None:
    started = asyncio.Event()
    ready = asyncio.Event()
    contexts = chat_app.client.list_chats.return_value

    async def list_chats():
        started.set()
        await ready.wait()
        return contexts

    chat_app.client.list_chats.side_effect = list_chats
    async with chat_app.run_test() as pilot:
        await pilot.press("f6")
        await asyncio.wait_for(started.wait(), timeout=2)
        await pilot.press("f6")
        ready.set()
        await pilot.pause()
        chat_app.client.list_chats.assert_awaited_once()
        assert len(chat_app.screen_stack) == 2
        await pilot.press("escape")
        assert len(chat_app.screen_stack) == 1
        chat_app._switch_context.assert_not_awaited()


async def test_another_chat_command_keeps_the_active_selector_and_its_worker(
    chat_app: AgentZeroCLI,
) -> None:
    async with chat_app.run_test() as pilot:
        await pilot.press("f6")
        worker = chat_app.run_worker(chat_commands.cmd_chats(chat_app), exclusive=True)
        await worker.wait()
        assert len(chat_app.screen_stack) == 2
        await pilot.press("enter")
        assert len(chat_app.screen_stack) == 1
        chat_app._switch_context.assert_awaited_once()


@pytest.mark.parametrize("selection", ["enter", "click"])
async def test_selection_closes_before_chat_loading_finishes(
    chat_app: AgentZeroCLI, selection: str,
) -> None:
    loading = asyncio.Event()
    loaded = asyncio.Event()

    async def switch_context(*args, **kwargs):
        loading.set()
        await loaded.wait()

    chat_app._switch_context.side_effect = switch_context
    async with chat_app.run_test() as pilot:
        await pilot.press("f6")
        if selection == "enter":
            await pilot.press("down", "enter")
        else:
            await pilot.click("#chat-list", offset=(2, 4))
        await asyncio.wait_for(loading.wait(), timeout=2)
        assert len(chat_app.screen_stack) == 1
        chat_app._switch_context.assert_awaited_once_with("chat-1", has_messages_hint=True)
        loaded.set()


@pytest.mark.parametrize("size", [(100, 36), (45, 18)])
async def test_long_chat_list_navigates_without_mounting_per_chat_widgets(
    chat_app: AgentZeroCLI, size: tuple[int, int],
) -> None:
    chat_app.client.list_chats.return_value *= 10
    for index, context in enumerate(chat_app.client.list_chats.return_value):
        chat_app.client.list_chats.return_value[index] = {
            **context, "id": f"chat-{index}", "updated_at": 10000 - index,
        }

    async with chat_app.run_test(size=size) as pilot:
        await pilot.press("f6")
        screen = chat_app.screen
        assert isinstance(screen, ChatListScreen)
        options = screen.query_one("#chat-list", OptionList)
        assert options.option_count == 300
        assert len(list(screen.walk_children())) < 10
        await pilot.press(*(["down"] * 20), *(["up"] * 5))
        assert options.highlighted == 15
        assert options.scroll_y > 0
        await pilot.press("enter")
        assert len(chat_app.screen_stack) == 1
        chat_app._switch_context.assert_awaited_once_with("chat-15", has_messages_hint=True)


async def test_search_matches_messages_and_titles_in_original_order_and_clears(chat_app, monkeypatch):
    called = asyncio.Event()

    async def search(query):
        assert query == "Conversation 2"
        called.set()
        return [
            {"id": "outside", "parts": [{"text": "outside", "match": True}]},
            {"id": "chat-1", "parts": [{"text": "Literal [/path] ", "match": False}, {"text": query, "match": True}]},
        ]

    monkeypatch.setattr(chat_app.client, "search_chats", search)
    async with chat_app.run_test() as pilot:
        await pilot.press("f6")
        screen = chat_app.screen
        field = screen.query_one("#chat-list-search", Input)
        assert field.has_focus and field.placeholder == "Search" and field.max_length == 200
        field.value = "Conversation 2"
        await asyncio.wait_for(called.wait(), 2)
        await screen._search_worker.wait()
        await pilot.pause()
        options = screen.query_one("#chat-list", OptionList)
        assert list(screen._item_contexts.values()) == ["chat-1", "chat-2", *[f"chat-{i}" for i in range(20, 30)]]
        preview = options.get_option_at_index(0).prompt.renderables[1].text
        assert "Literal [/path] Conversation 2" in preview.plain
        assert any(span.style == "bold not dim" for span in preview.spans)
        assert options.get_option_at_index(1).prompt.plain.count("Conversation 2") == 1
        await pilot.press("escape")
        assert field.value == "" and options.option_count == 30
        await pilot.press("tab", "ctrl+f")
        assert field.has_focus
        await pilot.press("escape")
        assert len(chat_app.screen_stack) == 1


@pytest.mark.parametrize("fail", [False, True])
async def test_search_ignores_cancelled_results_and_errors_after_clear(chat_app, monkeypatch, fail):
    called = asyncio.Event()

    async def search(query):
        called.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            if fail:
                raise RuntimeError("stale failure")
            return [{"id": "chat-1", "parts": []}]

    monkeypatch.setattr(chat_app.client, "search_chats", search)
    async with chat_app.run_test() as pilot:
        await pilot.press("f6")
        screen = chat_app.screen
        notice = Mock()
        monkeypatch.setattr(screen, "notify", notice)
        screen.query_one(Input).value = "old"
        await asyncio.wait_for(called.wait(), 2)
        await pilot.press("enter")
        assert len(chat_app.screen_stack) == 2
        await pilot.press("escape")
        assert screen.query_one(OptionList).option_count == 30
        notice.assert_not_called()
        chat_app._switch_context.assert_not_awaited()


async def test_search_empty_results_error_recovery_and_message_selection(chat_app, monkeypatch):
    called = asyncio.Queue()

    async def search(query):
        called.put_nowait(query)
        if query == "fail":
            raise RuntimeError("Connection unavailable")
        return [{"id": "chat-5", "parts": [{"text": "needle", "match": True}]}] if query == "needle" else []

    monkeypatch.setattr(chat_app.client, "search_chats", search)
    async with chat_app.run_test() as pilot:
        await pilot.press("f6")
        screen = chat_app.screen
        for query in ["nothing", "fail", "needle"]:
            screen.query_one(Input).value = query
            assert await asyncio.wait_for(called.get(), 2) == query
            await screen._search_worker.wait()
            await pilot.pause()
            if query == "nothing":
                assert screen.query_one("#chat-list-empty", Static).display
                await pilot.press("enter")
                assert len(chat_app.screen_stack) == 2
            elif query == "fail":
                assert "Search failed" in str(screen.query_one("#chat-list-description", Static).render())
        await pilot.press("enter")
        chat_app._switch_context.assert_awaited_once_with("chat-5", has_messages_hint=True)


@pytest.mark.parametrize("size", [(100, 36), (45, 18), (32, 12)])
async def test_search_and_results_fit_short_terminals(chat_app, size):
    async with chat_app.run_test(size=size) as pilot:
        await pilot.press("f6")
        field = chat_app.screen.query_one(Input)
        options = chat_app.screen.query_one(OptionList)
        assert field.region.bottom <= options.region.y
        assert options.region.bottom <= size[1]
        assert options.size.height >= 2


@pytest.mark.parametrize("width", [28, 60, 120])
def test_search_excerpt_reflows_to_two_lines_without_parsing_markup(width):
    output = StringIO()
    console = Console(width=width, file=output)
    console.print(_SearchExcerpt(Text("Literal [/path] needle " + "context " * 30)))
    lines = output.getvalue().splitlines()
    assert len(lines) == 2
    assert "[/path] needle" in lines[0]
    assert lines[-1].endswith("…")
