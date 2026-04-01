from types import SimpleNamespace

import pytest
from textual.app import App
from textual.widgets import ListView

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
        self.focused = False

    def focus(self) -> None:
        self.focused = True


class DummyAgentZeroCLI(AgentZeroCLI):
    def __init__(self) -> None:
        super().__init__(
            config=CLIConfig(
                instance_url="http://example.test",
                api_key="dev-a0-connector",
            )
        )
        self.rendered_events: list[dict] = []

    def _render_connector_event(self, log: FakeRichLog, event: dict) -> None:
        self.rendered_events.append(event)


@pytest.fixture
def dummy_app() -> DummyAgentZeroCLI:
    app = DummyAgentZeroCLI()
    log = FakeRichLog()
    input_widget = FakeInput()
    app.query_one = lambda selector, cls=None: log if selector == "#chat-log" else input_widget
    app._test_log = log
    app._test_input = input_widget
    return app


def test_validate_capabilities_accepts_current_ws_contract(dummy_app: DummyAgentZeroCLI) -> None:
    dummy_app._validate_capabilities(
        {
            "protocol": "a0-connector.v1",
            "auth": ["api_key"],
            "websocket_namespace": "/ws",
            "websocket_handlers": ["plugins/a0_connector/ws_connector"],
        }
    )


def test_validate_capabilities_rejects_old_namespace(dummy_app: DummyAgentZeroCLI) -> None:
    with pytest.raises(ValueError, match="Unsupported WebSocket namespace"):
        dummy_app._validate_capabilities(
            {
                "protocol": "a0-connector.v1",
                "auth": ["api_key"],
                "websocket_namespace": "/connector",
                "websocket_handlers": ["plugins/a0_connector/ws_connector"],
            }
        )


def test_context_snapshot_renders_events_for_current_context(dummy_app: DummyAgentZeroCLI) -> None:
    dummy_app.current_context = "ctx-1"

    dummy_app._handle_context_snapshot(
        {
            "context_id": "ctx-1",
            "events": [
                {"event": "assistant_message", "data": {"text": "Hello"}},
                {"event": "status", "data": {"text": "Done"}},
            ],
        }
    )

    assert dummy_app.rendered_events == [
        {"event": "assistant_message", "data": {"text": "Hello"}},
        {"event": "status", "data": {"text": "Done"}},
    ]


def test_context_event_ignores_other_contexts(dummy_app: DummyAgentZeroCLI) -> None:
    dummy_app.current_context = "ctx-1"

    dummy_app._handle_context_event(
        {
            "context_id": "ctx-2",
            "event": "assistant_message",
            "data": {"text": "Ignored"},
        }
    )

    assert dummy_app.rendered_events == []
    assert dummy_app.agent_active is False
    assert dummy_app._test_input.disabled is False


def test_context_complete_reenables_input(dummy_app: DummyAgentZeroCLI) -> None:
    dummy_app.current_context = "ctx-1"
    dummy_app.agent_active = True
    dummy_app._test_input.disabled = True

    dummy_app._handle_context_complete({"context_id": "ctx-1"})

    assert dummy_app.agent_active is False
    assert dummy_app._test_input.disabled is False
    assert dummy_app._test_input.focused is True


def test_handle_file_op_returns_error_for_unknown_operation(dummy_app: DummyAgentZeroCLI) -> None:
    result = dummy_app._handle_file_op(
        {"op_id": "op-1", "op": "unknown", "path": "/tmp/example.txt"}
    )

    assert result == {
        "op_id": "op-1",
        "ok": False,
        "error": "Unknown op: unknown",
    }


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
