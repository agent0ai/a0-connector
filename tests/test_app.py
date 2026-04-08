from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from rich.padding import Padding
from textual.app import App, ComposeResult
from textual.widgets import Button, Input, Static

from agent_zero_cli.app import AgentZeroCLI, _DEFAULT_HOST
from agent_zero_cli.config import CLIConfig
from agent_zero_cli.rendering import render_connector_event
from agent_zero_cli.screens.compact_modal import CompactResult
from agent_zero_cli.screens.model_presets import ModelPresetsResult
from agent_zero_cli.widgets.command_palette import AgentCommandPalette
from agent_zero_cli.widgets.chat_log import (
    _AGENT_ZERO_BANNER,
    _AGENT_ZERO_BANNER_COMPACT,
    _AGENT_ZERO_BANNER_TINY,
    _select_agent_zero_banner,
)
from agent_zero_cli.widgets import DynamicFooter, SplashState
from agent_zero_cli.widgets.splash_view import (
    SplashHostPanel,
    SplashLoginPanel,
    SplashStatusPanel,
    SplashView,
    _connection_target_summary,
    _validate_connection_target,
)


async def _async_return(value=None):
    return value


pytestmark = pytest.mark.anyio


class FakeChatLog:
    def __init__(self) -> None:
        self.writes: list[object] = []
        self.cleared = False
        self.lines: list[object] = []
        self.sequences: dict[int, object] = {}
        self.status_entries: dict[int, dict[str, object]] = {}
        self.intro_visible = False
        self._active_seq: int | None = None
        self._active_label = ""
        self._active_detail = ""
        self._active_meta: dict[str, object] = {}

    def write(self, message: object) -> None:
        self.writes.append(message)
        self.lines.append(message)

    def ensure_intro_banner(self) -> None:
        self.intro_visible = True

    def append_or_update(self, sequence: int, renderable: object, scroll: bool = True) -> None:
        if sequence not in self.sequences:
            self.writes.append(renderable)
        self.sequences[sequence] = renderable

    def append_or_update_status(
        self,
        sequence: int,
        label: str,
        detail: str,
        meta: dict[str, object] | None = None,
        *,
        active: bool = False,
        scroll: bool = True,
    ) -> None:
        entry = {
            "label": label,
            "detail": detail,
            "meta": meta or {},
            "active": active,
        }
        if sequence not in self.sequences:
            self.writes.append(entry)
        self.status_entries[sequence] = entry
        self.sequences[sequence] = entry

    def set_active_status(
        self,
        seq: int,
        label: str,
        detail: str,
        meta: dict[str, object] | None = None,
    ) -> None:
        self._active_seq = seq
        self._active_label = label
        self._active_detail = detail
        self._active_meta = meta or {}
        self.append_or_update_status(seq, label, detail, meta, active=True)

    def dim_active_status(self) -> None:
        if self._active_seq is not None:
            self.append_or_update_status(
                self._active_seq,
                self._active_label,
                self._active_detail,
                self._active_meta,
                active=False,
            )
        self._active_seq = None
        self._active_label = ""
        self._active_detail = ""
        self._active_meta = {}

    def stop_active_status(self) -> None:
        self._active_seq = None
        self._active_meta = {}

    def advance_shimmer(self) -> None:
        pass

    def clear(self) -> None:
        self.cleared = True
        self.lines.clear()
        self.sequences.clear()
        self.status_entries.clear()
        self._active_seq = None


class FakeInput:
    def __init__(self) -> None:
        self.disabled = False
        self.display = True
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


class FakeFooter:
    def __init__(self) -> None:
        self.display = True


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


class SplashHarnessApp(App[None]):
    def compose(self) -> ComposeResult:
        yield SplashView()


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
        DynamicFooter: FakeFooter(),
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


def test_splash_login_submit_requires_both_fields_in_top_context() -> None:
    view = SplashView()
    posted: list[object] = []
    view.post_message = posted.append

    view._submit_login()

    assert posted == []
    assert view._login_panel.error_message == "Username and password are required."


def test_splash_login_panel_restores_target_context_after_error() -> None:
    panel = SplashLoginPanel()
    target = "http://207.148.13.38:32080"

    panel.set_target(target)
    panel.set_error("Wrong username or password: retry.")

    assert panel.error_message == "Wrong username or password: retry."
    assert panel.target_host == target

    panel.clear_error()

    assert panel.error_message == ""
    assert panel.target_host == target


