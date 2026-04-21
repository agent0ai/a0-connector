from __future__ import annotations

import asyncio

import pytest
from rich.panel import Panel
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.selection import SELECT_ALL

from agent_zero_cli import chat_commands, connection
from agent_zero_cli.app import AgentZeroCLI
from agent_zero_cli.attachments import AttachmentRef
from agent_zero_cli.client import DEFAULT_HOST
from agent_zero_cli.config import CLIConfig
from agent_zero_cli.instance_discovery import DiscoveredInstance, DiscoveryResult
from agent_zero_cli.widgets.chat_log import ChatLog, SelectableStatic
from agent_zero_cli.widgets import ChatInput, SplashState


pytestmark = pytest.mark.anyio


def _instance(url: str, *, host_port: str = "50001") -> DiscoveredInstance:
    return DiscoveredInstance(
        id=f"agent-zero:{host_port}",
        name="agent-zero",
        url=url,
        host_port=host_port,
        status_text="agent-zero | frdel/agent-zero:latest",
    )


class FakeChatLog:
    def __init__(self) -> None:
        self.intro_visible = False
        self.cleared = False
        self.writes: list[object] = []
        self.status_entries: dict[int, dict[str, object]] = {}
        self._active_seq: int | None = None
        self._active_meta: dict[str, object] = {}

    def write(self, message: object) -> None:
        self.writes.append(message)

    def ensure_intro_banner(self) -> None:
        self.intro_visible = True

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
        del label, scroll
        self.status_entries[sequence] = {
            "detail": detail,
            "meta": meta or {},
            "active": active,
        }

    def set_active_status(
        self,
        sequence: int,
        label: str,
        detail: str,
        meta: dict[str, object] | None = None,
    ) -> None:
        del label, detail
        self._active_seq = sequence
        self._active_meta = meta or {}

    def dim_active_status(self) -> None:
        self._active_seq = None
        self._active_meta = {}

    def clear(self) -> None:
        self.cleared = True
        self.status_entries.clear()
        self._active_seq = None
        self._active_meta = {}


class FakeInput:
    def __init__(self) -> None:
        self.disabled = False
        self.display = True
        self.focused = False
        self.activity_label = ""
        self.activity_detail = ""
        self.activity_idle = True
        self.value = ""
        self.attachments = []

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

    def add_attachment(self, attachment: object) -> None:
        self.attachments.append(attachment)

    def set_attachments(self, attachments: list[object]) -> None:
        self.attachments = list(attachments)

    def clear_attachments(self) -> None:
        self.attachments = []


class FakeBodySwitcher:
    def __init__(self) -> None:
        self.current = "splash-view"


class FakeSplash:
    def __init__(self) -> None:
        self.state = SplashState(stage="host", host=DEFAULT_HOST)
        self.focused = False

    def set_state(self, state: SplashState) -> None:
        self.state = state

    def focus_primary(self) -> None:
        self.focused = True


class FakeConnectionStatus:
    def __init__(self) -> None:
        self.status = "disconnected"
        self.url = ""
        self.project_enabled = False
        self.project_state = None
        self.computer_use_status = ""
        self.computer_use_detail = ""

    def set_project_enabled(self, enabled: bool) -> None:
        self.project_enabled = enabled

    def set_project_state(self, project: object, *, enabled: bool) -> None:
        self.project_state = project
        self.project_enabled = enabled

    def set_computer_use_state(self, status: str, detail: str = "") -> None:
        self.computer_use_status = status
        self.computer_use_detail = detail

    def clear_token_usage(self) -> None:
        return None


class FakeComputerUseManager:
    def __init__(self) -> None:
        self.enabled = False
        self.trust_mode = "persistent"
        self.status_label = "disabled"
        self.status_detail = ""
        self.disconnect_calls = 0
        self.handled_ops: list[dict[str, object]] = []
        self._status_callback = None

    def set_status_callback(self, callback) -> None:
        self._status_callback = callback
        if callback is not None:
            callback(self.status_label, self.status_detail)

    def _emit(self) -> None:
        if self._status_callback is not None:
            self._status_callback(self.status_label, self.status_detail)

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled
        self.status_label = self.trust_mode if enabled else "disabled"
        self.status_detail = ""
        self._emit()

    def set_trust_mode(self, mode: str) -> str:
        self.trust_mode = mode
        if self.enabled:
            self.status_label = mode
        self._emit()
        return mode

    def metadata(self) -> dict[str, object]:
        return {
            "supported": True,
            "enabled": self.enabled,
            "trust_mode": self.trust_mode,
            "artifact_root": "/a0/tmp/_a0_connector/computer_use",
        }

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        self.status_label = "disabled" if not self.enabled else self.trust_mode
        self._emit()

    async def handle_op(self, data: dict[str, object]) -> dict[str, object]:
        self.handled_ops.append(dict(data))
        return {"op_id": data.get("op_id"), "ok": True, "result": {"status": "active"}}


