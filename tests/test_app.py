from types import SimpleNamespace

import pytest
from textual.app import App
from textual.widgets import ListView

from agent_zero_cli.app import AgentZeroCLI
from agent_zero_cli.config import CLIConfig
from agent_zero_cli.screens.chat_list import ChatListScreen
from agent_zero_cli.screens.host_input import HostInputScreen
from agent_zero_cli.screens.login import LoginResult, LoginScreen


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
            "auth": ["api_key", "login"],
            "websocket_namespace": "/ws",
            "websocket_handlers": ["plugins/a0_connector/ws_connector"],
        }
    )


def test_default_client_host_matches_host_input_default() -> None:
    app = AgentZeroCLI(config=CLIConfig(instance_url="", api_key=""))

    assert HostInputScreen.DEFAULT_HOST == "http://127.0.0.1:5080"
    assert app.client.base_url == HostInputScreen.DEFAULT_HOST


@pytest.mark.asyncio
async def test_startup_falls_back_to_default_host_when_prompt_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = AgentZeroCLI(config=CLIConfig(instance_url="", api_key=""))
    log = FakeRichLog()
    input_widget = FakeInput()
    saved: dict[str, str] = {}

    app.query_one = lambda selector, cls=None: log if selector == "#chat-log" else input_widget

    async def fake_push_screen_wait(screen: object) -> str:
        return ""

    async def fake_fetch_capabilities(log_widget: FakeRichLog) -> tuple[None, bool]:
        return None, False

    monkeypatch.setattr(app, "push_screen_wait", fake_push_screen_wait)
    monkeypatch.setattr(app, "_fetch_capabilities", fake_fetch_capabilities)
    monkeypatch.setattr(
        "agent_zero_cli.app.save_env",
        lambda key, value: saved.setdefault(key, value),
    )

    await app._startup()

    assert app.config.instance_url == HostInputScreen.DEFAULT_HOST
    assert app.client.base_url == HostInputScreen.DEFAULT_HOST
    assert saved == {}


@pytest.mark.asyncio
async def test_startup_persists_host_and_api_key_only_when_login_save_checked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = AgentZeroCLI(config=CLIConfig(instance_url="", api_key=""))
    log = FakeRichLog()
    input_widget = FakeInput()
    saved: dict[str, str] = {}

    app.query_one = lambda selector, cls=None: log if selector == "#chat-log" else input_widget

    async def fake_push_screen_wait(screen: object) -> object:
        if isinstance(screen, HostInputScreen):
            return "http://example.test"
        if isinstance(screen, LoginScreen):
            return LoginResult(api_key="api-key-123", save_credentials=True)
        raise AssertionError(f"Unexpected screen: {screen!r}")

    async def fake_fetch_capabilities(log_widget: FakeRichLog) -> tuple[dict[str, object], bool]:
        return {
            "auth": ["api_key", "login"],
            "protocol": "a0-connector.v1",
            "websocket_namespace": "/ws",
            "websocket_handlers": ["plugins/a0_connector/ws_connector"],
        }, False

    async def fake_verify_api_key() -> bool:
        return True

    async def fake_connect_websocket() -> None:
        return None

    async def fake_send_hello() -> None:
        return None

    async def fake_create_chat() -> str:
        return "ctx-1"

    async def fake_subscribe_context(context_id: str) -> None:
        return None

    monkeypatch.setattr(app, "push_screen_wait", fake_push_screen_wait)
    monkeypatch.setattr(app, "_fetch_capabilities", fake_fetch_capabilities)
    monkeypatch.setattr("agent_zero_cli.app.save_env", lambda key, value: saved.__setitem__(key, value))
    monkeypatch.setattr(app.client, "verify_api_key", fake_verify_api_key)
    monkeypatch.setattr(app.client, "connect_websocket", fake_connect_websocket)
    monkeypatch.setattr(app.client, "send_hello", fake_send_hello)
    monkeypatch.setattr(app.client, "create_chat", fake_create_chat)
    monkeypatch.setattr(app.client, "subscribe_context", fake_subscribe_context)

    await app._startup()

    assert saved == {
        "AGENT_ZERO_HOST": "http://example.test",
        "AGENT_ZERO_API_KEY": "api-key-123",
    }


@pytest.mark.asyncio
async def test_startup_keeps_host_and_api_key_ephemeral_when_login_save_unchecked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = AgentZeroCLI(config=CLIConfig(instance_url="", api_key=""))
    log = FakeRichLog()
    input_widget = FakeInput()
    saved: dict[str, str] = {}

    app.query_one = lambda selector, cls=None: log if selector == "#chat-log" else input_widget

    async def fake_push_screen_wait(screen: object) -> object:
        if isinstance(screen, HostInputScreen):
            return "http://example.test"
        if isinstance(screen, LoginScreen):
            return LoginResult(api_key="api-key-123", save_credentials=False)
        raise AssertionError(f"Unexpected screen: {screen!r}")

    async def fake_fetch_capabilities(log_widget: FakeRichLog) -> tuple[dict[str, object], bool]:
        return {
            "auth": ["api_key", "login"],
            "protocol": "a0-connector.v1",
            "websocket_namespace": "/ws",
            "websocket_handlers": ["plugins/a0_connector/ws_connector"],
        }, False

    async def fake_verify_api_key() -> bool:
        return True

    async def fake_connect_websocket() -> None:
        return None

    async def fake_send_hello() -> None:
        return None

    async def fake_create_chat() -> str:
        return "ctx-1"

    async def fake_subscribe_context(context_id: str) -> None:
        return None

    monkeypatch.setattr(app, "push_screen_wait", fake_push_screen_wait)
    monkeypatch.setattr(app, "_fetch_capabilities", fake_fetch_capabilities)
    monkeypatch.setattr("agent_zero_cli.app.save_env", lambda key, value: saved.__setitem__(key, value))
    monkeypatch.setattr(app.client, "verify_api_key", fake_verify_api_key)
    monkeypatch.setattr(app.client, "connect_websocket", fake_connect_websocket)
    monkeypatch.setattr(app.client, "send_hello", fake_send_hello)
    monkeypatch.setattr(app.client, "create_chat", fake_create_chat)
    monkeypatch.setattr(app.client, "subscribe_context", fake_subscribe_context)

    await app._startup()

    assert app.config.instance_url == "http://example.test"
    assert app.config.api_key == "api-key-123"
    assert saved == {}


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


@pytest.mark.asyncio
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