async def test_splash_host_panel_blocks_invalid_url_submission() -> None:
    app = SplashHarnessApp()

    async with app.run_test(size=(100, 32)) as pilot:
        view = app.query_one(SplashView)
        view.set_state(SplashState(stage="host", host=_DEFAULT_HOST))
        await pilot.pause(0.1)

        host_input = view.query_one("#splash-host-input", Input)
        host_input.value = "localhost:5080"
        await pilot.pause(0.1)

        assert view._host_panel.is_valid is False
        assert view.query_one("#splash-host-submit", Button).disabled is True


def test_splash_view_back_action_posts_back_request() -> None:
    view = SplashView()
    posted: list[object] = []
    view.post_message = posted.append

    view._request_back_to_host()

    assert len(posted) == 1
    assert isinstance(posted[0], SplashView.ActionRequested)
    assert posted[0].action == "back"


async def test_splash_view_preserves_login_error_during_state_credentials_sync() -> None:
    app = SplashHarnessApp()

    async with app.run_test(size=(100, 32)) as pilot:
        view = app.query_one(SplashView)
        view.set_state(
            SplashState(
                stage="login",
                host="http://127.0.0.1:5080",
                username="admin",
                password="wrong",
                login_error="Wrong username or password: retry.",
            )
        )
        await pilot.pause(0.1)

        view.set_state(
            SplashState(
                stage="login",
                host="http://127.0.0.1:5080",
                username="admin",
                password="",
                login_error="Wrong username or password: retry.",
            )
        )
        await pilot.pause(0.1)

        assert view._login_panel.error_message == "Wrong username or password: retry."
        assert len(view.query("#splash-login-error")) == 0


def test_connection_target_summary_handles_invalid_port() -> None:
    label, normalized, secure = _connection_target_summary("http://bad:abc")

    assert label == "bad"
    assert normalized == "http://bad:abc"
    assert secure is False


def test_connection_target_summary_handles_malformed_ipv6_url() -> None:
    label, normalized, secure = _connection_target_summary("http://[::1")

    assert label == "Connector endpoint"
    assert normalized == "http://[::1"
    assert secure is False


def test_validate_connection_target_accepts_empty_as_default() -> None:
    valid, message = _validate_connection_target("")

    assert valid is True
    assert message == ""


def test_validate_connection_target_rejects_missing_scheme() -> None:
    valid, message = _validate_connection_target("127.0.0.1:5080")

    assert valid is False
    assert "http://" in message


def test_validate_connection_target_rejects_missing_port() -> None:
    valid, message = _validate_connection_target("http://127.0.0.1")

    assert valid is False
    assert "Missing port" in message


def test_validate_connection_target_accepts_explicit_port() -> None:
    valid, message = _validate_connection_target("http://127.0.0.1:5080")

    assert valid is True
    assert "looks valid" in message


async def test_splash_host_stage_hides_redundant_header_copy() -> None:
    app = SplashHarnessApp()

    async with app.run_test(size=(100, 32)) as pilot:
        view = app.query_one(SplashView)
        view.set_state(
            SplashState(
                stage="host",
                message="Enter an Agent Zero WebUI URL and port.",
                detail="",
                host=_DEFAULT_HOST,
            )
        )
        await pilot.pause(0.1)

        assert view.query_one("#splash-stage-label", Static).display is False
        assert view.query_one("#splash-message", Static).display is False


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