class DummyAgentZeroCLI(AgentZeroCLI):
    def __init__(self) -> None:
        super().__init__(config=CLIConfig(instance_url="http://example.test"))
        self.rendered_events: list[dict[str, object]] = []


class TranscriptSelectionApp(App[None]):
    BINDINGS = [
        Binding("ctrl+c", "quit", "Exit", show=False),
    ]
    CSS = """
    #chat-log {
        width: 80;
        height: 20;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self.quit_attempts = 0

    def compose(self) -> ComposeResult:
        yield ChatLog(id="chat-log")

    def action_quit(self) -> None:
        self.quit_attempts += 1


@pytest.fixture
def dummy_app(monkeypatch: pytest.MonkeyPatch) -> DummyAgentZeroCLI:
    app = DummyAgentZeroCLI()
    app._computer_use = FakeComputerUseManager()
    widgets = {
        "#chat-log": FakeChatLog(),
        "#message-input": FakeInput(),
        "#body-switcher": FakeBodySwitcher(),
        "#splash-view": FakeSplash(),
        "#connection-status": FakeConnectionStatus(),
    }

    def _query_one(selector: object, cls: object = None) -> object:
        del cls
        return widgets[selector]

    app.query_one = _query_one  # type: ignore[method-assign]
    app._test_widgets = widgets  # type: ignore[attr-defined]
    app._computer_use.set_status_callback(lambda label, detail: app._apply_computer_use_status(label, detail))
    monkeypatch.setattr(
        "agent_zero_cli.app.render_connector_event",
        lambda log, event: app.rendered_events.append(event) or True,
        raising=False,
    )
    monkeypatch.setattr(
        "agent_zero_cli.event_handlers.render_connector_event",
        lambda log, event: app.rendered_events.append(event) or True,
    )
    return app


def test_default_client_host_uses_splash_default() -> None:
    app = AgentZeroCLI(config=CLIConfig(instance_url=""))
    assert app.client.base_url == DEFAULT_HOST


def test_shortcut_bindings_use_textual_canonical_key_names() -> None:
    bindings = {binding.action: binding for binding in AgentZeroCLI.BINDINGS}

    assert bindings["toggle_computer_use"].key == "f2"
    assert bindings["toggle_computer_use"].key_display == "F2"
    assert bindings["toggle_remote_file_mode"].key == "f3"
    assert bindings["toggle_remote_file_mode"].key_display == "F3"
    assert bindings["toggle_remote_exec"].key == "f4"
    assert bindings["toggle_remote_exec"].key_display == "F4"
    assert bindings["clear_chat"].key == "f5"
    assert bindings["clear_chat"].key_display == "F5"
    assert bindings["list_chats"].key == "f6"
    assert bindings["list_chats"].key_display == "F6"
    assert bindings["nudge_agent"].key == "f7"
    assert bindings["nudge_agent"].key_display == "F7"
    assert bindings["pause_agent"].key == "f8"
    assert bindings["pause_agent"].key_display == "F8"
    assert bindings["command_palette"].key == "ctrl+p"
    assert bindings["command_palette"].key_display == "^P"


def test_get_binding_description_reflects_computer_use_toggle_state(
    dummy_app: DummyAgentZeroCLI,
) -> None:
    bindings = {binding.action: binding for binding in AgentZeroCLI.BINDINGS}
    computer_use_binding = bindings["toggle_computer_use"]

    assert dummy_app.get_binding_description(computer_use_binding) == "Comp-use OFF"

    dummy_app._computer_use.set_enabled(True)

    assert dummy_app.get_binding_description(computer_use_binding) == "Comp-use ON"


def test_apply_instance_discovery_result_autoconnects_single_instance(
    dummy_app: DummyAgentZeroCLI,
) -> None:
    dummy_app._set_splash_state(host=DEFAULT_HOST)

    target = dummy_app._apply_instance_discovery_result(
        DiscoveryResult(
            status="ready",
            instances=(_instance("http://localhost:50001"),),
        ),
        auto_connect_single=True,
    )

    splash = dummy_app._test_widgets["#splash-view"]  # type: ignore[index]
    assert target == "http://localhost:50001"
    assert splash.state.host == "http://localhost:50001"
    assert splash.state.selected_host_url == "http://localhost:50001"
    assert splash.state.manual_entry_expanded is False


def test_context_event_status_updates_activity_lane_without_rendering_message(
    dummy_app: DummyAgentZeroCLI,
) -> None:
    dummy_app.connected = True
    dummy_app.current_context = "ctx-1"
    dummy_app.current_context_has_messages = True

    dummy_app._handle_context_event(
        {
            "context_id": "ctx-1",
            "event": "status",
            "sequence": 4,
            "data": {
                "meta": {
                    "step": "Using response...",
                    "thoughts": ["Plan the answer"],
                }
            },
        }
    )

    input_widget = dummy_app._test_widgets["#message-input"]  # type: ignore[index]
    log = dummy_app._test_widgets["#chat-log"]  # type: ignore[index]
    assert input_widget.activity_label == "Thinking"
    assert input_widget.activity_detail == "Using response..."
    assert log._active_seq == 4
    assert log._active_meta == {
        "step": "Using response...",
        "thoughts": ["Plan the answer"],
    }
    assert dummy_app.rendered_events == []


def test_context_event_after_complete_persists_status_without_reactivating_input(
    dummy_app: DummyAgentZeroCLI,
) -> None:
    dummy_app.connected = True
    dummy_app.current_context = "ctx-1"
    dummy_app.current_context_has_messages = True
    dummy_app._response_delivered = True
    dummy_app._context_run_complete = True

    dummy_app._handle_context_event(
        {
            "context_id": "ctx-1",
            "event": "status",
            "sequence": 7,
            "data": {"meta": {"step": "Memorizing results"}},
        }
    )

    input_widget = dummy_app._test_widgets["#message-input"]  # type: ignore[index]
    log = dummy_app._test_widgets["#chat-log"]  # type: ignore[index]
    assert input_widget.activity_idle is True
    assert log.status_entries[7] == {
        "detail": "Memorizing results",
        "meta": {"step": "Memorizing results"},
        "active": False,
    }


def test_assistant_message_switches_ready_view_to_chat(
    dummy_app: DummyAgentZeroCLI,
) -> None:
    dummy_app.connected = True
    dummy_app.current_context = "ctx-1"
    dummy_app.current_context_has_messages = False
    dummy_app._set_splash_state(stage="ready", actions=dummy_app._welcome_actions())

    dummy_app._handle_context_event(
        {
            "context_id": "ctx-1",
            "event": "assistant_message",
            "sequence": 1,
            "data": {"text": "Hello"},
        }
    )

    body = dummy_app._test_widgets["#body-switcher"]  # type: ignore[index]
    log = dummy_app._test_widgets["#chat-log"]  # type: ignore[index]
    input_widget = dummy_app._test_widgets["#message-input"]  # type: ignore[index]
    assert dummy_app.current_context_has_messages is True
    assert body.current == "chat-log"
    assert log.intro_visible is True
    assert input_widget.focused is True
    assert dummy_app.rendered_events[-1]["event"] == "assistant_message"


def test_remember_context_updates_config_and_persists(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saved: list[tuple[str, str]] = []

    monkeypatch.setattr(
        "agent_zero_cli.app.save_last_context",
        lambda host, context_id: saved.append((host, context_id)),
    )

    dummy_app._remember_context("ctx-42")

    assert dummy_app.config.last_context_id == "ctx-42"
    assert dummy_app.config.last_context_host == "http://example.test"
    assert saved == [("http://example.test", "ctx-42")]


async def test_switch_context_persists_last_context(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dummy_app.current_context = "ctx-old"

    unsubscribed: list[str] = []
    subscribed: list[tuple[str, int]] = []
    remembered: list[str] = []

    async def async_noop(*args, **kwargs) -> None:
        del args, kwargs

    async def fake_unsubscribe_context(context_id: str) -> None:
        unsubscribed.append(context_id)

    async def fake_subscribe_context(context_id: str, from_seq: int = 0) -> None:
        subscribed.append((context_id, from_seq))

    monkeypatch.setattr(dummy_app, "_stop_token_refresh", lambda: None)
    monkeypatch.setattr(dummy_app, "_hide_project_menu", async_noop)
    monkeypatch.setattr(dummy_app, "_hide_profile_menu", async_noop)
    monkeypatch.setattr(dummy_app.client, "unsubscribe_context", fake_unsubscribe_context)
    monkeypatch.setattr(dummy_app.client, "subscribe_context", fake_subscribe_context)
    monkeypatch.setattr(dummy_app, "_remember_context", lambda context_id: remembered.append(context_id))
    monkeypatch.setattr(dummy_app, "_refresh_projects", async_noop)
    monkeypatch.setattr(dummy_app, "_refresh_model_switcher", async_noop)
    monkeypatch.setattr(dummy_app, "_refresh_token_usage", async_noop)
    monkeypatch.setattr(dummy_app, "_start_token_refresh", lambda: None)

    await dummy_app._switch_context("ctx-2", has_messages_hint=True)

    assert unsubscribed == ["ctx-old"]
    assert subscribed == [("ctx-2", 0)]
    assert remembered == ["ctx-2"]
    assert dummy_app.current_context == "ctx-2"
    assert dummy_app.current_context_has_messages is True


async def test_resolve_initial_context_restores_saved_chat_for_same_host(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dummy_app.config.last_context_id = "ctx-saved"
    dummy_app.config.last_context_host = "http://example.test"
    dummy_app.connector_features = {"chat_get"}

    async def fake_list_chats() -> list[dict[str, object]]:
        return [{"id": "ctx-saved"}]

    async def fake_get_chat(context_id: str) -> dict[str, object]:
        assert context_id == "ctx-saved"
        return {"log_entries": [{"sequence": 1}]}

    async def fail_create_chat(*args, **kwargs) -> str:
        del args, kwargs
        raise AssertionError("create_chat should not run when the saved context still exists")

    monkeypatch.setattr(dummy_app.client, "list_chats", fake_list_chats)
    monkeypatch.setattr(dummy_app.client, "get_chat", fake_get_chat)
    monkeypatch.setattr(dummy_app.client, "create_chat", fail_create_chat)

    context_id, has_messages_hint = await connection._resolve_initial_context(
        dummy_app,
        "http://example.test",
    )

    assert context_id == "ctx-saved"
    assert has_messages_hint is True


async def test_resolve_initial_context_falls_back_to_new_chat_when_saved_chat_is_missing(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dummy_app.config.last_context_id = "ctx-saved"
    dummy_app.config.last_context_host = "http://example.test"

    async def fake_list_chats() -> list[dict[str, object]]:
        return [{"id": "ctx-other"}]

    async def fake_create_chat() -> str:
        return "ctx-new"

    monkeypatch.setattr(dummy_app.client, "list_chats", fake_list_chats)
    monkeypatch.setattr(dummy_app.client, "create_chat", fake_create_chat)

    context_id, has_messages_hint = await connection._resolve_initial_context(
        dummy_app,
        "http://example.test",
    )

    assert context_id == "ctx-new"
    assert has_messages_hint is False


async def test_action_pause_agent_resumes_when_pause_is_latched(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dummy_app.connected = True
    dummy_app.current_context = "ctx-1"
    dummy_app.current_context_has_messages = True
    dummy_app.connector_features = {"pause"}
    dummy_app._pause_latched = True

    calls: list[tuple[str | None, bool]] = []

    async def fake_pause_agent(context_id: str | None, *, paused: bool = True) -> dict[str, object]:
        calls.append((context_id, paused))
        return {"ok": True, "message": "Agent unpaused."}

    monkeypatch.setattr(dummy_app.client, "pause_agent", fake_pause_agent)

    await dummy_app.action_pause_agent()

    input_widget = dummy_app._test_widgets["#message-input"]  # type: ignore[index]
    assert calls == [("ctx-1", False)]
    assert dummy_app._pause_latched is False
    assert dummy_app.agent_active is True
    assert input_widget.activity_label == "Resuming"


async def test_active_run_submission_is_sent_as_intervention(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dummy_app.connected = True
    dummy_app.current_context = "ctx-1"
    dummy_app.current_context_has_messages = True
    dummy_app.agent_active = True

    calls: list[tuple[str, str | None, list[str] | None]] = []

    async def fake_send_message(
        text: str,
        context_id: str | None,
        attachments: list[str] | None = None,
    ) -> None:
        calls.append((text, context_id, attachments))

    monkeypatch.setattr(dummy_app.client, "send_message", fake_send_message)

    input_widget = dummy_app._test_widgets["#message-input"]  # type: ignore[index]
    await dummy_app.on_chat_input_submitted(
        ChatInput.Submitted(value="draft follow-up", input=input_widget)
    )

    assert calls == [("draft follow-up", "ctx-1", [])]
    assert input_widget.value == ""
    assert dummy_app.agent_active is True
    assert dummy_app._response_delivered is False
    assert dummy_app._context_run_complete is False


async def test_send_failure_restores_draft_and_previous_state(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dummy_app.connected = True
    dummy_app.current_context = "ctx-1"
    dummy_app.current_context_has_messages = False

    notices: list[tuple[str, bool]] = []

    async def fake_send_message(
        text: str,
        context_id: str | None,
        attachments: list[str] | None = None,
    ) -> None:
        del text, context_id, attachments
        raise RuntimeError("socket offline")

    monkeypatch.setattr(dummy_app.client, "send_message", fake_send_message)
    monkeypatch.setattr(
        dummy_app,
        "_show_notice",
        lambda message, *, error=False: notices.append((message, error)),
    )

    input_widget = dummy_app._test_widgets["#message-input"]  # type: ignore[index]
    body = dummy_app._test_widgets["#body-switcher"]  # type: ignore[index]

    await dummy_app.on_chat_input_submitted(
        ChatInput.Submitted(value="first hello", input=input_widget)
    )

    assert input_widget.value == "first hello"
    assert input_widget.focused is True
    assert dummy_app.current_context_has_messages is False
    assert dummy_app.agent_active is False
    assert body.current == "splash-view"
    assert notices == [("Error sending message: socket offline", True)]


async def test_attachment_only_submission_sends_attachment_refs(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dummy_app.connected = True
    dummy_app.current_context = "ctx-1"

    calls: list[tuple[str, str | None, list[str] | None]] = []

    async def fake_send_message(
        text: str,
        context_id: str | None,
        attachments: list[str] | None = None,
    ) -> None:
        calls.append((text, context_id, attachments))

    monkeypatch.setattr(dummy_app.client, "send_message", fake_send_message)
    input_widget = dummy_app._test_widgets["#message-input"]  # type: ignore[index]
    attachment = AttachmentRef(
        path="/a0/usr/uploads/clipboard.png",
        name="clipboard.png",
        mime_type="image/png",
    )

    await dummy_app.on_chat_input_submitted(
        ChatInput.Submitted(value="", input=input_widget, attachments=[attachment])
    )

    assert calls == [("", "ctx-1", ["/a0/usr/uploads/clipboard.png"])]
    assert dummy_app.current_context_has_messages is True


async def test_attach_clipboard_image_adds_pending_attachment(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    notices: list[tuple[str, bool]] = []
    attachment = AttachmentRef(
        path="/a0/usr/uploads/clipboard.png",
        name="clipboard.png",
        mime_type="image/png",
    )

    monkeypatch.setattr(
        "agent_zero_cli.app.save_clipboard_image_attachment",
        lambda: attachment,
    )
    monkeypatch.setattr(
        dummy_app,
        "_show_notice",
        lambda message, *, error=False: notices.append((message, error)),
    )

    handled = await dummy_app.attach_clipboard_image()

    input_widget = dummy_app._test_widgets["#message-input"]  # type: ignore[index]
    assert handled is True
    assert input_widget.attachments == [attachment]
    assert notices == [("Attached clipboard.png.", False)]


async def test_profile_command_dispatches_profile_menu(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dummy_app.connected = True
    dummy_app.connector_features = {"settings_get", "settings_set"}

    opened: list[str] = []

    async def fake_open_profile_menu() -> None:
        opened.append("profile-menu")

    monkeypatch.setattr(dummy_app, "_open_profile_menu", fake_open_profile_menu)

    await dummy_app._dispatch_command("/profile")

    assert opened == ["profile-menu"]


async def test_profile_command_with_argument_sets_profile(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dummy_app.connected = True
    dummy_app.connector_features = {"settings_get", "settings_set"}

    calls: list[dict[str, str]] = []
    notices: list[tuple[str, bool]] = []

    async def fake_get_settings() -> dict[str, object]:
        return {
            "settings": {"agent_profile": "agent0"},
            "additional": {
                "agent_subdirs": [
                    {"value": "agent0", "label": "Agent 0"},
                    {"value": "developer", "label": "Developer"},
                ]
            },
        }

    async def fake_set_settings(settings: dict[str, str]) -> dict[str, object]:
        calls.append(settings)
        return {
            "settings": {"agent_profile": "developer"},
            "additional": {
                "agent_subdirs": [
                    {"value": "agent0", "label": "Agent 0"},
                    {"value": "developer", "label": "Developer"},
                ]
            },
        }

    monkeypatch.setattr(dummy_app.client, "get_settings", fake_get_settings)
    monkeypatch.setattr(dummy_app.client, "set_settings", fake_set_settings)
    monkeypatch.setattr(dummy_app, "_show_notice", lambda message, *, error=False: notices.append((message, error)))

    await dummy_app._dispatch_command("/profile dev")

    assert calls == [{"agent_profile": "developer"}]
    assert notices == [("Agent profile set to Developer.", False)]


async def test_chat_list_command_supports_project_filter_and_sort_flags(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dummy_app.connected = True
    dummy_app.connector_features = {"chats_list"}

    parsed: list[tuple[str, bool]] = []

    async def fake_cmd_chats(
        _app: DummyAgentZeroCLI,
        *,
        sort_by: str = "updated",
        active_project_only: bool = False,
    ) -> None:
        parsed.append((sort_by, active_project_only))

    monkeypatch.setattr(chat_commands, "cmd_chats", fake_cmd_chats)

    await dummy_app._dispatch_command("/chats --project --sort=name")

    assert parsed == [("name", True)]


async def test_chat_list_command_rejects_unknown_flags(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dummy_app.connected = True
    dummy_app.connector_features = {"chats_list"}

    notices: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        dummy_app,
        "_show_notice",
        lambda message, *, error=False: notices.append((message, error)),
    )

    await dummy_app._dispatch_command("/chats --bogus")

    assert notices == [("Usage: /chats [--project|--all-projects] [--sort=updated|created|name]", True)]


async def test_remote_safety_toggles_update_local_permissions(
    dummy_app: DummyAgentZeroCLI,
) -> None:
    assert dummy_app._remote_files.allow_writes is False
    assert dummy_app._python_tty.enabled is False
    assert dummy_app._python_tty.allow_writes is False

    await dummy_app.action_toggle_remote_file_mode()
    await dummy_app.action_toggle_remote_exec()

    assert dummy_app._remote_file_write_enabled is True
    assert dummy_app._remote_exec_enabled is True
    assert dummy_app._remote_files.allow_writes is True
    assert dummy_app._python_tty.enabled is True
    assert dummy_app._python_tty.allow_writes is True


async def test_action_toggle_computer_use_updates_notice_and_status(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    notices: list[tuple[str, bool]] = []
    monkeypatch.setattr(dummy_app, "_show_notice", lambda message, *, error=False: notices.append((message, error)))

    await dummy_app.action_toggle_computer_use()

    status = dummy_app._test_widgets["#connection-status"]  # type: ignore[index]
    assert dummy_app._computer_use.enabled is True
    assert status.computer_use_status == "persistent"
    assert notices == [("Computer use enabled for this CLI session (persistent).", False)]

    await dummy_app.action_toggle_computer_use()

    assert dummy_app._computer_use.enabled is False
    assert dummy_app._computer_use.disconnect_calls == 1
    assert status.computer_use_status == "disabled"


async def test_action_toggle_computer_use_refreshes_hello_metadata_when_connected(
    dummy_app: DummyAgentZeroCLI,
) -> None:
    calls: list[dict[str, object]] = []

    async def fake_send_hello(
        *,
        computer_use: dict[str, object] | None = None,
        remote_files: dict[str, object] | None = None,
        remote_exec: dict[str, object] | None = None,
    ) -> dict[str, object]:
        calls.append(
            {
                "computer_use": dict(computer_use or {}),
                "remote_files": dict(remote_files or {}),
                "remote_exec": dict(remote_exec or {}),
            }
        )
        return {"exec_config": {"version": 1}}

    dummy_app.client.connected = True
    dummy_app.client.send_hello = fake_send_hello  # type: ignore[method-assign]

    await dummy_app.action_toggle_computer_use()
    await dummy_app.action_toggle_computer_use()

    assert calls == [
        {
            "computer_use": {
                "supported": True,
                "enabled": True,
                "trust_mode": "persistent",
                "artifact_root": "/a0/tmp/_a0_connector/computer_use",
            },
            "remote_files": {
                "enabled": True,
                "write_enabled": False,
                "mode": "read_only",
            },
            "remote_exec": {
                "enabled": False,
            },
        },
        {
            "computer_use": {
                "supported": True,
                "enabled": False,
                "trust_mode": "persistent",
                "artifact_root": "/a0/tmp/_a0_connector/computer_use",
            },
            "remote_files": {
                "enabled": True,
                "write_enabled": False,
                "mode": "read_only",
            },
            "remote_exec": {
                "enabled": False,
            },
        },
    ]


def test_system_commands_include_interactive_persistent_and_free_run(
    dummy_app: DummyAgentZeroCLI,
) -> None:
    commands = list(dummy_app.get_system_commands(None))
    titles = {getattr(command, "title", getattr(command, "name", "")) for command in commands}

    assert "Computer Use: Interactive" in titles
    assert "Computer Use: Persistent" in titles
    assert "Computer Use: Free-Run" in titles


async def test_set_computer_use_mode_updates_status_for_free_run_and_persistent(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    notices: list[tuple[str, bool]] = []
    monkeypatch.setattr(dummy_app, "_show_notice", lambda message, *, error=False: notices.append((message, error)))
    dummy_app._computer_use.set_enabled(True)

    await dummy_app._set_computer_use_mode("free_run")
    status = dummy_app._test_widgets["#connection-status"]  # type: ignore[index]
    assert dummy_app._computer_use.trust_mode == "free_run"
    assert status.computer_use_status == "free_run"

    await dummy_app._set_computer_use_mode("persistent")
    assert dummy_app._computer_use.trust_mode == "persistent"
    assert status.computer_use_status == "persistent"
    assert notices == [
        ("Computer use trust mode set to free_run.", False),
        ("Computer use trust mode set to persistent.", False),
    ]


async def test_set_computer_use_mode_refreshes_hello_metadata_when_connected(
    dummy_app: DummyAgentZeroCLI,
) -> None:
    calls: list[dict[str, object]] = []

    async def fake_send_hello(
        *,
        computer_use: dict[str, object] | None = None,
        remote_files: dict[str, object] | None = None,
        remote_exec: dict[str, object] | None = None,
    ) -> dict[str, object]:
        calls.append(
            {
                "computer_use": dict(computer_use or {}),
                "remote_files": dict(remote_files or {}),
                "remote_exec": dict(remote_exec or {}),
            }
        )
        return {"exec_config": {"version": 1}}

    dummy_app.client.connected = True
    dummy_app.client.send_hello = fake_send_hello  # type: ignore[method-assign]
    dummy_app._computer_use.set_enabled(True)

    await dummy_app._set_computer_use_mode("free_run")
    await dummy_app._set_computer_use_mode("persistent")

    assert calls == [
        {
            "computer_use": {
                "supported": True,
                "enabled": True,
                "trust_mode": "free_run",
                "artifact_root": "/a0/tmp/_a0_connector/computer_use",
            },
            "remote_files": {
                "enabled": True,
                "write_enabled": False,
                "mode": "read_only",
            },
            "remote_exec": {
                "enabled": False,
            },
        },
        {
            "computer_use": {
                "supported": True,
                "enabled": True,
                "trust_mode": "persistent",
                "artifact_root": "/a0/tmp/_a0_connector/computer_use",
            },
            "remote_files": {
                "enabled": True,
                "write_enabled": False,
                "mode": "read_only",
            },
            "remote_exec": {
                "enabled": False,
            },
        },
    ]


async def test_remote_safety_toggles_refresh_hello_metadata_when_connected(
    dummy_app: DummyAgentZeroCLI,
) -> None:
    calls: list[dict[str, object]] = []

    async def fake_send_hello(
        *,
        computer_use: dict[str, object] | None = None,
        remote_files: dict[str, object] | None = None,
        remote_exec: dict[str, object] | None = None,
    ) -> dict[str, object]:
        calls.append(
            {
                "computer_use": dict(computer_use or {}),
                "remote_files": dict(remote_files or {}),
                "remote_exec": dict(remote_exec or {}),
            }
        )
        return {"exec_config": {"version": 1}}

    dummy_app.client.connected = True
    dummy_app.client.send_hello = fake_send_hello  # type: ignore[method-assign]

    await dummy_app.action_toggle_remote_file_mode()
    await dummy_app.action_toggle_remote_exec()

    assert calls == [
        {
            "computer_use": {
                "supported": True,
                "enabled": False,
                "trust_mode": "persistent",
                "artifact_root": "/a0/tmp/_a0_connector/computer_use",
            },
            "remote_files": {
                "enabled": True,
                "write_enabled": True,
                "mode": "read_write",
            },
            "remote_exec": {
                "enabled": False,
            },
        },
        {
            "computer_use": {
                "supported": True,
                "enabled": False,
                "trust_mode": "persistent",
                "artifact_root": "/a0/tmp/_a0_connector/computer_use",
            },
            "remote_files": {
                "enabled": True,
                "write_enabled": True,
                "mode": "read_write",
            },
            "remote_exec": {
                "enabled": True,
            },
        },
    ]


def test_sync_computer_use_status_surfaces_rearm_required_state(
    dummy_app: DummyAgentZeroCLI,
) -> None:
    dummy_app._computer_use.status_label = "rearm required"
    dummy_app._computer_use.status_detail = "COMPUTER_USE_REARM_REQUIRED"

    dummy_app._sync_computer_use_status()

    status = dummy_app._test_widgets["#connection-status"]  # type: ignore[index]
    assert status.computer_use_status == "rearm required"
    assert status.computer_use_detail == "COMPUTER_USE_REARM_REQUIRED"


async def test_reset_disconnected_state_disconnects_computer_use_manager(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def async_noop(*args, **kwargs) -> None:
        del args, kwargs

    monkeypatch.setattr(dummy_app, "_cancel_compaction_refresh", lambda: None)
    monkeypatch.setattr(dummy_app, "_stop_remote_tree_publisher", lambda: None)
    monkeypatch.setattr(dummy_app, "_stop_token_refresh", lambda: None)
    monkeypatch.setattr(dummy_app, "_clear_token_usage", lambda: None)
    monkeypatch.setattr(dummy_app, "_clear_project_state", lambda: None)
    monkeypatch.setattr(dummy_app, "_set_workspace_context", lambda remote_workspace="": None)
    monkeypatch.setattr(dummy_app, "_clear_model_switcher", lambda: None)
    monkeypatch.setattr(dummy_app, "_sync_body_mode", lambda: None)
    monkeypatch.setattr(dummy_app._python_tty, "close", async_noop)

    connection._reset_disconnected_state(dummy_app)
    await asyncio.sleep(0)

    assert dummy_app._computer_use.disconnect_calls == 1


def test_copy_to_clipboard_mirrors_to_native_windows_clipboard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = AgentZeroCLI(config=CLIConfig(instance_url="http://example.test"))
    copied: list[str] = []
    mirrored: list[str] = []

    monkeypatch.setattr(
        "textual.app.App.copy_to_clipboard",
        lambda self, text: copied.append(text),
    )
    monkeypatch.setattr(
        "agent_zero_cli.app.should_use_native_windows_clipboard",
        lambda: True,
    )
    monkeypatch.setattr(
        "agent_zero_cli.app.copy_text_to_windows_clipboard",
        lambda text: mirrored.append(text) or True,
    )

    app.copy_to_clipboard("hello from transcript copy")

    assert copied == ["hello from transcript copy"]
    assert mirrored == ["hello from transcript copy"]


def test_copy_to_clipboard_skips_native_mirror_outside_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = AgentZeroCLI(config=CLIConfig(instance_url="http://example.test"))
    copied: list[str] = []
    mirrored: list[str] = []

    monkeypatch.setattr(
        "textual.app.App.copy_to_clipboard",
        lambda self, text: copied.append(text),
    )
    monkeypatch.setattr(
        "agent_zero_cli.app.should_use_native_windows_clipboard",
        lambda: False,
    )
    monkeypatch.setattr(
        "agent_zero_cli.app.copy_text_to_windows_clipboard",
        lambda text: mirrored.append(text) or True,
    )

    app.copy_to_clipboard("non-windows path")

    assert copied == ["non-windows path"]
    assert mirrored == []


async def test_chat_log_regular_entries_copy_selected_text() -> None:
    app = TranscriptSelectionApp()

    async with app.run_test() as pilot:
        log = app.query_one("#chat-log", ChatLog)
        log.append_or_update(
            1,
            Panel("Copy me from the live transcript", border_style="#555555", padding=(0, 1)),
        )
        await pilot.pause()

        widget = log._seq_to_widget[1]
        assert isinstance(widget, SelectableStatic)

        app.screen.selections = {widget: SELECT_ALL}
        app.screen.action_copy_text()

        assert "Copy me from the live transcript" in app.clipboard


async def test_chat_log_render_width_respects_scrollbar_gutter() -> None:
    app = TranscriptSelectionApp()

    async with app.run_test(size=(80, 20)) as pilot:
        log = app.query_one("#chat-log", ChatLog)
        for sequence in range(20):
            log.append_or_update(
                sequence,
                Panel(f"scroll row {sequence}", border_style="#555555", padding=(0, 1)),
            )
        await pilot.pause()

        widget = log._seq_to_widget[19]
        lines = widget.render().plain.splitlines()

        assert widget.size.width < log.size.width
        assert lines
        assert max(len(line) for line in lines) <= widget.size.width
        assert lines[0].endswith("╮")
        assert lines[-1].endswith("╯")


async def test_chat_log_status_entries_copy_selected_text() -> None:
    app = TranscriptSelectionApp()

    async with app.run_test() as pilot:
        log = app.query_one("#chat-log", ChatLog)
        log.append_or_update_status(
            2,
            "Thinking",
            "Planning next step",
            {"thoughts": ["Check transcript selection behavior"]},
            active=False,
        )
        await pilot.pause()

        widget = log._seq_to_widget[2]
        widget.action_toggle()
        await pilot.pause()
        app.screen.selections = {widget: SELECT_ALL}
        app.screen.action_copy_text()

        assert "Thinking" in app.clipboard
        assert "Planning next step" in app.clipboard
        assert "Check transcript selection behavior" in app.clipboard


async def test_chat_log_selection_ctrl_c_copies_without_triggering_quit() -> None:
    app = TranscriptSelectionApp()

    async with app.run_test() as pilot:
        log = app.query_one("#chat-log", ChatLog)
        log.append_or_update(3, Panel("Ctrl+C should copy this selection", border_style="#555555", padding=(0, 1)))
        await pilot.pause()

        widget = log._seq_to_widget[3]
        widget.focus()
        app.screen.selections = {widget: SELECT_ALL}
        await pilot.press("ctrl+c")

        assert app.quit_attempts == 0
        assert "Ctrl+C should copy this selection" in app.clipboard
