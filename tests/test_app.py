from __future__ import annotations

from types import SimpleNamespace

import pytest
from textual.widgets import Static

from agent_zero_cli.app import AgentZeroCLI, _DEFAULT_HOST
from agent_zero_cli.config import CLIConfig
from agent_zero_cli.screens.model_presets import ModelPresetsResult
from agent_zero_cli.widgets.chat_log import (
    _AGENT_ZERO_BANNER,
    _AGENT_ZERO_BANNER_COMPACT,
    _AGENT_ZERO_BANNER_TINY,
    _select_agent_zero_banner,
)
from agent_zero_cli.widgets import SplashState
from agent_zero_cli.widgets.splash_view import SplashHostPanel, SplashStatusPanel, SplashView


async def _async_return(value=None):
    return value


pytestmark = pytest.mark.anyio


class FakeChatLog:
    def __init__(self) -> None:
        self.writes: list[object] = []
        self.cleared = False
        self.lines: list[object] = []
        self.sequences: dict[int, object] = {}
        self.intro_visible = False
        self._active_seq: int | None = None
        self._active_label = ""
        self._active_detail = ""

    def write(self, message: object) -> None:
        self.writes.append(message)
        self.lines.append(message)

    def ensure_intro_banner(self) -> None:
        self.intro_visible = True

    def append_or_update(self, sequence: int, renderable: object, scroll: bool = True) -> None:
        if sequence not in self.sequences:
            self.writes.append(renderable)
        self.sequences[sequence] = renderable

    def set_active_status(self, seq: int, label: str, detail: str) -> None:
        self._active_seq = seq
        self._active_label = label
        self._active_detail = detail
        self.append_or_update(seq, f"active:{label}:{detail}")

    def dim_active_status(self) -> None:
        if self._active_seq is not None:
            self.append_or_update(self._active_seq, f"dim:{self._active_label}:{self._active_detail}")
        self._active_seq = None
        self._active_label = ""
        self._active_detail = ""

    def stop_active_status(self) -> None:
        self._active_seq = None

    def advance_shimmer(self) -> None:
        pass

    def clear(self) -> None:
        self.cleared = True
        self.lines.clear()
        self.sequences.clear()
        self._active_seq = None


class FakeInput:
    def __init__(self) -> None:
        self.disabled = False
        self.focused = False
        self.activity_label = ""
        self.activity_detail = ""
        self.activity_idle = True
        self.slash_menu_active = False
        self.value = ""

    def focus(self) -> None:
        self.focused = True

    def set_activity(self, label: str, detail: str = "") -> None:
        self.activity_label = label
        self.activity_detail = detail
        self.activity_idle = False

    def set_idle(self) -> None:
        self.activity_label = ""
        self.activity_detail = ""
        self.activity_idle = True

    def set_slash_menu_active(self, active: bool) -> None:
        self.slash_menu_active = active


class FakeConnectionStatus:
    def __init__(self) -> None:
        self.status = "disconnected"
        self.url = ""


class FakeModelSwitcherBar:
    def __init__(self) -> None:
        self.visible = False
        self.busy = False
        self.allowed = False
        self.main_model = None
        self.utility_model = None
        self.presets: list[object] = []
        self.selected_preset = ""
        self.override_label = ""

    def clear(self) -> None:
        self.visible = False
        self.busy = False
        self.allowed = False
        self.main_model = None
        self.utility_model = None
        self.presets = []
        self.selected_preset = ""
        self.override_label = ""

    def set_busy(self, busy: bool) -> None:
        self.busy = busy

    def set_state(
        self,
        *,
        main_model: object,
        utility_model: object,
        presets: list[object] | tuple[object, ...],
        allowed: bool,
        selected_preset: str = "",
        override_label: str = "",
    ) -> None:
        self.visible = True
        self.main_model = main_model
        self.utility_model = utility_model
        self.presets = list(presets)
        self.allowed = allowed
        self.selected_preset = selected_preset
        self.override_label = override_label


