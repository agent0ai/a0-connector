from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from textual.widgets import OptionList

from agent_zero_cli import chat_commands
from agent_zero_cli.app import AgentZeroCLI
from agent_zero_cli.config import CLIConfig
from agent_zero_cli.screens.chat_list import ChatListScreen


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
