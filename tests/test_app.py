from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_zero_cli.app import AgentZeroCLI
from agent_zero_cli.client import DEFAULT_HOST
from agent_zero_cli.config import CLIConfig
from agent_zero_cli.instance_discovery import DiscoveredInstance, DiscoveryResult
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

    def set_project_enabled(self, enabled: bool) -> None:
        self.project_enabled = enabled

    def clear_token_usage(self) -> None:
        return None


class DummyAgentZeroCLI(AgentZeroCLI):
    def __init__(self) -> None:
        super().__init__(config=CLIConfig(instance_url="http://example.test"))
        self.rendered_events: list[dict[str, object]] = []


@pytest.fixture
def dummy_app(monkeypatch: pytest.MonkeyPatch) -> DummyAgentZeroCLI:
    app = DummyAgentZeroCLI()
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


async def test_active_run_preserves_draft_and_blocks_new_send(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dummy_app.connected = True
    dummy_app.current_context = "ctx-1"
    dummy_app.current_context_has_messages = True
    dummy_app.agent_active = True

    notices: list[tuple[str, bool]] = []

    async def fake_send_message(text: str, context_id: str | None) -> None:
        raise AssertionError(f"send_message should not run for {text=} {context_id=}")

    monkeypatch.setattr(dummy_app.client, "send_message", fake_send_message)
    monkeypatch.setattr(
        dummy_app,
        "_show_notice",
        lambda message, *, error=False: notices.append((message, error)),
    )

    input_widget = dummy_app._test_widgets["#message-input"]  # type: ignore[index]
    await dummy_app.on_chat_input_submitted(
        ChatInput.Submitted(value="draft follow-up", input=input_widget)
    )

    assert input_widget.value == "draft follow-up"
    assert input_widget.focused is True
    assert notices == [
        (
            "The agent is still running. Keep drafting here, then send after it finishes or pause it with F8.",
            True,
        )
    ]


async def test_remote_safety_toggles_update_local_permissions(
    dummy_app: DummyAgentZeroCLI,
) -> None:
    assert dummy_app._remote_files.allow_writes is False
    assert dummy_app._python_tty.enabled is False

    await dummy_app.action_toggle_remote_file_mode()
    await dummy_app.action_toggle_remote_exec()

    assert dummy_app._remote_file_write_enabled is True
    assert dummy_app._remote_exec_enabled is True
    assert dummy_app._remote_files.allow_writes is True
    assert dummy_app._python_tty.enabled is True