class FakeBodySwitcher:
    def __init__(self) -> None:
        self.current = "splash-view"


class FakeSplash:
    def __init__(self) -> None:
        self.state = SplashState(stage="host", host=_DEFAULT_HOST)
        self.focused = False

    def set_state(self, state: SplashState) -> None:
        self.state = state

    def focus_primary(self) -> None:
        self.focused = True


class FakeSlashMenu:
    def __init__(self) -> None:
        self.display = False
        self.commands: list[object] = []
        self._highlighted_index: int | None = None

    def set_visible_commands(self, commands) -> None:
        self.commands = list(commands)
        self._highlighted_index = 0 if self.commands else None

    @property
    def highlighted_command(self):
        if self._highlighted_index is None:
            return None
        if self._highlighted_index >= len(self.commands):
            return None
        return self.commands[self._highlighted_index]

    def action_cursor_up(self) -> None:
        if self._highlighted_index is None:
            return
        self._highlighted_index = max(0, self._highlighted_index - 1)

    def action_cursor_down(self) -> None:
        if self._highlighted_index is None:
            return
        self._highlighted_index = min(len(self.commands) - 1, self._highlighted_index + 1)


class DummyAgentZeroCLI(AgentZeroCLI):
    def __init__(self, *, config: CLIConfig | None = None) -> None:
        super().__init__(
            config=config
            or CLIConfig(
                instance_url="http://example.test",
                api_key="",
            )
        )
        self.rendered_events: list[dict] = []


@pytest.fixture
def dummy_app(monkeypatch: pytest.MonkeyPatch) -> DummyAgentZeroCLI:
    app = DummyAgentZeroCLI()
    widgets = {
        "#chat-log": FakeChatLog(),
        "#message-input": FakeInput(),
        "#connection-status": FakeConnectionStatus(),
        "#model-switcher-bar": FakeModelSwitcherBar(),
        "#body-switcher": FakeBodySwitcher(),
        "#splash-view": FakeSplash(),
        "#slash-menu": FakeSlashMenu(),
    }

    def _query_one(selector: str, cls: object = None) -> object:
        return widgets[selector]

    app.query_one = _query_one
    app._test_widgets = widgets
    monkeypatch.setattr(
        "agent_zero_cli.app.render_connector_event",
        lambda log, event: app.rendered_events.append(event) or True,
    )
    return app


def test_default_client_host_uses_splash_default() -> None:
    app = AgentZeroCLI(config=CLIConfig(instance_url="", api_key=""))
    assert app.client.base_url == _DEFAULT_HOST


def test_splash_host_panel_uses_default_host_as_placeholder() -> None:
    panel = SplashHostPanel()

    panel.set_host(_DEFAULT_HOST)

    assert panel.host == _DEFAULT_HOST
    assert panel._host.value == ""
    assert panel._host.placeholder == _DEFAULT_HOST


def test_splash_view_uses_shared_agent_zero_banner_widget() -> None:
    view = SplashView()

    hero = next(widget for widget in view.compose() if getattr(widget, "id", "") == "splash-hero")

    assert isinstance(hero, Static)
    assert "agent-zero-banner" in hero.classes


def test_agent_zero_banner_selects_full_variant_when_it_fits() -> None:
    assert _select_agent_zero_banner(120) == _AGENT_ZERO_BANNER


def test_agent_zero_banner_selects_compact_variant_on_narrow_width() -> None:
    assert _select_agent_zero_banner(20) == _AGENT_ZERO_BANNER_COMPACT


def test_agent_zero_banner_selects_tiny_variant_for_extreme_narrow_width() -> None:
    assert _select_agent_zero_banner(1) == _AGENT_ZERO_BANNER_TINY


def test_splash_status_panel_hides_duplicate_error_copy() -> None:
    panel = SplashStatusPanel()

    panel.set_error("WebSocket connection failed", "Detailed connector guidance")

    assert panel._spinner.display is False
    assert panel._button.display is True
    assert panel._title.display is False
    assert panel._detail.display is False


