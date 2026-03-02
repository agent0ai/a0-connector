import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from textual.app import App
from textual.widgets import ListView

REPO_ROOT = Path(__file__).resolve().parents[2]
CLI_SRC = REPO_ROOT / "cli" / "src"
if CLI_SRC.as_posix() not in sys.path:
    sys.path.insert(0, CLI_SRC.as_posix())

from agent_zero_cli.app import AgentZeroCLI
from agent_zero_cli.config import CLIConfig
from agent_zero_cli.screens.chat_list import ChatListScreen


class FakeRichLog:
    def __init__(self) -> None:
        self.writes: list[object] = []
        self.cleared = False

    def write(self, message: object) -> None:
        self.writes.append(message)

    def clear(self) -> None:
        self.cleared = True


class FakeInput:
    def __init__(self) -> None:
        self.disabled = False


class DummyAgentZeroCLI(AgentZeroCLI):
    def __init__(self) -> None:
        super().__init__(config=CLIConfig(instance_url="http://example.test"))
        self.rendered_entries: list[dict] = []

    def _render_log_entry(self, log: FakeRichLog, entry: dict) -> None:
        self.rendered_entries.append(entry)


@pytest.fixture
def dummy_app() -> DummyAgentZeroCLI:
    app = DummyAgentZeroCLI()
    log = FakeRichLog()
    input_widget = FakeInput()
    app.query_one = lambda selector, cls=None: log if selector == "#chat-log" else input_widget
    app._test_log = log
    return app


def test_state_push_renders_entries_even_when_log_version_smaller(dummy_app: DummyAgentZeroCLI) -> None:
    payload = {
        "data": {
            "snapshot": {
                "logs": [
                    {"no": 10, "type": "info", "content": "first"},
                    {"no": 11, "type": "info", "content": "second"},
                ],
                "log_version": 2,
                "log_progress_active": True,
            }
        }
    }

    dummy_app._handle_state_push_ui(payload)

    assert [entry["no"] for entry in dummy_app.rendered_entries] == [10, 11]


def test_log_guid_reset_clears_log_and_resets_cursor(dummy_app: DummyAgentZeroCLI) -> None:
    dummy_app.log_cursor = 7
    dummy_app.log_guid = "old-guid"

    payload = {"data": {"snapshot": {"log_guid": "new-guid", "logs": []}}}

    dummy_app._handle_state_push_ui(payload)

    assert dummy_app._test_log.cleared is True
    assert dummy_app.log_cursor == 0


class CapturingChatListScreen(ChatListScreen):
    def __init__(self, contexts: list[dict]) -> None:
        super().__init__(contexts)
        self.dismissed: str | None = None

    def dismiss(self, result: str | None = None) -> None:  # type: ignore[override]
        self.dismissed = result


class ChatListTestApp(App[None]):
    def __init__(self, screen: ChatListScreen) -> None:
        super().__init__()
        self._screen = screen

    async def on_mount(self) -> None:
        await self.push_screen(self._screen)


@pytest.mark.anyio
async def test_chat_list_uses_safe_ids_and_maps_back_to_context() -> None:
    contexts = [
        {"id": "ctx-1", "name": "One", "created_at": "2026-02-06", "last_message": "Hello"},
        {"id": "ctx-2", "name": "Two", "created_at": "2026-02-07", "last_message": "Hi"},
    ]
    screen = CapturingChatListScreen(contexts)
    app = ChatListTestApp(screen)

    async with app.run_test() as pilot:
        await pilot.pause()
        assert all(item_id.startswith("ctx-") for item_id in screen._item_contexts)
        first_item_id = next(iter(screen._item_contexts))

        screen.on_list_view_selected(SimpleNamespace(item=SimpleNamespace(id=first_item_id)))

        assert screen.dismissed == "ctx-1"

        list_view = screen.query_one(ListView)
        nodes = list(getattr(list_view, "_nodes", list_view.children))
        assert len(nodes) == len(contexts)