async def test_rejected_login_shows_inline_retry_copy(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_fetch_capabilities():
        return {
            "auth": ["api_key", "login"],
            "features": ["chat_create", "chats_list"],
            "protocol": "a0-connector.v1",
            "websocket_namespace": "/ws",
            "websocket_handlers": ["plugins/a0_connector/ws_connector"],
        }, False, ""

    monkeypatch.setattr(dummy_app, "_fetch_capabilities", fake_fetch_capabilities)
    monkeypatch.setattr(dummy_app.client, "login", lambda u, p: _async_return(None))

    await dummy_app._begin_connection(
        "http://example.test",
        username="admin",
        password="wrong-password",
        save_credentials_flag=False,
    )

    splash = dummy_app._test_widgets["#splash-view"]
    assert splash.state.stage == "login"
    assert splash.state.login_error == "Wrong username or password: retry."


def test_splash_back_action_returns_to_host_stage(dummy_app: DummyAgentZeroCLI) -> None:
    dummy_app._set_splash_state(
        stage="login",
        host="http://example.test:5080",
        username="admin",
        save_credentials=True,
        login_error="Wrong username or password: retry.",
    )

    dummy_app.on_splash_view_action_requested(SplashView.ActionRequested("back"))

    splash = dummy_app._test_widgets["#splash-view"]
    assert splash.state.stage == "host"
    assert splash.state.host == "http://example.test:5080"
    assert splash.state.login_error == ""
    assert splash.focused is True


def test_login_stage_hides_composer_until_ready(dummy_app: DummyAgentZeroCLI) -> None:
    input_widget = dummy_app._test_widgets["#message-input"]
    footer = dummy_app._test_widgets[DynamicFooter]
    dummy_app.connected = True
    dummy_app.current_context_has_messages = False

    dummy_app._set_splash_state(stage="login")

    assert input_widget.display is False
    assert footer.display is False

    dummy_app._set_splash_state(stage="ready", actions=dummy_app._welcome_actions())

    assert input_widget.display is True
    assert footer.display is True


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


def test_context_event_renders_info_messages_as_standalone_entries(dummy_app: DummyAgentZeroCLI) -> None:
    dummy_app.connected = True
    dummy_app.current_context = "ctx-1"
    dummy_app.current_context_has_messages = True

    dummy_app._handle_context_event(
        {
            "context_id": "ctx-1",
            "event": "info",
            "sequence": 3,
            "data": {"text": "Process reset, agent nudged."},
        }
    )

    assert dummy_app.rendered_events[-1]["event"] == "info"


def test_render_connector_event_pads_info_messages() -> None:
    log = FakeChatLog()

    rendered = render_connector_event(
        log,
        {
            "event": "info",
            "sequence": 7,
            "data": {"text": "Process reset, agent nudged."},
        },
    )

    assert rendered is True
    assert isinstance(log.writes[-1], Padding)


def test_context_event_keeps_status_messages_in_activity_lane(dummy_app: DummyAgentZeroCLI) -> None:
    dummy_app.connected = True
    dummy_app.current_context = "ctx-1"
    dummy_app.current_context_has_messages = True

    dummy_app._handle_context_event(
        {
            "context_id": "ctx-1",
            "event": "status",
            "sequence": 4,
            "data": {
                "text": "Thinking about the next step",
                "meta": {
                    "step": "Using response...",
                    "thoughts": ["Plan the answer", "Send the answer"],
                },
            },
        }
    )

    input_widget = dummy_app._test_widgets["#message-input"]
    log = dummy_app._test_widgets["#chat-log"]
    assert input_widget.activity_label == "Thinking"
    assert input_widget.activity_detail == "Using response..."
    assert log._active_seq == 4
    assert log._active_meta == {
        "step": "Using response...",
        "thoughts": ["Plan the answer", "Send the answer"],
    }
    assert dummy_app.rendered_events == []


def test_context_event_status_after_first_response_is_not_skipped(dummy_app: DummyAgentZeroCLI) -> None:
    dummy_app.connected = True
    dummy_app.current_context = "ctx-1"
    dummy_app.current_context_has_messages = True
    dummy_app._response_delivered = True

    dummy_app._handle_context_event(
        {
            "context_id": "ctx-1",
            "event": "status",
            "sequence": 12,
            "data": {"meta": {"step": "Calling subordinate A1"}},
        }
    )

    input_widget = dummy_app._test_widgets["#message-input"]
    log = dummy_app._test_widgets["#chat-log"]
    assert input_widget.disabled is True
    assert input_widget.activity_label == "Thinking"
    assert input_widget.activity_detail == "Calling subordinate A1"
    assert log._active_seq == 12
    assert log._active_meta == {"step": "Calling subordinate A1"}
    assert dummy_app.rendered_events == []


def test_context_event_after_complete_does_not_reactivate_input_lock(dummy_app: DummyAgentZeroCLI) -> None:
    dummy_app.connected = True
    dummy_app.current_context = "ctx-1"
    dummy_app.current_context_has_messages = True
    dummy_app._response_delivered = True
    dummy_app._context_run_complete = True
    dummy_app.agent_active = False

    input_widget = dummy_app._test_widgets["#message-input"]
    input_widget.disabled = False

    dummy_app._handle_context_event(
        {
            "context_id": "ctx-1",
            "event": "status",
            "sequence": 13,
            "data": {"meta": {"step": "Memorizing results"}},
        }
    )

    log = dummy_app._test_widgets["#chat-log"]
    assert input_widget.disabled is False
    assert dummy_app.agent_active is False
    assert input_widget.activity_idle is True
    assert log.status_entries[13]["active"] is False
    assert log.status_entries[13]["detail"] == "Memorizing results"


def test_context_snapshot_preserves_status_meta_for_history(dummy_app: DummyAgentZeroCLI) -> None:
    dummy_app.connected = True
    dummy_app.current_context = "ctx-1"
    dummy_app.current_context_has_messages = True

    dummy_app._handle_context_snapshot(
        {
            "context_id": "ctx-1",
            "events": [
                {
                    "event": "status",
                    "sequence": 9,
                    "data": {
                        "meta": {
                            "step": "Using web_search...",
                            "thoughts": ["Search docs", "Compare options"],
                        }
                    },
                }
            ],
        }
    )

    log = dummy_app._test_widgets["#chat-log"]
    assert log.status_entries[9]["detail"] == "Using web_search..."
    assert log.status_entries[9]["meta"] == {
        "step": "Using web_search...",
        "thoughts": ["Search docs", "Compare options"],
    }


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


def test_pause_requires_advertised_feature(dummy_app: DummyAgentZeroCLI) -> None:
    dummy_app.connected = True
    dummy_app.current_context = "ctx-1"
    dummy_app.agent_active = True
    dummy_app.connector_features = set()

    availability = dummy_app._pause_availability()

    assert availability.available is False
    assert "pause" in (availability.reason or "")


def test_nudge_requires_advertised_feature(dummy_app: DummyAgentZeroCLI) -> None:
    dummy_app.connected = True
    dummy_app.current_context = "ctx-1"
    dummy_app.current_context_has_messages = True
    dummy_app.connector_features = set()

    availability = dummy_app._nudge_availability()

    assert availability.available is False
    assert "nudge" in (availability.reason or "")


def test_nudge_is_available_during_active_run(dummy_app: DummyAgentZeroCLI) -> None:
    dummy_app.connected = True
    dummy_app.current_context = "ctx-1"
    dummy_app.current_context_has_messages = False
    dummy_app.agent_active = True
    dummy_app.connector_features = {"nudge"}

    availability = dummy_app._nudge_availability()

    assert availability.available is True


async def test_pause_command_releases_input_and_latches_paused_state(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dummy_app.connected = True
    dummy_app.current_context = "ctx-1"
    dummy_app.current_context_has_messages = True
    dummy_app.agent_active = True
    dummy_app.connector_features = {"pause"}
    input_widget = dummy_app._test_widgets["#message-input"]
    input_widget.disabled = True

    monkeypatch.setattr(
        dummy_app.client,
        "pause_agent",
        lambda context_id: _async_return({"ok": True, "message": "Agent paused."}),
    )

    await dummy_app._cmd_pause()

    log = dummy_app._test_widgets["#chat-log"]
    assert dummy_app.agent_active is False
    assert dummy_app._pause_latched is True
    assert input_widget.disabled is False
    assert input_widget.focused is True
    assert input_widget.activity_idle is True
    assert log.writes == []


def test_binding_description_switches_to_resume_when_paused(dummy_app: DummyAgentZeroCLI) -> None:
    binding = next(binding for binding in dummy_app.BINDINGS if binding.action == "pause_agent")

    assert dummy_app.get_binding_description(binding) == "Pause"

    dummy_app._pause_latched = True

    assert dummy_app.get_binding_description(binding) == "Resume"


async def test_pause_action_resumes_when_paused(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dummy_app.connected = True
    dummy_app.current_context = "ctx-1"
    dummy_app.current_context_has_messages = True
    dummy_app.connector_features = {"pause"}
    dummy_app._pause_latched = True
    input_widget = dummy_app._test_widgets["#message-input"]

    calls: list[tuple[str | None, bool]] = []

    async def fake_pause_agent(context_id: str | None, *, paused: bool = True):
        calls.append((context_id, paused))
        return {"ok": True, "message": "Agent unpaused."}

    monkeypatch.setattr(dummy_app.client, "pause_agent", fake_pause_agent)

    await dummy_app.action_pause_agent()

    log = dummy_app._test_widgets["#chat-log"]
    assert calls == [("ctx-1", False)]
    assert dummy_app._pause_latched is False
    assert dummy_app.agent_active is True
    assert input_widget.disabled is True
    assert input_widget.activity_label == "Resuming"
    assert log.writes == []


async def test_nudge_command_uses_connector_nudge_endpoint(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dummy_app.connected = True
    dummy_app.current_context = "ctx-1"
    dummy_app.current_context_has_messages = True
    dummy_app.connector_features = {"nudge"}
    input_widget = dummy_app._test_widgets["#message-input"]

    called: list[str | None] = []

    async def fake_nudge_agent(context_id: str | None):
        called.append(context_id)
        return {"ok": True, "status": "nudged"}

    monkeypatch.setattr(dummy_app.client, "nudge_agent", fake_nudge_agent)

    await dummy_app._cmd_nudge()

    assert called == ["ctx-1"]
    assert dummy_app.agent_active is True
    assert input_widget.disabled is True


def test_system_commands_are_curated_and_ordered(dummy_app: DummyAgentZeroCLI) -> None:
    screen = SimpleNamespace(query=lambda selector: [])

    titles = [command.title for command in dummy_app.get_system_commands(screen)]

    assert titles == ["/new", "/chats", "/compact", "/keys", "/help", "/quit"]


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
        "/new",
        "/chats",
        "/compact",
        "/presets",
        "/keys",
        "/help",
        "/quit",
    ]


def test_welcome_actions_match_visible_system_commands(dummy_app: DummyAgentZeroCLI) -> None:
    screen = SimpleNamespace(query=lambda selector: [])
    titles = [command.title for command in dummy_app.get_system_commands(screen)]
    splash_titles = [action.title for action in dummy_app._welcome_actions()]
    splash_keys = [action.key for action in dummy_app._welcome_actions()]

    assert splash_titles == titles
    assert splash_keys == titles


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


async def test_compact_command_works_without_model_presets_feature(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dummy_app.connected = True
    dummy_app.current_context = "ctx-1"
    dummy_app.current_context_has_messages = True
    dummy_app.connector_features = {"compact_chat"}

    monkeypatch.setattr(
        dummy_app.client,
        "get_compaction_stats",
        lambda context_id: _async_return({"ok": True, "stats": {"message_count": 4, "token_count": 1298}}),
    )

    presets_calls: list[bool] = []

    async def fail_if_called():
        presets_calls.append(True)
        raise AssertionError("get_model_presets should not be called when feature is unavailable")

    monkeypatch.setattr(dummy_app.client, "get_model_presets", fail_if_called)

    opened: list[object] = []

    async def fake_push_screen_wait(screen):
        opened.append(screen)
        return CompactResult(use_chat_model=False, preset_name=None)

    monkeypatch.setattr(dummy_app, "push_screen_wait", fake_push_screen_wait)

    compact_calls: list[tuple[str, bool, str | None]] = []

    async def fake_compact_chat(
        context_id: str,
        *,
        use_chat_model: bool,
        preset_name: str | None = None,
    ):
        compact_calls.append((context_id, use_chat_model, preset_name))
        return {"ok": True, "message": "Compaction started"}

    monkeypatch.setattr(dummy_app.client, "compact_chat", fake_compact_chat)
    refresh_contexts: list[str] = []
    monkeypatch.setattr(
        dummy_app,
        "_begin_compaction_refresh",
        lambda context_id: refresh_contexts.append(context_id),
    )

    await dummy_app._cmd_compact()

    assert opened
    assert compact_calls == [("ctx-1", False, None)]
    assert presets_calls == []
    assert refresh_contexts == ["ctx-1"]


async def test_compact_command_handles_compact_request_exception_gracefully(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dummy_app.connected = True
    dummy_app.current_context = "ctx-1"
    dummy_app.current_context_has_messages = True
    dummy_app.connector_features = {"compact_chat", "model_presets"}

    monkeypatch.setattr(
        dummy_app.client,
        "get_compaction_stats",
        lambda context_id: _async_return({"ok": True, "stats": {"message_count": 4, "token_count": 1298}}),
    )
    monkeypatch.setattr(
        dummy_app.client,
        "get_model_presets",
        lambda: _async_return([{"name": "Balanced"}]),
    )
    monkeypatch.setattr(
        dummy_app,
        "push_screen_wait",
        lambda screen: _async_return(CompactResult(use_chat_model=True, preset_name=None)),
    )

    async def fail_compact_chat(
        context_id: str,
        *,
        use_chat_model: bool,
        preset_name: str | None = None,
    ):
        del context_id, use_chat_model, preset_name
        raise RuntimeError("[Errno 104] Connection reset by peer")

    monkeypatch.setattr(dummy_app.client, "compact_chat", fail_compact_chat)

    await dummy_app._cmd_compact()

    log = dummy_app._test_widgets["#chat-log"]
    assert any("Failed to start compaction:" in str(line) for line in log.writes)


def test_context_event_suppresses_stream_during_compaction_refresh(dummy_app: DummyAgentZeroCLI) -> None:
    dummy_app.connected = True
    dummy_app.current_context = "ctx-1"
    dummy_app.current_context_has_messages = True
    dummy_app._compaction_refresh_context = "ctx-1"

    dummy_app._handle_context_event(
        {
            "context_id": "ctx-1",
            "event": "assistant_message",
            "sequence": 7,
            "data": {"text": "Compacting chat history..."},
        }
    )

    dummy_app._handle_context_event(
        {
            "context_id": "ctx-1",
            "event": "status",
            "sequence": 8,
            "data": {"meta": {"step": "Analyzing context"}},
        }
    )

    assert dummy_app.rendered_events == []
    log = dummy_app._test_widgets["#chat-log"]
    assert log.status_entries == {}


async def test_wait_for_compaction_and_reload_resubscribes_context(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dummy_app.connected = True
    dummy_app.current_context = "ctx-1"
    dummy_app.current_context_has_messages = True
    dummy_app._compaction_refresh_context = "ctx-1"
    dummy_app._compaction_refresh_task = asyncio.current_task()

    polls = iter(
        [
            {"ok": False, "status_code": 409, "message": "Cannot compact while agent is running"},
            {"ok": False, "message": "Not enough content to compact (minimum 1,000 tokens)"},
        ]
    )

    async def fake_get_compaction_stats(context_id: str):
        assert context_id == "ctx-1"
        return next(polls)

    monkeypatch.setattr(dummy_app.client, "get_compaction_stats", fake_get_compaction_stats)
    monkeypatch.setattr("agent_zero_cli.app._COMPACTION_POLL_INTERVAL_SECONDS", 0.0)

    switched: list[tuple[str, bool]] = []

    async def fake_switch_context(context_id: str, *, has_messages_hint: bool) -> None:
        switched.append((context_id, has_messages_hint))

    monkeypatch.setattr(dummy_app, "_switch_context", fake_switch_context)

    await dummy_app._wait_for_compaction_and_reload("ctx-1")

    assert switched == [("ctx-1", True)]
    assert dummy_app._compaction_refresh_context is None


def test_slash_query_opens_command_palette_with_seeded_query(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dummy_app.connected = True
    dummy_app.connector_features = {"chat_create", "chats_list"}
    input_widget = dummy_app._test_widgets["#message-input"]
    input_widget.value = "/"

    opened: list[AgentCommandPalette] = []
    monkeypatch.setattr(dummy_app, "_is_command_palette_open", lambda: False)
    monkeypatch.setattr(dummy_app, "push_screen", lambda screen: opened.append(screen))

    dummy_app.on_chat_input_value_changed(SimpleNamespace(value="/", input=input_widget))

    assert opened
    assert isinstance(opened[0], AgentCommandPalette)
    assert opened[0]._initial_query == "/"
    assert dummy_app._slash_palette_query == "/"


def test_command_palette_closed_clears_stale_slash_query(dummy_app: DummyAgentZeroCLI) -> None:
    input_widget = dummy_app._test_widgets["#message-input"]
    input_widget.value = "/"
    dummy_app._slash_palette_query = "/"

    dummy_app.on_command_palette_closed(SimpleNamespace(option_selected=False))

    assert input_widget.value == ""
    assert dummy_app._slash_palette_query is None


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