async def test_startup_without_host_shows_host_stage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = DummyAgentZeroCLI(config=CLIConfig(instance_url="", api_key=""))
    widgets = {
        "#chat-log": FakeChatLog(),
        "#message-input": FakeInput(),
        "#connection-status": FakeConnectionStatus(),
        "#model-switcher-bar": FakeModelSwitcherBar(),
        "#body-switcher": FakeBodySwitcher(),
        "#splash-view": FakeSplash(),
        "#slash-menu": FakeSlashMenu(),
    }
    app.query_one = lambda selector, cls=None: widgets[selector]

    await app._startup()

    splash = widgets["#splash-view"]
    assert splash.state.stage == "host"
    assert splash.state.host == _DEFAULT_HOST
    assert splash.focused is True


async def test_begin_connection_with_saved_login_persists_host_and_api_key(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saved: dict[str, str] = {}

    async def fake_fetch_capabilities():
        return {
            "auth": ["api_key", "login"],
            "features": ["chat_create", "chats_list", "model_switcher"],
            "protocol": "a0-connector.v1",
            "websocket_namespace": "/ws",
            "websocket_handlers": ["plugins/a0_connector/ws_connector"],
        }, False, ""

    monkeypatch.setattr(dummy_app, "_fetch_capabilities", fake_fetch_capabilities)
    monkeypatch.setattr(dummy_app.client, "login", lambda u, p: _async_return("api-key-123"))
    monkeypatch.setattr(dummy_app.client, "verify_api_key", lambda: _async_return(True))
    monkeypatch.setattr(dummy_app.client, "connect_websocket", lambda: _async_return(None))
    monkeypatch.setattr(dummy_app.client, "send_hello", lambda: _async_return(None))
    monkeypatch.setattr(dummy_app.client, "create_chat", lambda: _async_return("ctx-1"))
    monkeypatch.setattr(dummy_app.client, "subscribe_context", lambda context_id, from_seq=0: _async_return(None))
    monkeypatch.setattr(
        dummy_app.client,
        "get_model_switcher",
        lambda context_id: _async_return(
            {
                "ok": True,
                "allowed": True,
                "override": {"preset_name": "Fast"},
                "presets": [{"name": "Fast"}],
                "main_model": {"provider": "anthropic", "name": "claude-haiku-4-5"},
                "utility_model": {"provider": "anthropic", "name": "claude-haiku-4-5"},
            }
        ),
    )
    monkeypatch.setattr("agent_zero_cli.app.save_env", lambda key, value: saved.__setitem__(key, value))

    await dummy_app._begin_connection(
        "http://example.test",
        username="admin",
        password="secret",
        save_credentials_flag=True,
    )

    splash = dummy_app._test_widgets["#splash-view"]
    body = dummy_app._test_widgets["#body-switcher"]
    input_widget = dummy_app._test_widgets["#message-input"]
    model_switcher = dummy_app._test_widgets["#model-switcher-bar"]
    assert splash.state.stage == "ready"
    assert dummy_app.current_context == "ctx-1"
    assert body.current == "splash-view"
    assert input_widget.focused is True
    assert model_switcher.visible is True
    assert model_switcher.selected_preset == "Fast"
    assert saved == {
        "AGENT_ZERO_HOST": "http://example.test",
        "AGENT_ZERO_API_KEY": "api-key-123",
    }


async def test_invalid_api_key_returns_to_login_stage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = DummyAgentZeroCLI(config=CLIConfig(instance_url="http://example.test", api_key="bad-key"))
    widgets = {
        "#chat-log": FakeChatLog(),
        "#message-input": FakeInput(),
        "#connection-status": FakeConnectionStatus(),
        "#model-switcher-bar": FakeModelSwitcherBar(),
        "#body-switcher": FakeBodySwitcher(),
        "#splash-view": FakeSplash(),
        "#slash-menu": FakeSlashMenu(),
    }
    app.query_one = lambda selector, cls=None: widgets[selector]

    async def fake_fetch_capabilities():
        return {
            "auth": ["api_key", "login"],
            "features": ["chat_create", "chats_list"],
            "protocol": "a0-connector.v1",
            "websocket_namespace": "/ws",
            "websocket_handlers": ["plugins/a0_connector/ws_connector"],
        }, False, ""

    monkeypatch.setattr(app, "_fetch_capabilities", fake_fetch_capabilities)
    monkeypatch.setattr(app.client, "verify_api_key", lambda: _async_return(False))

    await app._begin_connection("http://example.test")

    splash = widgets["#splash-view"]
    assert splash.state.stage == "login"
    assert app.config.api_key == ""
    assert app.client.api_key == ""


def test_context_event_switches_empty_welcome_to_chat(dummy_app: DummyAgentZeroCLI) -> None:
    dummy_app.connected = True
    dummy_app.current_context = "ctx-1"
    dummy_app.current_context_has_messages = False
    dummy_app._set_splash_state(stage="ready", actions=dummy_app._welcome_actions())

    dummy_app._handle_context_event(
        {
            "context_id": "ctx-1",
            "event": "assistant_message",
            "data": {"text": "Hello"},
            "sequence": 1,
        }
    )

    body = dummy_app._test_widgets["#body-switcher"]
    log = dummy_app._test_widgets["#chat-log"]
    assert dummy_app.current_context_has_messages is True
    assert body.current == "chat-log"
    assert log.intro_visible is True


def test_context_snapshot_shows_intro_before_first_message(dummy_app: DummyAgentZeroCLI) -> None:
    dummy_app.connected = True
    dummy_app.current_context = "ctx-1"
    dummy_app.current_context_has_messages = False

    dummy_app._handle_context_snapshot(
        {
            "context_id": "ctx-1",
            "events": [
                {"event": "status", "sequence": 1, "data": {"text": "Thinking"}},
                {"event": "assistant_message", "sequence": 2, "data": {"text": "Hello"}},
            ],
        }
    )

    body = dummy_app._test_widgets["#body-switcher"]
    log = dummy_app._test_widgets["#chat-log"]
    assert body.current == "chat-log"
    assert log.intro_visible is True


async def test_clear_chat_does_not_return_to_welcome(dummy_app: DummyAgentZeroCLI) -> None:
    dummy_app.connected = True
    dummy_app.current_context = "ctx-1"
    dummy_app.current_context_has_messages = True
    dummy_app._sync_body_mode()

    await dummy_app.action_clear_chat()

    body = dummy_app._test_widgets["#body-switcher"]
    log = dummy_app._test_widgets["#chat-log"]
    assert body.current == "chat-log"
    assert log.cleared is True


async def test_new_chat_returns_to_ready_welcome(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dummy_app.connected = True
    dummy_app.current_context = "ctx-old"
    dummy_app.current_context_has_messages = True
    dummy_app._sync_body_mode()

    monkeypatch.setattr(dummy_app.client, "unsubscribe_context", lambda context_id: _async_return(None))
    monkeypatch.setattr(dummy_app.client, "create_chat", lambda: _async_return("ctx-new"))
    monkeypatch.setattr(dummy_app.client, "subscribe_context", lambda context_id, from_seq=0: _async_return(None))
    monkeypatch.setattr(
        dummy_app.client,
        "get_model_switcher",
        lambda context_id: _async_return(
            {
                "ok": True,
                "allowed": True,
                "override": {"preset_name": "Fast"},
                "presets": [{"name": "Fast"}],
                "main_model": {"provider": "anthropic", "name": "claude-haiku-4-5"},
                "utility_model": {"provider": "anthropic", "name": "claude-haiku-4-5"},
            }
        ),
    )

    await dummy_app._cmd_new()

    splash = dummy_app._test_widgets["#splash-view"]
    body = dummy_app._test_widgets["#body-switcher"]
    input_widget = dummy_app._test_widgets["#message-input"]
    assert dummy_app.current_context == "ctx-new"
    assert dummy_app.current_context_has_messages is False
    assert splash.state.stage == "ready"
    assert body.current == "splash-view"
    assert input_widget.focused is True


async def test_model_switcher_preset_change_updates_current_chat_models(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dummy_app.connected = True
    dummy_app.current_context = "ctx-1"
    dummy_app.connector_features = {"model_switcher"}
    bar = dummy_app._test_widgets["#model-switcher-bar"]

    monkeypatch.setattr(
        dummy_app.client,
        "set_model_preset",
        lambda context_id, preset_name: _async_return(
            {
                "ok": True,
                "allowed": True,
                "override": {"preset_name": preset_name},
                "presets": [{"name": "Balanced"}],
                "main_model": {"provider": "anthropic", "name": "claude-sonnet-4"},
                "utility_model": {"provider": "anthropic", "name": "claude-haiku-4-5"},
            }
        ),
    )

    await dummy_app.on_model_switcher_bar_preset_changed(SimpleNamespace(value="Balanced", bar=bar))

    assert bar.busy is False
    assert bar.visible is True
    assert bar.selected_preset == "Balanced"
    assert bar.main_model == {"provider": "anthropic", "name": "claude-sonnet-4"}


async def test_help_is_generated_from_registry_on_welcome(dummy_app: DummyAgentZeroCLI) -> None:
    dummy_app.connected = True
    dummy_app.current_context = "ctx-1"
    dummy_app.current_context_has_messages = False
    dummy_app.connector_features = {
        "chat_create",
        "chats_list",
        "compact_chat",
        "model_presets",
    }
    dummy_app._set_splash_state(stage="ready", actions=dummy_app._welcome_actions())

    await dummy_app._cmd_help()

    splash = dummy_app._test_widgets["#splash-view"]
    assert "Available commands:" in splash.state.detail
    assert "/help" in splash.state.detail
    assert "/new" in splash.state.detail
    assert "/settings" not in splash.state.detail
    assert "/clear" not in splash.state.detail
    assert "/skills" not in splash.state.detail
    assert "/exit" not in splash.state.detail


def test_system_commands_are_curated_and_ordered(dummy_app: DummyAgentZeroCLI) -> None:
    screen = SimpleNamespace(query=lambda selector: [])

    titles = [command.title for command in dummy_app.get_system_commands(screen)]

    assert titles == ["New Chat", "Chats", "Keys", "Help", "Quit"]


def test_system_commands_include_model_presets_when_available(dummy_app: DummyAgentZeroCLI) -> None:
    dummy_app.connected = True
    dummy_app.current_context = "ctx-1"
    dummy_app.connector_features = {"model_switcher"}
    dummy_app._apply_model_switcher_state(
        {
            "ok": True,
            "allowed": True,
            "override": {"preset_name": "Balanced"},
            "presets": [
                {"name": "Balanced"},
                {"name": "Fast", "label": "Fast lane"},
            ],
            "main_model": {"provider": "anthropic", "name": "claude-haiku-4-5"},
            "utility_model": {"provider": "anthropic", "name": "claude-haiku-4-5"},
        }
    )
    screen = SimpleNamespace(query=lambda selector: [])

    titles = [command.title for command in dummy_app.get_system_commands(screen)]

    assert titles == [
        "New Chat",
        "Chats",
        "Model Presets",
        "Keys",
        "Help",
        "Quit",
    ]


async def test_model_presets_command_opens_picker_and_applies_selection(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dummy_app.connected = True
    dummy_app.current_context = "ctx-1"
    dummy_app.connector_features = {"model_switcher"}
    dummy_app._apply_model_switcher_state(
        {
            "ok": True,
            "allowed": True,
            "override": {"preset_name": "Balanced"},
            "presets": [{"name": "Balanced"}, {"name": "Fast"}],
            "main_model": {"provider": "anthropic", "name": "claude-haiku-4-5"},
            "utility_model": {"provider": "anthropic", "name": "claude-haiku-4-5"},
        }
    )

    monkeypatch.setattr(
        dummy_app.client,
        "get_model_switcher",
        lambda context_id: _async_return(
            {
                "ok": True,
                "allowed": True,
                "override": {"preset_name": "Balanced"},
                "presets": [{"name": "Balanced"}, {"name": "Fast"}],
                "main_model": {"provider": "anthropic", "name": "claude-haiku-4-5"},
                "utility_model": {"provider": "anthropic", "name": "claude-haiku-4-5"},
            }
        ),
    )
    monkeypatch.setattr(
        dummy_app.client,
        "get_model_presets",
        lambda: _async_return([{"name": "Balanced"}, {"name": "Fast"}]),
    )
    monkeypatch.setattr(
        dummy_app,
        "push_screen_wait",
        lambda screen: _async_return(ModelPresetsResult(preset_name="Fast")),
    )

    selected: list[str | None] = []

    async def fake_set_model_preset(
        preset_name: str | None,
        *,
        bar=None,
    ) -> None:
        selected.append(preset_name)

    monkeypatch.setattr(dummy_app, "_set_model_preset", fake_set_model_preset)

    await dummy_app._cmd_model_presets()

    assert selected == ["Fast"]


def test_slash_menu_opens_and_closes_with_query_changes(dummy_app: DummyAgentZeroCLI) -> None:
    dummy_app.connected = True
    dummy_app.connector_features = {"chat_create", "chats_list"}

    dummy_app.on_chat_input_value_changed(SimpleNamespace(value="/", input=dummy_app._test_widgets["#message-input"]))
    menu = dummy_app._test_widgets["#slash-menu"]
    assert menu.display is True
    assert menu.commands

    dummy_app.on_chat_input_value_changed(SimpleNamespace(value="/help ", input=dummy_app._test_widgets["#message-input"]))
    assert menu.display is False


async def test_slash_tab_inserts_highlighted_canonical_command(dummy_app: DummyAgentZeroCLI) -> None:
    dummy_app.connected = True
    dummy_app.connector_features = {"chat_create", "chats_list"}
    input_widget = dummy_app._test_widgets["#message-input"]

    dummy_app.on_chat_input_value_changed(SimpleNamespace(value="/", input=input_widget))
    await dummy_app.on_chat_input_slash_navigation(SimpleNamespace(key="tab", input=input_widget))

    assert input_widget.value.endswith(" ")
    assert input_widget.value.startswith("/")


async def test_unknown_command_fails_gracefully_on_welcome(dummy_app: DummyAgentZeroCLI) -> None:
    dummy_app.connected = True
    dummy_app.current_context = "ctx-1"
    dummy_app.current_context_has_messages = False
    dummy_app._set_splash_state(stage="ready", actions=dummy_app._welcome_actions())

    await dummy_app._dispatch_command("/wat")

    splash = dummy_app._test_widgets["#splash-view"]
    assert "Unknown command" in splash.state.message or "Unknown command" in splash.state.detail


async def test_removed_slash_commands_fail_gracefully(dummy_app: DummyAgentZeroCLI) -> None:
    dummy_app.connected = True
    dummy_app.current_context = "ctx-1"
    dummy_app.current_context_has_messages = False
    dummy_app._set_splash_state(stage="ready", actions=dummy_app._welcome_actions())

    splash = dummy_app._test_widgets["#splash-view"]
    for command in ("/clear", "/skills", "/exit"):
        await dummy_app._dispatch_command(command)
        assert f"Unknown command: {command}." in splash.state.detail


async def test_quit_disconnects_before_exit(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    disconnected: list[bool] = []
    exited: list[bool] = []

    async def fake_disconnect() -> None:
        disconnected.append(True)

    monkeypatch.setattr(dummy_app.client, "disconnect", fake_disconnect)
    monkeypatch.setattr(dummy_app, "exit", lambda: exited.append(True))

    await dummy_app.action_quit()

    assert disconnected == [True]
    assert exited == [True]
