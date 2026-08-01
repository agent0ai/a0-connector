from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace

import pytest
from rich.panel import Panel
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.css.query import NoMatches
from textual.selection import SELECT_ALL

from agent_zero_cli import __version__, chat_commands, connection, event_handlers, model_commands, self_update, splash_helpers
from agent_zero_cli.app import AgentZeroCLI
from agent_zero_cli.attachments import AttachmentRef, AttachmentUpload
from agent_zero_cli.client import DEFAULT_HOST
from agent_zero_cli.config import CLIConfig
from agent_zero_cli.instance_discovery import DiscoveredInstance, DiscoveryResult
from agent_zero_cli.rendering import extract_detail, render_connector_event
from agent_zero_cli.remote_files import RemoteTreeSnapshot
from agent_zero_cli.screens.installed_plugins import InstalledPluginsScreen
from agent_zero_cli.screens.model_runtime import ModelRuntimeResult
from agent_zero_cli.widgets import computer_use_banner as computer_use_banner_mod
from agent_zero_cli.widgets.command_palette import is_raw_skill_command, is_raw_slash_command
from agent_zero_cli.widgets.chat_log import ChatLog, SelectableStatic
from agent_zero_cli.widgets import (
    ChatInput,
    ComputerUseBanner,
    ConnectionStatus,
    ContextTab,
    ContextTabs,
    GoalBar,
    MessageQueueBar,
    ModelSwitcherBar,
    ProfileMenuItem,
    ProjectMenuItem,
    ProjectMenuPopover,
    SplashState,
    context_tab_from_metadata,
)


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
        self._seq_to_widget: dict[int, object] = {}
        self._active_seq: int | None = None
        self._active_meta: dict[str, object] = {}
        self.copy_text = "visible transcript"
        self.copy_visible_only: bool | None = None
        self.history_pages: list[tuple[int, bool]] = []

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

    def set_history_page(self, *, before: int, has_more: bool) -> None:
        self.history_pages.append((before, has_more))

    def copyable_text(self, *, visible_only: bool = True) -> str:
        self.copy_visible_only = visible_only
        return self.copy_text


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
        self.history_context = None
        self.history_seeded: list[str] = []
        self.queue_active = False

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

    def set_queue_active(self, active: bool) -> None:
        self.queue_active = active

    def set_history_context(self, context_id: str | None) -> None:
        self.history_context = context_id

    def seed_history(self, values: list[str]) -> None:
        self.history_seeded.extend(values)


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


class FakeContextTabs:
    def __init__(self) -> None:
        self.display = False
        self.tabs: tuple[ContextTab, ...] = ()
        self.active_context_id = ""
        self.can_create = False

    def set_tabs(
        self,
        tabs: list[ContextTab],
        active_context_id: str | None,
        *,
        can_create: bool = False,
    ) -> None:
        self.tabs = tuple(tabs)
        self.active_context_id = active_context_id or ""
        self.can_create = can_create
        self.display = bool(tabs)


class FakeComputerUseBanner:
    def __init__(self) -> None:
        self.display = False
        self.message = ""

    def set_state(
        self,
        *,
        enabled: bool,
        status: str = "",
        backend_id: str = "",
        backend_family: str = "",
    ) -> None:
        if not enabled or status == "Disabled":
            self.display = False
            self.message = ""
            return
        is_windows = backend_id == "windows" or backend_family == "windows"
        if is_windows and status in {"Approval Required", "Rearm Required"}:
            self.message = "Computer Use is checking Windows desktop access."
            self.display = True
            return
        if status == "Active":
            self.message = "Computer Use is active for this CLI session."
        elif status == "Arming":
            self.message = "Computer Use is checking host permissions."
        elif status == "Approval Required":
            self.message = (
                "Computer Use is enabled. Ask Agent Zero to perform the desktop task; "
                "the system permission portal will appear."
            )
        elif status == "Rearm Required":
            self.message = "Computer use needs re-arming before Agent Zero can control your computer again."
        else:
            self.message = "Agent Zero CLI can control your computer in this session."
        self.display = True


class FakeModelSwitcher:
    def __init__(self) -> None:
        self.busy = False
        self.cleared = False
        self.classes: set[str] = set()
        self.state_calls: list[dict[str, object]] = []

    def clear(self) -> None:
        self.cleared = True

    def set_busy(self, busy: bool) -> None:
        self.busy = busy

    def set_state(self, **kwargs: object) -> None:
        self.state_calls.append(dict(kwargs))
        self.cleared = False

    def set_class(self, add: bool, class_name: str) -> None:
        if add:
            self.classes.add(class_name)
        else:
            self.classes.discard(class_name)


class FakeMessageQueueBar:
    def __init__(self) -> None:
        self.display = False
        self.items: list[dict[str, object]] = []
        self.cleared = False

    def set_items(self, items: list[dict[str, object]]) -> None:
        self.items = list(items)
        self.display = bool(items)
        self.cleared = False

    def clear(self) -> None:
        self.items = []
        self.display = False
        self.cleared = True


class FakeGoalBar:
    def __init__(self) -> None:
        self.display = False
        self.goal: dict[str, object] | None = None
        self.cleared = False
        self.classes: set[str] = set()

    def set_goal(self, goal: dict[str, object] | None) -> None:
        self.goal = dict(goal) if goal else None
        self.display = bool(self.goal and self.goal.get("status") != "complete")
        self.cleared = False

    def clear(self) -> None:
        self.goal = None
        self.display = False
        self.cleared = True

    def set_class(self, add: bool, class_name: str) -> None:
        if add:
            self.classes.add(class_name)
        else:
            self.classes.discard(class_name)


class FakeComputerUseManager:
    def __init__(self) -> None:
        self.enabled = False
        self.trust_mode = "allow"
        self.status_label = "disabled"
        self.status_detail = ""
        self.backend_id = ""
        self.backend_family = ""
        self.disconnect_calls = 0
        self.arm_calls: list[str | None] = []
        self.rearm_calls: list[str | None] = []
        self.handled_ops: list[dict[str, object]] = []
        self.arm_result: dict[str, object] = {
            "ok": False,
            "code": "COMPUTER_USE_REARM_REQUIRED",
            "error": "Computer use is not armed.",
        }
        self.rearm_result: dict[str, object] = {"ok": True, "result": {"status": "active"}}
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
        if enabled and self.trust_mode == "allow":
            self.status_label = "rearm required"
        else:
            self.status_label = self.trust_mode if enabled else "disabled"
        self.status_detail = ""
        self._emit()

    def reset_enabled_for_shutdown(self) -> None:
        self.enabled = False
        self.status_label = "disabled"
        self.status_detail = ""
        self._emit()

    def mark_approval_pending(self) -> None:
        if self.enabled:
            self.status_label = "arming"
            self.status_detail = ""
            self._emit()

    def set_trust_mode(self, mode: str) -> str:
        self.trust_mode = mode
        if self.enabled:
            self.status_label = mode
        self._emit()
        return mode

    def metadata(self) -> dict[str, object]:
        metadata = {
            "supported": True,
            "enabled": self.enabled,
            "trust_mode": self.trust_mode,
            "status": self.status_label,
            "last_error": self.status_detail,
            "restore_token_present": False,
            "artifact_root": "/a0/tmp/_a0_connector/computer_use",
        }
        if self.backend_id:
            metadata["backend_id"] = self.backend_id
        if self.backend_family:
            metadata["backend_family"] = self.backend_family
        return metadata

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        self.status_label = "disabled" if not self.enabled else self.trust_mode
        self._emit()

    async def rearm(self, context_id: str | None = None) -> dict[str, object]:
        self.rearm_calls.append(context_id)
        self.enabled = True
        if bool(self.rearm_result.get("ok")):
            self.status_label = "active"
            self.status_detail = ""
        else:
            self.status_label = "rearm required"
            self.status_detail = str(self.rearm_result.get("error") or "")
        self._emit()
        return dict(self.rearm_result)

    async def ensure_armed(self, context_id: str | None = None) -> dict[str, object]:
        self.arm_calls.append(context_id)
        self.enabled = True
        if bool(self.arm_result.get("ok")):
            self.status_label = "active"
            self.status_detail = ""
        else:
            result = self.arm_result.get("result")
            result_status = result.get("status") if isinstance(result, dict) else ""
            self.status_label = str(result_status or "error")
            if self.arm_result.get("code") == "COMPUTER_USE_REARM_REQUIRED":
                self.status_label = "rearm required"
            self.status_detail = str(self.arm_result.get("error") or "")
        self._emit()
        return dict(self.arm_result)

    async def handle_op(self, data: dict[str, object]) -> dict[str, object]:
        self.handled_ops.append(dict(data))
        action = str(data.get("action") or "").strip().lower().replace("-", "_")
        if action == "start_session":
            self.enabled = True
            self.status_label = "active"
            self.status_detail = ""
        elif action == "stop_session":
            self.status_label = "allow" if self.enabled else "disabled"
            self.status_detail = ""
        return {"op_id": data.get("op_id"), "ok": True, "result": {"status": "active"}}


class FakeHostBrowserManager:
    def __init__(self) -> None:
        self.enabled = False
        self.disconnect_calls = 0
        self.handled_ops: list[dict[str, object]] = []
        self.playwright_available = True
        self.install_calls = 0
        self.remote_debugging = False
        self.profile_selections: list[tuple[str, str]] = []

    def metadata(self) -> dict[str, object]:
        supported = self.playwright_available or self.remote_debugging
        return {
            "supported": supported,
            "enabled": self.enabled,
            "status": "ready" if self.enabled and supported else "disabled",
            "browser_family": "chrome-cdp" if self.remote_debugging else "chrome",
            "profile_label": "localhost:9222" if self.remote_debugging else "Default",
            "features": ["open", "content"],
            "support_reason": "" if supported else "missing playwright",
        }

    def status_text(self) -> str:
        if not self.playwright_available and not self.remote_debugging:
            return "Host browser unsupported: missing playwright"
        if self.remote_debugging:
            state = "ready" if self.enabled else "disabled"
            return f"Host browser {state}: remote debugging browser at ws://localhost:9222/devtools/browser/test."
        state = "ready" if self.enabled else "disabled"
        return f"Host browser {state}: chrome profile Default (/tmp/profile)."

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled

    def selected_profile(
        self,
        profile_mode: object = "",
        *,
        browser_selection: object = "",
    ) -> SimpleNamespace:
        self.profile_selections.append((str(profile_mode or ""), str(browser_selection or "")))
        return SimpleNamespace(
            is_remote_debugging=self.remote_debugging
            or str(browser_selection or "").startswith("ws://")
        )

    def available_browser_metadata(self) -> list[dict[str, object]]:
        return [
            {
                "id": "ws://localhost:9222/devtools/browser/test",
                "family": "chrome-cdp",
                "label": "Chrome (allowed) - localhost:9222",
                "cdp_endpoint": "ws://localhost:9222/devtools/browser/test",
                "status": "ready",
                "enabled": True,
            },
            {
                "id": "chrome:default",
                "family": "chrome",
                "label": "Chrome - Default",
                "cdp_endpoint": "",
                "status": "ready",
                "enabled": True,
            },
        ]

    def has_playwright_dependency(self) -> bool:
        return self.playwright_available

    def playwright_install_command(self) -> list[str]:
        return ["uv", "pip", "install", "--python", "/tmp/python", "playwright"]

    async def ensure_playwright_dependency(self) -> dict[str, object]:
        self.install_calls += 1
        self.playwright_available = True
        return {"installed": True, "command": self.playwright_install_command(), "output": ""}

    async def disconnect(self) -> None:
        self.disconnect_calls += 1

    async def handle_op(self, data: dict[str, object]) -> dict[str, object]:
        self.handled_ops.append(dict(data))
        return {"op_id": data.get("op_id"), "ok": True, "result": {"status": "ready"}}


def _host_browser_metadata(enabled: bool = False) -> dict[str, object]:
    return {
        "supported": True,
        "enabled": enabled,
        "status": "ready" if enabled else "disabled",
        "browser_family": "chrome",
        "profile_label": "Default",
        "features": ["open", "content"],
        "support_reason": "",
    }


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


class ContextTabsRenderApp(App[None]):
    CSS = """
    ContextTabs {
        height: 1;
        padding: 0 1;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self.close_events: list[tuple[str, str]] = []
        self.selected_events: list[str] = []

    def compose(self) -> ComposeResult:
        yield ContextTabs(id="context-tabs")

    def on_mount(self) -> None:
        self.query_one("#context-tabs", ContextTabs).set_tabs(
            [
                ContextTab("ctx-alpha", "Architecture sketch", True, project_color="#14d6c8"),
                ContextTab("ctx-beta", "Streaming fix", True),
            ],
            "ctx-alpha",
            can_create=True,
        )

    def on_context_tabs_close_requested(self, event: ContextTabs.CloseRequested) -> None:
        event.stop()
        self.close_events.append((event.context_id, event.replacement_context_id))

    def on_context_tabs_context_selected(self, event: ContextTabs.ContextSelected) -> None:
        event.stop()
        self.selected_events.append(event.context_id)


@pytest.fixture
def dummy_app(monkeypatch: pytest.MonkeyPatch) -> DummyAgentZeroCLI:
    app = DummyAgentZeroCLI()
    app._computer_use = FakeComputerUseManager()
    app._host_browser = FakeHostBrowserManager()
    widgets = {
        "#chat-log": FakeChatLog(),
        "#message-input": FakeInput(),
        "#body-switcher": FakeBodySwitcher(),
        "#splash-view": FakeSplash(),
        "#computer-use-banner": FakeComputerUseBanner(),
        "#goal-bar": FakeGoalBar(),
        "#model-switcher-bar": FakeModelSwitcher(),
        "#message-queue-bar": FakeMessageQueueBar(),
        "#connection-status": FakeConnectionStatus(),
        "#context-tabs": FakeContextTabs(),
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


def test_remote_exec_config_sets_startup_default() -> None:
    app = AgentZeroCLI(config=CLIConfig(remote_exec_enabled=True))

    assert app._remote_exec_enabled is True
    assert app._python_tty.enabled is True


async def test_full_cli_quit_resets_computer_use_enablement(dummy_app: DummyAgentZeroCLI) -> None:
    class FakePythonTty:
        def __init__(self) -> None:
            self.close_calls = 0

        async def close(self) -> None:
            self.close_calls += 1

    class FakeClient:
        def __init__(self) -> None:
            self.disconnect_calls = 0

        async def disconnect(self) -> None:
            self.disconnect_calls += 1

    fake_tty = FakePythonTty()
    fake_client = FakeClient()
    exit_calls: list[bool] = []
    dummy_app._python_tty = fake_tty  # type: ignore[assignment]
    dummy_app.client = fake_client  # type: ignore[assignment]
    dummy_app.exit = lambda: exit_calls.append(True)  # type: ignore[method-assign]
    dummy_app._computer_use.set_enabled(True)

    await dummy_app._disconnect_and_exit()

    assert dummy_app._computer_use.enabled is False
    assert dummy_app._computer_use.status_label == "disabled"
    assert dummy_app._computer_use.disconnect_calls == 1
    assert fake_tty.close_calls == 1
    assert fake_client.disconnect_calls == 1
    assert exit_calls == [True]


def test_profile_menu_item_click_stops_event_and_posts_selection() -> None:
    item = ProfileMenuItem("Developer", profile_key="developer")
    captured: list[object] = []
    stopped: list[bool] = []
    item.post_message = lambda message: captured.append(message)  # type: ignore[method-assign]
    event = SimpleNamespace(stop=lambda: stopped.append(True))

    item.on_click(event)

    assert stopped == [True]
    assert len(captured) == 1
    assert isinstance(captured[0], ProfileMenuItem.Selected)


def test_project_menu_item_click_stops_event_and_posts_selection() -> None:
    item = ProjectMenuItem("Plugins 1", action="activate", project_name="plugins_1")
    captured: list[object] = []
    stopped: list[bool] = []
    item.post_message = lambda message: captured.append(message)  # type: ignore[method-assign]
    event = SimpleNamespace(stop=lambda: stopped.append(True))

    item.on_click(event)

    assert stopped == [True]
    assert len(captured) == 1
    assert isinstance(captured[0], ProjectMenuItem.Selected)


async def test_project_menu_accepts_human_readable_project_names() -> None:
    class ProjectMenuApp(App[None]):
        def compose(self) -> ComposeResult:
            yield ProjectMenuPopover([{"name": "Project Showreel", "title": "Project Showreel"}])

    app = ProjectMenuApp()
    async with app.run_test():
        item = app.query_one(ProjectMenuItem)

    assert item.project_name == "Project Showreel"


async def test_escape_dismisses_open_profile_menu_when_focus_is_elsewhere(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dismissed: list[bool] = []
    key_events: list[str] = []
    workers: list[tuple[str | None, bool]] = []
    tasks: list[asyncio.Task[None]] = []

    async def fake_dismiss_profile_menu() -> None:
        dismissed.append(True)

    def fake_run_worker(coro, *, exclusive: bool = False, name: str | None = None):
        workers.append((name, exclusive))
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task

    event = SimpleNamespace(
        key="escape",
        prevent_default=lambda: key_events.append("prevent_default"),
        stop=lambda: key_events.append("stop"),
    )
    dummy_app._profile_menu_popover = SimpleNamespace()  # type: ignore[assignment]
    monkeypatch.setattr(dummy_app, "_dismiss_profile_menu", fake_dismiss_profile_menu)
    monkeypatch.setattr(dummy_app, "run_worker", fake_run_worker)

    dummy_app.on_key(event)
    await asyncio.gather(*tasks)

    assert key_events == ["prevent_default", "stop"]
    assert dismissed == [True]
    assert workers == [("dismiss-profile-menu", True)]


async def test_escape_dismisses_open_project_menu_when_focus_is_elsewhere(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hidden: list[bool] = []
    key_events: list[str] = []
    workers: list[tuple[str | None, bool]] = []
    tasks: list[asyncio.Task[None]] = []

    async def fake_hide_project_menu() -> None:
        hidden.append(True)

    def fake_run_worker(coro, *, exclusive: bool = False, name: str | None = None):
        workers.append((name, exclusive))
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task

    event = SimpleNamespace(
        key="escape",
        prevent_default=lambda: key_events.append("prevent_default"),
        stop=lambda: key_events.append("stop"),
    )
    dummy_app._project_menu_popover = SimpleNamespace()  # type: ignore[assignment]
    monkeypatch.setattr(dummy_app, "_hide_project_menu", fake_hide_project_menu)
    monkeypatch.setattr(dummy_app, "run_worker", fake_run_worker)

    dummy_app.on_key(event)
    await asyncio.gather(*tasks)

    assert key_events == ["prevent_default", "stop"]
    assert hidden == [True]
    assert workers == [("hide-project-menu", True)]


async def test_escape_key_closes_profile_menu_from_composer_focus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = AgentZeroCLI(
        config=CLIConfig(instance_url="http://example.test"),
        auto_connect_single_instance=False,
        discover_instances=False,
        connect_configured_host=False,
    )

    async def async_noop(*args, **kwargs) -> None:
        del args, kwargs

    async def fake_get_settings() -> dict[str, object]:
        return {
            "settings": {"agent_profile": "agent0"},
            "additional": {
                "agent_subdirs": [
                    {"value": "agent0", "label": "Agent 0"},
                ]
            },
        }

    async def fake_get_chat(context_id: str) -> dict[str, object]:
        assert context_id == "ctx-1"
        return {"agent_profile": "agent0"}

    monkeypatch.setattr(app, "_startup", async_noop)
    monkeypatch.setattr(app, "_start_cli_update_check", lambda: None)
    monkeypatch.setattr(app.client, "get_settings", fake_get_settings)
    monkeypatch.setattr(app.client, "get_chat", fake_get_chat)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        app.connected = True
        app.current_context = "ctx-1"
        app.current_context_has_messages = True
        app.connector_features = {"settings_get", "agent_profile_set", "chat_get"}
        app._sync_body_mode()

        await app._open_profile_menu()
        await pilot.pause(0.1)
        assert app._profile_menu_popover is not None
        composer = app.query_one("#message-input", ChatInput)
        assert app._profile_menu_popover.region.y + app._profile_menu_popover.region.height == composer.region.y

        app.query_one("#message-input", ChatInput).focus()
        await pilot.press("escape")
        await pilot.pause(0.1)

    assert app._profile_menu_popover is None


def test_shortcut_bindings_use_textual_canonical_key_names() -> None:
    bindings = {binding.action: binding for binding in AgentZeroCLI.BINDINGS}
    quit_bindings = [
        binding
        for binding in AgentZeroCLI.BINDINGS
        if binding.key.lower() in {"ctrl+c", "ctrl+q"}
    ]
    quit_keys = {binding.key.lower() for binding in quit_bindings}

    assert "toggle_computer_use" not in bindings
    assert all(binding.key != "f2" for binding in AgentZeroCLI.BINDINGS)
    assert {"ctrl+c", "ctrl+q"} <= quit_keys
    assert all(binding.show is False for binding in quit_bindings)
    assert bindings["toggle_remote_file_mode"].key == "f3"
    assert bindings["toggle_remote_file_mode"].key_display == "F3"
    assert bindings["toggle_remote_exec"].key == "f4"
    assert bindings["toggle_remote_exec"].key_display == "F4"
    assert bindings["clear_chat"].key == "f5"
    assert bindings["clear_chat"].key_display == "F5"
    assert bindings["clear_chat"].show is False
    assert bindings["list_chats"].key == "f6"
    assert bindings["list_chats"].key_display == "F6"
    assert bindings["nudge_agent"].key == "f7"
    assert bindings["nudge_agent"].key_display == "F7"
    assert bindings["pause_agent"].key == "f8"
    assert bindings["pause_agent"].key_display == "F8"
    assert bindings["copy_visible_chat"].key == "f9"
    assert bindings["copy_visible_chat"].key_display == "F9"
    assert bindings["copy_visible_chat"].show is False
    assert bindings["command_palette"].key == "ctrl+p"
    assert bindings["command_palette"].key_display == "^P"


def test_surface_help_documents_chat_tab_shortcuts(dummy_app: DummyAgentZeroCLI) -> None:
    dummy_app.connected = True
    dummy_app.current_context_has_messages = True

    dummy_app._surface_help()

    log = dummy_app._test_widgets["#chat-log"]  # type: ignore[index]
    help_text = "\n".join(str(write) for write in log.writes)
    assert "Chat tab shortcuts:" in help_text
    assert "Tab + n - create a new chat in a new tab." in help_text
    assert "Tab + x - close/hide the current tab without deleting the chat" in help_text
    assert "Tab + Left/Right - move between visible chat tabs." in help_text


def test_raw_slash_command_detection_requires_arguments() -> None:
    assert is_raw_slash_command("/browser status")
    assert is_raw_slash_command("/computer-use on")
    assert is_raw_slash_command("  /project Main  ")
    assert not is_raw_slash_command("/")
    assert not is_raw_slash_command("/browser")
    assert not is_raw_slash_command("hello /browser status")


def test_get_binding_description_reflects_remote_safety_toggle_state(
    dummy_app: DummyAgentZeroCLI,
) -> None:
    bindings = {binding.action: binding for binding in AgentZeroCLI.BINDINGS}
    file_binding = bindings["toggle_remote_file_mode"]
    exec_binding = bindings["toggle_remote_exec"]

    assert dummy_app.get_binding_description(file_binding) == "Read&Write"
    assert dummy_app.get_binding_description(exec_binding) == "Code-exec OFF"

    dummy_app._set_remote_file_write_enabled(False)
    dummy_app._set_remote_exec_enabled(True)

    assert dummy_app.get_binding_description(file_binding) == "Read-only"
    assert dummy_app.get_binding_description(exec_binding) == "Code-exec ON"


def test_pause_binding_description_switches_to_resume_when_latched(
    dummy_app: DummyAgentZeroCLI,
) -> None:
    bindings = {binding.action: binding for binding in AgentZeroCLI.BINDINGS}
    pause_binding = bindings["pause_agent"]

    assert dummy_app.get_key_display(pause_binding) == "F8"
    assert dummy_app.get_binding_description(pause_binding) == "Pause"

    dummy_app._pause_latched = True

    assert dummy_app.get_key_display(pause_binding) == "F8"
    assert dummy_app.get_binding_description(pause_binding) == "Resume"


async def test_cli_update_check_surfaces_available_release(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    notices: list[tuple[str, bool]] = []
    notifications: list[tuple[str, dict[str, object]]] = []

    def fake_check_for_update(current_version: str) -> self_update.UpdateCheckResult:
        assert current_version == __version__
        return self_update.UpdateCheckResult(
            current_version=current_version,
            latest_version="99.0",
            latest_tag="v99.0",
        )

    monkeypatch.setattr(
        "agent_zero_cli.app.self_update.check_for_update",
        fake_check_for_update,
    )
    monkeypatch.setattr(
        dummy_app,
        "_show_notice",
        lambda message, *, error=False: notices.append((message, error)),
    )
    monkeypatch.setattr(
        dummy_app,
        "notify",
        lambda message, **kwargs: notifications.append((message, kwargs)),
    )

    await dummy_app._check_for_cli_update()

    message = (
        f"a0 CLI update available: 99.0 (installed {__version__}). "
        "Run `a0 update` after exiting to upgrade."
    )
    assert notices == [(message, False)]
    assert notifications == [
        (
            message,
            {
                "title": "a0 CLI update available",
                "severity": "information",
                "timeout": 12,
                "markup": False,
            },
        )
    ]


async def test_cli_update_check_stays_quiet_when_no_update(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    notices: list[tuple[str, bool]] = []
    notifications: list[tuple[str, dict[str, object]]] = []

    monkeypatch.setattr(
        "agent_zero_cli.app.self_update.check_for_update",
        lambda current_version: None,
    )
    monkeypatch.setattr(
        dummy_app,
        "_show_notice",
        lambda message, *, error=False: notices.append((message, error)),
    )
    monkeypatch.setattr(
        dummy_app,
        "notify",
        lambda message, **kwargs: notifications.append((message, kwargs)),
    )

    await dummy_app._check_for_cli_update()

    assert notices == []
    assert notifications == []


def test_connector_version_warning_flags_newer_core() -> None:
    message = connection.connector_version_warning(
        {"agent_zero_version": "v1.18"},
        client_version="1.12",
    )

    assert "Agent Zero v1.18 is newer than a0 CLI 1.12" in message
    assert "a0 update" in message


def test_connector_version_warning_ignores_equal_or_older_core() -> None:
    assert connection.connector_version_warning(
        {"agent_zero_version": "v1.18"},
        client_version="1.19",
    ) == ""
    assert connection.connector_version_warning(
        {"agent_zero_version": "unknown"},
        client_version="1.12",
    ) == ""


def test_dispatch_command_worker_passes_coroutine_function_not_created_coroutine(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[tuple[object, dict[str, object]]] = []

    def fake_run_worker(work: object, **kwargs: object) -> object:
        captured.append((work, kwargs))
        return object()

    monkeypatch.setattr(dummy_app, "run_worker", fake_run_worker)

    dummy_app._run_dispatch_command("/help", worker_name="slash-help")

    work, kwargs = captured[0]
    assert callable(work)
    assert not inspect.isawaitable(work)
    assert inspect.iscoroutinefunction(work.func)  # type: ignore[attr-defined]
    assert kwargs == {
        "exclusive": True,
        "name": "slash-help",
    }


async def test_remote_exec_toggle_off_closes_existing_sessions(
    dummy_app: DummyAgentZeroCLI,
) -> None:
    class FakePythonTty:
        def __init__(self) -> None:
            self.enabled = True
            self.close_calls = 0

        def set_enabled(self, enabled: bool) -> None:
            self.enabled = enabled

        async def close(self) -> None:
            self.close_calls += 1

    fake_tty = FakePythonTty()
    dummy_app._python_tty = fake_tty  # type: ignore[assignment]
    dummy_app._remote_exec_enabled = True

    await dummy_app.action_toggle_remote_exec()

    assert dummy_app._remote_exec_enabled is False
    assert fake_tty.enabled is False
    assert fake_tty.close_calls == 1


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


def test_apply_instance_discovery_result_opens_manual_when_docker_unavailable(
    dummy_app: DummyAgentZeroCLI,
) -> None:
    dummy_app._set_splash_state(host=DEFAULT_HOST, manual_entry_expanded=False)

    target = dummy_app._apply_instance_discovery_result(
        DiscoveryResult(
            status="unavailable",
            detail="No local Docker runtime responded. Enter a URL manually.",
        ),
        auto_connect_single=True,
    )

    splash = dummy_app._test_widgets["#splash-view"]  # type: ignore[index]
    assert target == ""
    assert splash.state.host == DEFAULT_HOST
    assert splash.state.discovered_instances == ()
    assert splash.state.discovery_status == "unavailable"
    assert splash.state.selected_host_url == ""
    assert splash.state.manual_entry_expanded is True


def test_apply_instance_discovery_result_lists_multiple_instances_without_autoconnect(
    dummy_app: DummyAgentZeroCLI,
) -> None:
    first = _instance("http://localhost:5080", host_port="5080")
    second = _instance("http://localhost:5081", host_port="5081")
    dummy_app._set_splash_state(host=DEFAULT_HOST, manual_entry_expanded=False)

    target = dummy_app._apply_instance_discovery_result(
        DiscoveryResult(
            status="ready",
            instances=(first, second),
        ),
        auto_connect_single=True,
    )

    splash = dummy_app._test_widgets["#splash-view"]  # type: ignore[index]
    assert target == ""
    assert splash.state.discovered_instances == (first, second)
    assert splash.state.host == "http://localhost:5080"
    assert splash.state.selected_host_url == "http://localhost:5080"
    assert splash.state.manual_entry_expanded is False


def test_apply_instance_discovery_result_keeps_agent_zero_host_in_manual_mode(
    dummy_app: DummyAgentZeroCLI,
) -> None:
    cloudflare_host = "https://webmasters-ink-tribe-zope.trycloudflare.com"
    dummy_app.config.instance_url = cloudflare_host
    dummy_app._set_splash_state(host=cloudflare_host, manual_entry_expanded=False)

    target = dummy_app._apply_instance_discovery_result(
        DiscoveryResult(
            status="ready",
            instances=(_instance("http://localhost:5080", host_port="5080"),),
        ),
        auto_connect_single=True,
    )

    splash = dummy_app._test_widgets["#splash-view"]  # type: ignore[index]
    assert target == ""
    assert splash.state.host == cloudflare_host
    assert splash.state.selected_host_url == "http://localhost:5080"
    assert splash.state.manual_entry_expanded is True


def test_start_instance_discovery_can_be_disabled_for_manual_url_testing(
    dummy_app: DummyAgentZeroCLI,
) -> None:
    dummy_app._discover_instances = False
    dummy_app._set_splash_state(host=DEFAULT_HOST, manual_entry_expanded=False)

    dummy_app._start_instance_discovery(auto_connect_single=True)

    splash = dummy_app._test_widgets["#splash-view"]  # type: ignore[index]
    assert splash.state.discovered_instances == ()
    assert splash.state.discovery_status == "unavailable"
    assert splash.state.discovery_detail == "Docker discovery disabled by --no-docker-discovery."
    assert splash.state.selected_host_url == ""
    assert splash.state.manual_entry_expanded is True


async def test_startup_direct_connect_skips_instance_discovery(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dummy_app.config.instance_url = "http://127.0.0.1:5080/"
    dummy_app._connect_configured_host = True
    connections: list[str] = []
    discovery_calls: list[bool] = []

    async def fake_begin_connection(host: str, **_: object) -> None:
        connections.append(host)

    monkeypatch.setattr(dummy_app, "_begin_connection", fake_begin_connection)
    monkeypatch.setattr(
        dummy_app,
        "_start_instance_discovery",
        lambda *, auto_connect_single=False: discovery_calls.append(auto_connect_single),
    )

    await connection.startup(dummy_app)

    assert connections == ["http://127.0.0.1:5080/"]
    assert discovery_calls == []


async def test_begin_connection_to_protected_instance_advances_to_login(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ProtectedClient:
        def __init__(self) -> None:
            self.base_url = ""
            self.disconnect_calls = 0
            self.verify_session_calls = 0

        async def disconnect(self, *, close_http: bool = False, notify: bool = False) -> None:
            del close_http, notify
            self.disconnect_calls += 1

        async def verify_session(self) -> bool:
            self.verify_session_calls += 1
            return False

        def clear_session(self) -> None:
            return None

    async def async_noop(*args, **kwargs) -> None:
        del args, kwargs

    capabilities = {
        "protocol": "a0-connector.v1",
        "websocket_namespace": "/ws",
        "websocket_handlers": ["plugins/_a0_connector/ws_connector"],
        "auth": ["session"],
        "auth_required": True,
        "features": [],
    }
    client = ProtectedClient()
    dummy_app.client = client  # type: ignore[assignment]

    async def fetch_capabilities() -> tuple[dict[str, object], bool, str]:
        return capabilities, False, ""

    monkeypatch.setattr(dummy_app, "_fetch_capabilities", fetch_capabilities)
    monkeypatch.setattr(dummy_app, "_stop_remote_tree_publisher", lambda: None)
    monkeypatch.setattr(dummy_app, "_stop_token_refresh", lambda: None)
    monkeypatch.setattr(dummy_app, "_stop_state_sync", lambda: None)
    monkeypatch.setattr(dummy_app, "_clear_token_usage", lambda: None)
    monkeypatch.setattr(dummy_app, "_hide_project_menu", async_noop)
    monkeypatch.setattr(dummy_app, "_hide_profile_menu", async_noop)
    monkeypatch.setattr(dummy_app, "_clear_project_state", lambda: None)

    await connection.begin_connection(dummy_app, "http://localhost:5080")

    splash = dummy_app._test_widgets["#splash-view"]  # type: ignore[index]
    status = dummy_app._test_widgets["#connection-status"]  # type: ignore[index]
    input_widget = dummy_app._test_widgets["#message-input"]  # type: ignore[index]
    assert client.base_url == "http://localhost:5080"
    assert client.disconnect_calls == 1
    assert client.verify_session_calls == 1
    assert splash.state.stage == "login"
    assert splash.state.host == "http://localhost:5080"
    assert splash.state.login_error == ""
    assert status.status == "disconnected"
    assert input_widget.disabled is True


async def test_begin_connection_to_protected_instance_uses_environment_credentials(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class EnvironmentLoginClient:
        def __init__(self) -> None:
            self.base_url = ""
            self.disconnect_calls = 0
            self.verify_session_calls = 0
            self.login_calls: list[tuple[str, str]] = []
            self.clear_persisted_session_calls: list[str] = []
            self.subscribed_contexts: list[str] = []

        async def disconnect(self, *, close_http: bool = False, notify: bool = False) -> None:
            del close_http, notify
            self.disconnect_calls += 1

        async def verify_session(self) -> bool:
            self.verify_session_calls += 1
            return False

        async def login(self, username: str, password: str) -> bool:
            self.login_calls.append((username, password))
            return True

        def clear_session(self) -> None:
            return None

        def clear_persisted_session(self, host: str) -> None:
            self.clear_persisted_session_calls.append(host)

        async def connect_websocket(self) -> None:
            return None

        async def send_hello(self, **kwargs) -> dict[str, object]:
            del kwargs
            return {}

        async def create_chat(self) -> str:
            return "ctx-env"

        async def subscribe_context(self, context_id: str, **kwargs) -> dict[str, object]:
            del kwargs
            self.subscribed_contexts.append(context_id)
            return {}

    async def async_noop(*args, **kwargs) -> None:
        del args, kwargs

    capabilities = {
        "protocol": "a0-connector.v1",
        "websocket_namespace": "/ws",
        "websocket_handlers": ["plugins/_a0_connector/ws_connector"],
        "auth": ["session"],
        "auth_required": True,
        "features": [],
    }
    client = EnvironmentLoginClient()
    dummy_app.client = client  # type: ignore[assignment]

    async def fetch_capabilities() -> tuple[dict[str, object], bool, str]:
        return capabilities, False, ""

    monkeypatch.setenv("A0_USERNAME", " neo ")
    monkeypatch.setenv("A0_PASSWORD", "trinity")
    monkeypatch.setattr(dummy_app, "_fetch_capabilities", fetch_capabilities)
    monkeypatch.setattr(dummy_app, "_stop_remote_tree_publisher", lambda: None)
    monkeypatch.setattr(dummy_app, "_stop_token_refresh", lambda: None)
    monkeypatch.setattr(dummy_app, "_stop_state_sync", lambda: None)
    monkeypatch.setattr(dummy_app, "_clear_token_usage", lambda: None)
    monkeypatch.setattr(dummy_app, "_hide_project_menu", async_noop)
    monkeypatch.setattr(dummy_app, "_hide_profile_menu", async_noop)
    monkeypatch.setattr(dummy_app, "_clear_project_state", lambda: None)
    monkeypatch.setattr(dummy_app, "_refresh_remote_tool_metadata", async_noop)
    monkeypatch.setattr(dummy_app, "_refresh_goal_bar", async_noop)
    monkeypatch.setattr(dummy_app, "_refresh_model_switcher", async_noop)
    monkeypatch.setattr(dummy_app, "_refresh_settings_snapshot", async_noop)
    monkeypatch.setattr(dummy_app, "_refresh_projects", async_noop)
    monkeypatch.setattr(dummy_app, "_refresh_token_usage", async_noop)
    monkeypatch.setattr(dummy_app, "_start_state_sync", lambda: None)
    monkeypatch.setattr(dummy_app, "_start_token_refresh", lambda: None)
    monkeypatch.setattr(dummy_app, "_start_remote_tree_publisher", lambda: None)
    monkeypatch.setattr(dummy_app, "_sync_body_mode", lambda: None)
    monkeypatch.setattr(dummy_app, "_focus_message_input", lambda: None)
    monkeypatch.setattr(dummy_app, "_welcome_actions", lambda: ())

    await connection.begin_connection(dummy_app, "http://localhost:5080")

    splash = dummy_app._test_widgets["#splash-view"]  # type: ignore[index]
    status = dummy_app._test_widgets["#connection-status"]  # type: ignore[index]
    input_widget = dummy_app._test_widgets["#message-input"]  # type: ignore[index]
    assert client.login_calls == [("neo", "trinity")]
    assert client.verify_session_calls == 1
    assert client.subscribed_contexts == ["ctx-env"]
    assert splash.state.stage == "ready"
    assert splash.state.username == "neo"
    assert splash.state.password == ""
    assert status.status == "connected"
    assert input_widget.disabled is False


async def test_begin_connection_to_protected_instance_reuses_remembered_session(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RememberedSessionClient:
        def __init__(self) -> None:
            self.base_url = ""
            self.disconnect_calls = 0
            self.verify_session_calls = 0
            self.restore_session_calls: list[str] = []
            self.persist_session_calls: list[str] = []
            self.subscribed_contexts: list[str] = []

        async def disconnect(self, *, close_http: bool = False, notify: bool = False) -> None:
            del close_http, notify
            self.disconnect_calls += 1

        def restore_session(self, host: str) -> bool:
            self.restore_session_calls.append(host)
            return True

        def clear_session(self) -> None:
            return None

        def clear_persisted_session(self, host: str) -> None:
            del host

        def persist_session(self, host: str) -> None:
            self.persist_session_calls.append(host)

        async def verify_session(self) -> bool:
            self.verify_session_calls += 1
            return True

        async def connect_websocket(self) -> None:
            return None

        async def send_hello(self, **kwargs) -> dict[str, object]:
            del kwargs
            return {}

        async def create_chat(self) -> str:
            return "ctx-remembered"

        async def subscribe_context(self, context_id: str, **kwargs) -> dict[str, object]:
            del kwargs
            self.subscribed_contexts.append(context_id)
            return {}

    async def async_noop(*args, **kwargs) -> None:
        del args, kwargs

    capabilities = {
        "protocol": "a0-connector.v1",
        "websocket_namespace": "/ws",
        "websocket_handlers": ["plugins/_a0_connector/ws_connector"],
        "auth": ["session"],
        "auth_required": True,
        "features": [],
    }
    client = RememberedSessionClient()
    dummy_app.client = client  # type: ignore[assignment]

    async def fetch_capabilities() -> tuple[dict[str, object], bool, str]:
        return capabilities, False, ""

    monkeypatch.setattr(dummy_app, "_fetch_capabilities", fetch_capabilities)
    monkeypatch.setattr(dummy_app, "_stop_remote_tree_publisher", lambda: None)
    monkeypatch.setattr(dummy_app, "_stop_token_refresh", lambda: None)
    monkeypatch.setattr(dummy_app, "_stop_state_sync", lambda: None)
    monkeypatch.setattr(dummy_app, "_clear_token_usage", lambda: None)
    monkeypatch.setattr(dummy_app, "_hide_project_menu", async_noop)
    monkeypatch.setattr(dummy_app, "_hide_profile_menu", async_noop)
    monkeypatch.setattr(dummy_app, "_clear_project_state", lambda: None)
    monkeypatch.setattr(dummy_app, "_refresh_remote_tool_metadata", async_noop)
    monkeypatch.setattr(dummy_app, "_refresh_goal_bar", async_noop)
    monkeypatch.setattr(dummy_app, "_refresh_model_switcher", async_noop)
    monkeypatch.setattr(dummy_app, "_refresh_settings_snapshot", async_noop)
    monkeypatch.setattr(dummy_app, "_refresh_projects", async_noop)
    monkeypatch.setattr(dummy_app, "_refresh_token_usage", async_noop)
    monkeypatch.setattr(dummy_app, "_start_state_sync", lambda: None)
    monkeypatch.setattr(dummy_app, "_start_token_refresh", lambda: None)
    monkeypatch.setattr(dummy_app, "_start_remote_tree_publisher", lambda: None)
    monkeypatch.setattr(dummy_app, "_sync_body_mode", lambda: None)
    monkeypatch.setattr(dummy_app, "_focus_message_input", lambda: None)
    monkeypatch.setattr(dummy_app, "_welcome_actions", lambda: ())

    await connection.begin_connection(
        dummy_app,
        "http://localhost:5080",
        remember_host_flag=True,
    )

    splash = dummy_app._test_widgets["#splash-view"]  # type: ignore[index]
    status = dummy_app._test_widgets["#connection-status"]  # type: ignore[index]
    input_widget = dummy_app._test_widgets["#message-input"]  # type: ignore[index]
    assert client.restore_session_calls == ["http://localhost:5080"]
    assert client.verify_session_calls == 1
    assert client.persist_session_calls == ["http://localhost:5080"]
    assert client.subscribed_contexts == ["ctx-remembered"]
    assert splash.state.stage == "ready"
    assert status.status == "connected"
    assert input_widget.disabled is False


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


def test_context_code_event_strips_icon_heading_from_activity_lane(
    dummy_app: DummyAgentZeroCLI,
) -> None:
    dummy_app.connected = True
    dummy_app.current_context = "ctx-1"
    dummy_app.current_context_has_messages = True

    dummy_app._handle_context_event(
        {
            "context_id": "ctx-1",
            "event": "code_output",
            "sequence": 5,
            "data": {
                "heading": "icon://terminal [0] pytest -q icon://done_all",
                "text": "",
                "meta": {"runtime": "python"},
            },
        }
    )

    input_widget = dummy_app._test_widgets["#message-input"]  # type: ignore[index]
    assert input_widget.activity_label == "Running code"
    assert input_widget.activity_detail == "pytest -q"


def test_extract_detail_strips_icon_markers_from_status_and_tool_headings() -> None:
    assert (
        extract_detail(
            "status",
            {"meta": {"step": "icon://terminal [0] code_execution_tool - python"}},
        )
        == "code_execution_tool - python"
    )
    assert (
        extract_detail(
            "tool_start",
            {"heading": "icon://construction A0: Using tool 'browser'"},
        )
        == "A0: Using tool 'browser'"
    )


def test_context_snapshot_seeds_input_history_from_user_messages(
    dummy_app: DummyAgentZeroCLI,
) -> None:
    dummy_app.connected = True
    dummy_app.current_context = "ctx-1"
    dummy_app.current_context_has_messages = True

    dummy_app._handle_context_snapshot(
        {
            "context_id": "ctx-1",
            "events": [
                {
                    "event": "user_message",
                    "sequence": 1,
                    "data": {"text": "previous prompt"},
                },
                {
                    "event": "assistant_message",
                    "sequence": 2,
                    "data": {"text": "Hello"},
                },
            ],
        }
    )

    input_widget = dummy_app._test_widgets["#message-input"]  # type: ignore[index]
    assert input_widget.history_seeded == ["previous prompt"]


def test_context_snapshot_updates_message_queue(
    dummy_app: DummyAgentZeroCLI,
) -> None:
    dummy_app.connected = True
    dummy_app.current_context = "ctx-1"
    dummy_app.current_context_has_messages = True

    dummy_app._handle_context_snapshot(
        {
            "context_id": "ctx-1",
            "events": [],
            "message_queue": [
                {
                    "id": "item-1",
                    "seq": 1,
                    "text": "queued prompt",
                    "attachments": [],
                    "attachment_count": 0,
                }
            ],
        }
    )

    input_widget = dummy_app._test_widgets["#message-input"]  # type: ignore[index]
    queue_bar = dummy_app._test_widgets["#message-queue-bar"]  # type: ignore[index]
    assert dummy_app.message_queue[0]["id"] == "item-1"
    assert input_widget.queue_active is True
    assert queue_bar.display is True


def test_context_snapshot_tracks_older_history_cursor(
    dummy_app: DummyAgentZeroCLI,
) -> None:
    dummy_app.current_context = "ctx-1"

    dummy_app._handle_context_snapshot(
        {
            "context_id": "ctx-1",
            "events": [],
            "history_before": 150,
            "has_more_history": True,
        }
    )

    log = dummy_app._test_widgets["#chat-log"]  # type: ignore[index]
    assert log.history_pages == [(150, True)]


def test_message_queue_update_ignores_other_context(
    dummy_app: DummyAgentZeroCLI,
) -> None:
    dummy_app.current_context = "ctx-1"

    dummy_app._handle_message_queue_updated(
        {
            "context_id": "ctx-2",
            "message_queue": [{"id": "item-1", "text": "wrong context"}],
        }
    )

    assert dummy_app.message_queue == []


async def test_message_queue_bar_sits_directly_below_model_switcher() -> None:
    app = AgentZeroCLI(config=CLIConfig(instance_url="http://127.0.0.1:19999"), discover_instances=False)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.5)
        app.connected = True
        app.current_context_has_messages = True
        app.current_context = "ctx-1"
        app._sync_body_mode()
        model_bar = app.query_one("#model-switcher-bar", ModelSwitcherBar)
        model_bar.set_state(
            main_model={"provider": "codex", "name": "gpt-5.5"},
            presets=[],
            allowed=False,
        )
        app._set_message_queue(
            [{"id": "item-1", "seq": 1, "text": "queued prompt", "attachments": [], "attachment_count": 0}]
        )
        await pilot.pause(0.5)

        queue_bar = app.query_one("#message-queue-bar", MessageQueueBar)
        assert len(model_bar.query(".model-switcher-chip")) == 1
        assert str(model_bar._main_button.label) == "codex/gpt-5.5"
        assert queue_bar.region.y == model_bar.region.y + model_bar.region.height


async def test_composer_bars_stack_without_gaps() -> None:
    app = AgentZeroCLI(config=CLIConfig(instance_url="http://127.0.0.1:19999"), discover_instances=False)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.5)
        app.connected = True
        app.current_context_has_messages = True
        app.current_context = "ctx-1"
        app._sync_body_mode()
        goal_bar = app.query_one("#goal-bar", GoalBar)
        model_bar = app.query_one("#model-switcher-bar", ModelSwitcherBar)
        model_bar.set_state(
            main_model={"provider": "codex", "name": "gpt-5.5"},
            presets=[],
            allowed=False,
        )
        banner = app.query_one("#computer-use-banner", ComputerUseBanner)
        banner.set_state(enabled=True, status="active")
        app._sync_composer_bar_spacing()
        await pilot.pause(0.5)

        assert model_bar.region.y == banner.region.y + banner.region.height

        app._set_goal({"objective": "Ship goal support", "status": "active", "elapsed_seconds": 0})
        await pilot.pause(0.5)

        assert goal_bar.region.y == banner.region.y + banner.region.height
        assert goal_bar.region.height == 2
        assert model_bar.region.y == goal_bar.region.y + goal_bar.region.height
        assert str(goal_bar._update.label) == "✎ Edit"
        assert str(goal_bar._pause_resume.label) == "Ⅱ Pause"
        assert str(goal_bar._delete.label) == "× Delete"
        assert goal_bar._summary.render().plain.startswith("● Goal · ")

        goal_bar.set_goal({"objective": "Ship goal support", "status": "paused"})
        assert str(goal_bar._pause_resume.label) == "▶ Resume"
        assert goal_bar._summary.render().plain.startswith("● Goal paused · ")


def test_chat_input_activity_placeholder_renders_detail_literally() -> None:
    input_widget = ChatInput()

    input_widget.set_activity("Using tool", "[/a0/tests/test_a0_connector_prompt_gating.py]")

    # [ in the detail is escaped to \[ so Rich/Textual never interprets it as a
    # markup tag; the outer \[ wrapper similarly avoids [/...] being read as a
    # closing tag (issue #13).
    assert input_widget.placeholder == (
        "|>  Using tool \\[\\[/a0/tests/test_a0_connector_prompt_gating.py]]"
    )
    assert "[dim]" not in input_widget.placeholder


def test_chat_input_activity_placeholder_bare_path_is_markup_safe() -> None:
    """Bare paths in the detail (no brackets) must not produce a [/...] closing tag."""
    input_widget = ChatInput()

    input_widget.set_activity("Running code", "/a0/usr/workdir/process_aws_health_event")

    assert input_widget.placeholder == (
        "|>  Running code \\[/a0/usr/workdir/process_aws_health_event]"
    )


def test_chat_input_activity_placeholder_shell_expr_is_markup_safe() -> None:
    """Shell expressions with Rich-like syntax must not raise MarkupError."""
    input_widget = ChatInput()

    input_widget.set_activity("Running code", "decision=$(sed -n 's/^DECISION: //p' <<<")

    assert input_widget.placeholder == (
        "|>  Running code \\[decision=$(sed -n 's/^DECISION: //p' <<<]"
    )


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


def test_context_event_missing_chat_log_is_ignored(
    dummy_app: DummyAgentZeroCLI,
) -> None:
    dummy_app.connected = True
    dummy_app.current_context = "ctx-1"

    original_query_one = dummy_app.query_one

    def _query_one(selector: object, cls: object = None) -> object:
        if selector == "#chat-log":
            raise NoMatches("No nodes match '#chat-log'")
        return original_query_one(selector, cls)

    dummy_app.query_one = _query_one  # type: ignore[method-assign]

    dummy_app._handle_context_event(
        {
            "context_id": "ctx-1",
            "event": "info",
            "sequence": 11,
            "data": {"text": "[/a0/tests/test_a0_connector_prompt_gating.py]"},
        }
    )

    assert dummy_app.rendered_events == []


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
    dummy_app.agent_active = True

    unsubscribed: list[str] = []
    subscribed: list[tuple[str, int, str | None]] = []
    remembered: list[str] = []
    published: list[bool] = []

    async def async_noop(*args, **kwargs) -> None:
        del args, kwargs

    async def fake_unsubscribe_context(context_id: str) -> None:
        unsubscribed.append(context_id)

    async def fake_subscribe_context(
        context_id: str,
        from_seq: int = 0,
        *,
        history: str | None = None,
    ) -> None:
        subscribed.append((context_id, from_seq, history))

    async def fake_publish_remote_tree_snapshot(*, force: bool = False) -> None:
        published.append(force)

    monkeypatch.setattr(dummy_app, "_stop_token_refresh", lambda: None)
    monkeypatch.setattr(dummy_app, "_hide_project_menu", async_noop)
    monkeypatch.setattr(dummy_app, "_hide_profile_menu", async_noop)
    monkeypatch.setattr(dummy_app.client, "unsubscribe_context", fake_unsubscribe_context)
    monkeypatch.setattr(dummy_app.client, "subscribe_context", fake_subscribe_context)
    monkeypatch.setattr(dummy_app, "_remember_context", lambda context_id: remembered.append(context_id))
    monkeypatch.setattr(dummy_app, "_publish_remote_tree_snapshot", fake_publish_remote_tree_snapshot)
    monkeypatch.setattr(dummy_app, "_refresh_projects", async_noop)
    monkeypatch.setattr(dummy_app, "_refresh_goal_bar", async_noop)
    monkeypatch.setattr(dummy_app, "_refresh_model_switcher", async_noop)
    monkeypatch.setattr(dummy_app, "_refresh_token_usage", async_noop)
    monkeypatch.setattr(dummy_app, "_start_token_refresh", lambda: None)

    await dummy_app._switch_context("ctx-2", has_messages_hint=True)

    assert unsubscribed == ["ctx-old"]
    assert subscribed == [("ctx-2", 0, "tail")]
    assert published == [True]
    assert remembered == ["ctx-2"]
    assert dummy_app.current_context == "ctx-2"
    assert dummy_app.current_context_has_messages is True
    assert dummy_app.agent_active is False


def test_context_tab_metadata_updates_active_tab(dummy_app: DummyAgentZeroCLI) -> None:
    dummy_app.connected = True
    dummy_app.connector_features = {"chat_create"}
    dummy_app.current_context = "ctx-alpha"

    dummy_app._remember_context_tab(
        "ctx-alpha",
        {
            "id": "ctx-alpha",
            "name": "",
            "no": 45,
            "last_message": "ignored",
            "project": {"name": "project-1", "color": "#12ABEF"},
        },
        has_messages_hint=True,
    )

    tab_strip = dummy_app._test_widgets["#context-tabs"]  # type: ignore[index]
    assert tab_strip.display is True
    assert tab_strip.active_context_id == "ctx-alpha"
    assert tab_strip.can_create is True
    assert tab_strip.tabs == (ContextTab("ctx-alpha", "Chat #45", True, "#12abef"),)


def test_context_tab_metadata_uses_webui_name_rule() -> None:
    named = context_tab_from_metadata(
        {"id": "ctx-alpha", "name": "Initial Greeting", "no": 45},
        index=1,
    )
    numbered = context_tab_from_metadata(
        {"id": "ctx-beta", "name": "", "no": 44, "last_message": "2026-06-01T12:00:00"},
        index=2,
    )

    assert named.label == "Initial Greeting"
    assert numbered.label == "Chat #44"
    assert numbered.has_messages is False


async def test_context_complete_refreshes_active_tab_metadata(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refreshed: list[tuple[str, bool]] = []

    async def fake_refresh_token_usage(*args, **kwargs) -> None:
        del args, kwargs

    async def fake_refresh_context_tab_metadata(
        context_id: str,
        *,
        has_messages_hint: bool = False,
    ) -> None:
        refreshed.append((context_id, has_messages_hint))
        dummy_app._remember_context_tab(
            context_id,
            {"id": context_id, "name": "Renamed Chat", "no": 1},
            has_messages_hint=has_messages_hint,
        )

    dummy_app.connected = True
    dummy_app.current_context = "ctx-alpha"
    dummy_app._context_tabs = [ContextTab("ctx-alpha", "Chat #1", True)]

    monkeypatch.setattr(dummy_app, "_refresh_token_usage", fake_refresh_token_usage)
    monkeypatch.setattr(dummy_app, "_refresh_goal_bar", fake_refresh_token_usage)
    monkeypatch.setattr(dummy_app, "_refresh_context_tab_metadata", fake_refresh_context_tab_metadata)

    event_handlers.handle_context_complete(dummy_app, {"context_id": "ctx-alpha"})
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert refreshed == [("ctx-alpha", True)]
    assert dummy_app._context_tabs == [ContextTab("ctx-alpha", "Renamed Chat", True)]


async def test_context_complete_surfaces_response_without_assistant_message(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def async_noop(*args, **kwargs) -> None:
        del args, kwargs

    dummy_app.connected = True
    dummy_app.current_context = "ctx-alpha"
    dummy_app.current_context_has_messages = True
    monkeypatch.setattr(dummy_app, "_refresh_token_usage", async_noop)
    monkeypatch.setattr(dummy_app, "_refresh_goal_bar", async_noop)
    monkeypatch.setattr(dummy_app, "_refresh_context_tab_metadata", async_noop)

    event_handlers.handle_context_complete(
        dummy_app,
        {"context_id": "ctx-alpha", "response": "Command completed."},
    )
    await asyncio.sleep(0)

    log = dummy_app._test_widgets["#chat-log"]  # type: ignore[index]
    assert log.writes == ["Command completed."]
    assert dummy_app._response_delivered is True


async def test_context_tabs_render_in_textual() -> None:
    app = ContextTabsRenderApp()

    async with app.run_test(size=(80, 4)) as pilot:
        await pilot.pause(delay=0.1)
        tab_strip = app.query_one("#context-tabs", ContextTabs)
        screenshot = app.export_screenshot()

    assert "Architecture" in screenshot
    assert "Streaming" in screenshot
    assert "[×]" in screenshot
    assert len(tab_strip._close_spans) == 2


async def test_context_tabs_x_requests_close_for_active_tab() -> None:
    app = ContextTabsRenderApp()

    async with app.run_test(size=(80, 4)) as pilot:
        await pilot.press("tab")
        await pilot.press("x")
        await pilot.pause(delay=0.1)

    assert app.close_events == [("ctx-alpha", "ctx-beta")]


async def test_context_tab_close_glyph_requests_close_for_clicked_tab() -> None:
    app = ContextTabsRenderApp()
    stopped: list[bool] = []

    async with app.run_test(size=(80, 4)) as pilot:
        await pilot.pause(delay=0.1)
        tab_strip = app.query_one("#context-tabs", ContextTabs)
        close_start, _, clicked_context = tab_strip._close_spans[1]
        event = SimpleNamespace(
            get_content_offset=lambda _: SimpleNamespace(x=close_start, y=0),
            stop=lambda: stopped.append(True),
        )

        tab_strip.on_click(event)
        await pilot.pause(delay=0.1)

    assert clicked_context == "ctx-beta"
    assert stopped == [True]
    assert app.selected_events == []
    assert app.close_events == [("ctx-beta", "ctx-alpha")]


async def test_context_tabs_x_ignores_only_visible_tab() -> None:
    app = ContextTabsRenderApp()

    async with app.run_test(size=(80, 4)) as pilot:
        app.query_one("#context-tabs", ContextTabs).set_tabs(
            [ContextTab("ctx-alpha", "Architecture sketch", True)],
            "ctx-alpha",
            can_create=True,
        )
        await pilot.press("tab")
        await pilot.press("x")
        await pilot.pause(delay=0.1)

    assert app.close_events == []


async def test_context_tab_switch_uses_stored_message_hint(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dummy_app.current_context = "ctx-alpha"
    dummy_app._context_tabs = [
        ContextTab("ctx-alpha", "Alpha", True),
        ContextTab("ctx-beta", "Beta", True),
    ]
    switches: list[tuple[str, bool]] = []

    async def fake_switch_context(context_id: str, *, has_messages_hint: bool) -> None:
        switches.append((context_id, has_messages_hint))

    monkeypatch.setattr(dummy_app, "_switch_context", fake_switch_context)

    await dummy_app._switch_context_from_tab("ctx-beta")

    assert switches == [("ctx-beta", True)]


async def test_closing_active_context_tab_hides_tab_and_switches_to_neighbor(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dummy_app.connected = True
    dummy_app.connector_features = {"chat_create"}
    dummy_app.current_context = "ctx-alpha"
    dummy_app._context_tabs = [
        ContextTab("ctx-alpha", "Alpha", True),
        ContextTab("ctx-beta", "Beta", True),
        ContextTab("ctx-gamma", "Gamma", False),
    ]
    switches: list[str] = []

    async def fake_switch_context_from_tab(context_id: str) -> None:
        switches.append(context_id)

    monkeypatch.setattr(dummy_app, "_switch_context_from_tab", fake_switch_context_from_tab)

    await dummy_app._close_context_tab("ctx-alpha", replacement_context_id="ctx-beta")

    tab_strip = dummy_app._test_widgets["#context-tabs"]  # type: ignore[index]
    assert tab_strip.tabs == (
        ContextTab("ctx-beta", "Beta", True),
        ContextTab("ctx-gamma", "Gamma", False),
    )
    assert switches == ["ctx-beta"]


async def test_closing_only_context_tab_is_ignored(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dummy_app.connected = True
    dummy_app.current_context = "ctx-alpha"
    dummy_app._context_tabs = [ContextTab("ctx-alpha", "Alpha", True)]
    dummy_app._sync_context_tabs()
    switches: list[str] = []

    async def fake_switch_context_from_tab(context_id: str) -> None:
        switches.append(context_id)

    monkeypatch.setattr(dummy_app, "_switch_context_from_tab", fake_switch_context_from_tab)

    await dummy_app._close_context_tab("ctx-alpha")

    tab_strip = dummy_app._test_widgets["#context-tabs"]  # type: ignore[index]
    input_widget = dummy_app._test_widgets["#message-input"]  # type: ignore[index]
    assert dummy_app._context_tabs == [ContextTab("ctx-alpha", "Alpha", True)]
    assert tab_strip.tabs == (ContextTab("ctx-alpha", "Alpha", True),)
    assert tab_strip.display is True
    assert switches == []
    assert input_widget.focused is False


async def test_remote_tree_snapshot_resends_unchanged_tree_before_backend_expiry(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dummy_app.connected = True
    snapshot = RemoteTreeSnapshot(
        root_path="/workspace",
        tree="/workspace/\n└── pyproject.toml",
        tree_hash="tree-hash",
        generated_at="2026-05-15T00:00:00+00:00",
    )
    sent: list[dict[str, object]] = []

    async def fake_send_remote_tree_update(payload: dict[str, object]) -> dict[str, object]:
        sent.append(payload)
        return {"accepted": True}

    monotonic_values = iter([100.0, 120.0, 161.0, 162.0])

    monkeypatch.setattr(dummy_app._remote_files, "build_tree_snapshot", lambda: snapshot)
    monkeypatch.setattr(dummy_app.client, "send_remote_tree_update", fake_send_remote_tree_update)
    monkeypatch.setattr(event_handlers, "monotonic", lambda: next(monotonic_values))

    await dummy_app._publish_remote_tree_snapshot()
    await dummy_app._publish_remote_tree_snapshot()
    await dummy_app._publish_remote_tree_snapshot()
    await dummy_app._publish_remote_tree_snapshot(force=True)

    assert [payload["tree_hash"] for payload in sent] == [
        "tree-hash",
        "tree-hash",
        "tree-hash",
    ]
    assert dummy_app._last_remote_tree_published_at == 162.0


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


async def test_resolve_initial_context_prefers_configured_default_chat(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dummy_app.config.default_context_id = "ctx-default"
    dummy_app.config.last_context_id = "ctx-saved"
    dummy_app.config.last_context_host = "http://example.test"
    dummy_app.connector_features = {"chat_get"}

    async def fake_list_chats() -> list[dict[str, object]]:
        return [{"id": "ctx-saved"}, {"id": "ctx-default"}]

    async def fake_get_chat(context_id: str) -> dict[str, object]:
        assert context_id == "ctx-default"
        return {"last_message": "hello"}

    async def fail_create_chat(*args, **kwargs) -> str:
        del args, kwargs
        raise AssertionError("create_chat should not run when the default context exists")

    monkeypatch.setattr(dummy_app.client, "list_chats", fake_list_chats)
    monkeypatch.setattr(dummy_app.client, "get_chat", fake_get_chat)
    monkeypatch.setattr(dummy_app.client, "create_chat", fail_create_chat)

    context_id, has_messages_hint = await connection._resolve_initial_context(
        dummy_app,
        "http://example.test",
    )

    assert context_id == "ctx-default"
    assert has_messages_hint is True


async def test_resolve_initial_context_uses_configured_default_without_chat_list(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dummy_app.config.default_context_id = "ctx-direct"
    dummy_app.connector_features = set()

    async def fail_list_chats(*args, **kwargs) -> list[dict[str, object]]:
        del args, kwargs
        raise AssertionError("list_chats should not run for an explicit default context")

    async def fail_create_chat(*args, **kwargs) -> str:
        del args, kwargs
        raise AssertionError("create_chat should not run for an explicit default context")

    monkeypatch.setattr(dummy_app.client, "list_chats", fail_list_chats)
    monkeypatch.setattr(dummy_app.client, "create_chat", fail_create_chat)

    context_id, has_messages_hint = await connection._resolve_initial_context(
        dummy_app,
        "http://example.test",
    )

    assert context_id == "ctx-direct"
    assert has_messages_hint is False


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


async def test_active_run_submission_is_added_to_message_queue(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dummy_app.connected = True
    dummy_app.current_context = "ctx-1"
    dummy_app.current_context_has_messages = True
    dummy_app.agent_active = True
    dummy_app.connector_features = {"message_queue"}

    calls: list[tuple[str, str | None, list[str] | None]] = []

    async def fake_add_message_to_queue(
        text: str,
        context_id: str | None,
        attachments: list[str] | None = None,
    ) -> dict[str, object]:
        calls.append((text, context_id, attachments))
        return {
            "message_queue": [
                {"id": "item-1", "seq": 1, "text": text, "attachments": [], "attachment_count": 0}
            ]
        }

    monkeypatch.setattr(dummy_app.client, "add_message_to_queue", fake_add_message_to_queue)

    input_widget = dummy_app._test_widgets["#message-input"]  # type: ignore[index]
    await dummy_app.on_chat_input_submitted(
        ChatInput.Submitted(value="draft follow-up", input=input_widget)
    )

    assert calls == [("draft follow-up", "ctx-1", [])]
    assert input_widget.value == ""
    assert dummy_app.agent_active is True
    assert dummy_app._response_delivered is False
    assert dummy_app._context_run_complete is False
    assert dummy_app.message_queue[0]["text"] == "draft follow-up"
    assert input_widget.queue_active is True


async def test_empty_submission_sends_queued_messages(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dummy_app.connected = True
    dummy_app.current_context = "ctx-1"
    dummy_app.current_context_has_messages = True
    dummy_app.connector_features = {"message_queue"}
    dummy_app._set_message_queue(
        [{"id": "item-1", "seq": 1, "text": "queued prompt", "attachments": [], "attachment_count": 0}]
    )

    calls: list[tuple[str | None, bool]] = []

    async def fake_send_message_queue(
        context_id: str | None,
        *,
        item_id: str | None = None,
        send_all: bool = True,
    ) -> dict[str, object]:
        calls.append((context_id, send_all))
        assert item_id is None
        return {"sent_count": 1, "message_queue": []}

    monkeypatch.setattr(dummy_app.client, "send_message_queue", fake_send_message_queue)

    input_widget = dummy_app._test_widgets["#message-input"]  # type: ignore[index]
    await dummy_app.on_chat_input_submitted(ChatInput.Submitted(value="", input=input_widget))

    assert calls == [("ctx-1", True)]
    assert dummy_app.message_queue == []
    assert input_widget.queue_active is False
    assert dummy_app.agent_active is True


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


async def test_chat_submission_refreshes_remote_tool_metadata_before_send(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dummy_app.connected = True
    dummy_app.client.connected = True
    dummy_app.current_context = "ctx-1"
    dummy_app._host_browser.enabled = True
    dummy_app._host_browser.remote_debugging = True
    calls: list[tuple[str, object]] = []

    async def fake_send_hello(
        *,
        context_id: str | None = None,
        computer_use: dict[str, object] | None = None,
        host_browser: dict[str, object] | None = None,
        remote_files: dict[str, object] | None = None,
        remote_exec: dict[str, object] | None = None,
    ) -> dict[str, object]:
        del computer_use, remote_files, remote_exec
        calls.append(("hello", {"context_id": context_id, "host_browser": dict(host_browser or {})}))
        return {"exec_config": {"version": 1}}

    async def fake_send_message(
        text: str,
        context_id: str | None,
        attachments: list[str] | None = None,
    ) -> None:
        calls.append(("message", (text, context_id, attachments)))

    async def fake_publish_remote_tree_snapshot(*, force: bool = False) -> None:
        calls.append(("tree", {"force": force}))

    dummy_app.client.send_hello = fake_send_hello  # type: ignore[method-assign]
    monkeypatch.setattr(dummy_app.client, "send_message", fake_send_message)
    monkeypatch.setattr(dummy_app, "_publish_remote_tree_snapshot", fake_publish_remote_tree_snapshot)
    input_widget = dummy_app._test_widgets["#message-input"]  # type: ignore[index]

    await dummy_app.on_chat_input_submitted(
        ChatInput.Submitted(value="visit LinkedIn in my browser", input=input_widget)
    )

    assert calls[0] == (
        "hello",
        {
            "context_id": "ctx-1",
            "host_browser": {
                "supported": True,
                "enabled": True,
                "status": "ready",
                "browser_family": "chrome-cdp",
                "profile_label": "localhost:9222",
                "features": ["open", "content"],
                "support_reason": "",
            },
        },
    )
    assert calls[1] == ("tree", {"force": True})
    assert calls[2] == ("message", ("visit LinkedIn in my browser", "ctx-1", []))


async def test_remote_tool_op_handlers_return_before_metadata_refresh(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_if_refreshed() -> None:
        raise AssertionError("metadata refresh should happen after result emission")

    monkeypatch.setattr(dummy_app, "_refresh_remote_tool_metadata", fail_if_refreshed)

    computer_use_result = await event_handlers.handle_computer_use_op(
        dummy_app,
        {"op_id": "cu-1", "action": "start_session", "context_id": "ctx-1"},
    )
    browser_result = await event_handlers.handle_browser_op(
        dummy_app,
        {"op_id": "browser-1", "action": "open", "context_id": "ctx-1"},
    )

    assert computer_use_result == {"op_id": "cu-1", "ok": True, "result": {"status": "active"}}
    assert browser_result == {"op_id": "browser-1", "ok": True, "result": {"status": "ready"}}


async def test_attach_clipboard_image_adds_pending_attachment(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    notices: list[tuple[str, bool]] = []
    upload = AttachmentUpload(
        filename="clipboard-local.png",
        content=b"png-bytes",
        mime_type="image/png",
    )
    attachment = AttachmentRef(
        path="/a0/usr/uploads/clipboard.png",
        name="clipboard.png",
        mime_type="image/png",
    )

    monkeypatch.setattr(
        "agent_zero_cli.app.create_clipboard_image_upload",
        lambda: upload,
    )

    async def fake_upload_attachments(uploads: list[AttachmentUpload]) -> list[AttachmentRef]:
        assert uploads == [upload]
        return [attachment]

    monkeypatch.setattr(dummy_app.client, "upload_attachments", fake_upload_attachments)
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


async def test_attach_command_uploads_local_image_paths(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dummy_app.connected = True
    dummy_app.current_context = "ctx-1"
    notices: list[tuple[str, bool]] = []
    requested_paths: list[str] = []
    upload_a = AttachmentUpload("first.png", b"first", "image/png")
    upload_b = AttachmentUpload("second.webp", b"second", "image/webp")
    ref_a = AttachmentRef("/a0/usr/uploads/first.png", "first.png", "image/png")
    ref_b = AttachmentRef("/a0/usr/uploads/second.webp", "second.webp", "image/webp")

    def fake_create_image_file_upload(path: str) -> AttachmentUpload:
        requested_paths.append(path)
        return upload_a if len(requested_paths) == 1 else upload_b

    async def fake_upload_attachments(uploads: list[AttachmentUpload]) -> list[AttachmentRef]:
        assert uploads == [upload_a, upload_b]
        return [ref_a, ref_b]

    monkeypatch.setattr(
        "agent_zero_cli.chat_commands.create_image_file_upload",
        fake_create_image_file_upload,
    )
    monkeypatch.setattr(dummy_app.client, "upload_attachments", fake_upload_attachments)
    monkeypatch.setattr(
        dummy_app,
        "_show_notice",
        lambda message, *, error=False: notices.append((message, error)),
    )

    await dummy_app._dispatch_command('/attach "/tmp/first image.png" /tmp/second.webp')

    input_widget = dummy_app._test_widgets["#message-input"]  # type: ignore[index]
    assert requested_paths == ["/tmp/first image.png", "/tmp/second.webp"]
    assert input_widget.attachments == [ref_a, ref_b]
    assert notices == [("Attached 2 images.", False)]


async def test_clear_command_clears_visible_chat_log(
    dummy_app: DummyAgentZeroCLI,
) -> None:
    log = dummy_app._test_widgets["#chat-log"]  # type: ignore[index]
    input_widget = dummy_app._test_widgets["#message-input"]  # type: ignore[index]
    input_widget.set_activity("Working")

    await dummy_app._dispatch_command("/clear")

    assert log.cleared is True
    assert input_widget.activity_idle is True


async def test_goal_command_sets_goal_and_sends_objective(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dummy_app.connected = True
    dummy_app.current_context = "ctx-1"
    calls: list[tuple[str, str, dict[str, object]]] = []
    sent: list[str] = []

    async def fake_goal_action(action: str, context_id: str, **payload: object) -> dict[str, object]:
        calls.append((action, context_id, dict(payload)))
        return {
            "ok": True,
            "goal": {"objective": str(payload.get("objective") or ""), "status": "active"},
        }

    async def fake_send_chat_text(text: str, **kwargs: object) -> None:
        del kwargs
        sent.append(text)

    monkeypatch.setattr(dummy_app.client, "goal_action", fake_goal_action)
    monkeypatch.setattr(dummy_app, "_send_chat_text", fake_send_chat_text)
    monkeypatch.setattr(dummy_app, "_show_notice", lambda *args, **kwargs: None)

    await dummy_app._dispatch_command("/goal Find the weak spots")

    assert calls == [("create", "ctx-1", {"objective": "Find the weak spots", "created_by": "user"})]
    assert sent == ["Find the weak spots"]
    assert dummy_app.goal == {"objective": "Find the weak spots", "status": "active"}


async def test_goal_command_update_and_delete_do_not_send_message(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dummy_app.connected = True
    dummy_app.current_context = "ctx-1"
    calls: list[tuple[str, dict[str, object]]] = []
    sent: list[str] = []

    async def fake_goal_action(action: str, context_id: str, **payload: object) -> dict[str, object]:
        assert context_id == "ctx-1"
        calls.append((action, dict(payload)))
        goal = None if action == "delete" else {"objective": payload.get("objective"), "status": "active"}
        return {"ok": True, "goal": goal, "reactivated": False}

    async def fake_send_chat_text(text: str, **kwargs: object) -> None:
        del kwargs
        sent.append(text)

    monkeypatch.setattr(dummy_app.client, "goal_action", fake_goal_action)
    monkeypatch.setattr(dummy_app, "_send_chat_text", fake_send_chat_text)
    monkeypatch.setattr(dummy_app, "_show_notice", lambda *args, **kwargs: None)

    await dummy_app._dispatch_command("/goal update Ship the CLI row")
    await dummy_app._dispatch_command("/goal delete")

    assert calls == [
        ("update", {"objective": "Ship the CLI row", "status": "active"}),
        ("delete", {}),
    ]
    assert sent == []
    assert dummy_app.goal is None


async def test_goal_command_reactivated_update_sends_objective(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dummy_app.connected = True
    dummy_app.current_context = "ctx-1"
    sent: list[str] = []

    async def fake_goal_action(*args: object, **kwargs: object) -> dict[str, object]:
        del args, kwargs
        return {
            "ok": True,
            "goal": {"objective": "Continue the goal", "status": "active"},
            "reactivated": True,
        }

    async def fake_send_chat_text(text: str, **kwargs: object) -> None:
        del kwargs
        sent.append(text)

    monkeypatch.setattr(dummy_app.client, "goal_action", fake_goal_action)
    monkeypatch.setattr(dummy_app, "_send_chat_text", fake_send_chat_text)
    monkeypatch.setattr(dummy_app, "_show_notice", lambda *args, **kwargs: None)

    await dummy_app._dispatch_command("/goal update Continue the goal")

    assert sent == ["Continue the goal"]


def test_goal_bar_update_button_prefills_update_command(dummy_app: DummyAgentZeroCLI) -> None:
    dummy_app._set_goal({"objective": "Ship the CLI row", "status": "active"})

    dummy_app.on_goal_bar_update_requested(GoalBar.UpdateRequested(GoalBar()))

    input_widget = dummy_app._test_widgets["#message-input"]  # type: ignore[index]
    assert input_widget.value == "/goal update Ship the CLI row"
    assert input_widget.focused is True


def test_attach_command_token_does_not_auto_open_slash_palette(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[str] = []
    monkeypatch.setattr(
        dummy_app,
        "_open_command_palette",
        lambda *, initial_query="", from_slash=False: opened.append(initial_query),
    )

    input_widget = dummy_app._test_widgets["#message-input"]  # type: ignore[index]
    dummy_app.on_chat_input_value_changed(
        ChatInput.ValueChanged(value="/attach", input=input_widget)
    )

    assert opened == []


def test_mid_input_attach_command_token_does_not_auto_open_slash_palette(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[str] = []
    monkeypatch.setattr(
        dummy_app,
        "_open_command_palette",
        lambda *, initial_query="", from_slash=False: opened.append(initial_query),
    )

    input_widget = dummy_app._test_widgets["#message-input"]  # type: ignore[index]
    dummy_app.on_chat_input_value_changed(
        ChatInput.ValueChanged(value="please add example /attach", input=input_widget)
    )

    assert opened == []


def test_pause_and_nudge_tokens_auto_open_slash_palette(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        dummy_app,
        "_open_command_palette",
        lambda *, initial_query="", from_slash=False: opened.append((initial_query, from_slash)),
    )

    input_widget = dummy_app._test_widgets["#message-input"]  # type: ignore[index]
    dummy_app.on_chat_input_value_changed(
        ChatInput.ValueChanged(value="/pause", input=input_widget)
    )
    dummy_app.on_chat_input_value_changed(
        ChatInput.ValueChanged(value="/nudge", input=input_widget)
    )

    assert opened == [("/pause", True), ("/nudge", True)]


def test_mid_input_slash_tokens_auto_open_slash_palette(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        dummy_app,
        "_open_command_palette",
        lambda *, initial_query="", from_slash=False: opened.append((initial_query, from_slash)),
    )

    input_widget = dummy_app._test_widgets["#message-input"]  # type: ignore[index]
    for value in (
        "example /",
        "example / ",
        "example /pause",
        "line one\n/pause",
    ):
        dummy_app.on_chat_input_value_changed(
            ChatInput.ValueChanged(value=value, input=input_widget)
        )

    assert opened == [
        ("/", True),
        ("/", True),
        ("/pause", True),
        ("/pause", True),
    ]


def test_mid_input_slash_tokens_require_token_boundary(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[str] = []
    monkeypatch.setattr(
        dummy_app,
        "_open_command_palette",
        lambda *, initial_query="", from_slash=False: opened.append(initial_query),
    )

    input_widget = dummy_app._test_widgets["#message-input"]  # type: ignore[index]
    for value in (
        "example/",
        "https://example.test/",
        "example /pause ",
        "example /pause later",
    ):
        dummy_app.on_chat_input_value_changed(
            ChatInput.ValueChanged(value=value, input=input_widget)
        )

    assert opened == []


def test_computer_use_token_auto_opens_main_slash_palette_but_aliases_stay_hidden(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        dummy_app,
        "_open_command_palette",
        lambda *, initial_query="", from_slash=False: opened.append((initial_query, from_slash)),
    )

    input_widget = dummy_app._test_widgets["#message-input"]  # type: ignore[index]
    for value in ("/computer-use", "/computer", "/cu"):
        dummy_app.on_chat_input_value_changed(
            ChatInput.ValueChanged(value=value, input=input_widget)
        )

    assert opened == [("/computer-use", True)]


def test_bare_slash_auto_opens_command_palette(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        dummy_app,
        "_open_command_palette",
        lambda *, initial_query="", from_slash=False: opened.append((initial_query, from_slash)),
    )

    input_widget = dummy_app._test_widgets["#message-input"]  # type: ignore[index]
    dummy_app.on_chat_input_value_changed(
        ChatInput.ValueChanged(value="/", input=input_widget)
    )
    dummy_app.on_chat_input_value_changed(
        ChatInput.ValueChanged(value="/ ", input=input_widget)
    )

    assert opened == [("/", True), ("/", True)]


def test_raw_skill_command_detection() -> None:
    assert is_raw_skill_command("$a0-live-e2e-tester") is True
    assert is_raw_skill_command("$imagegen make a texture") is True
    assert is_raw_skill_command("$") is False
    assert is_raw_skill_command("$100") is False
    assert is_raw_skill_command("please use $imagegen") is False


def test_bare_dollar_auto_opens_skill_palette(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dummy_app.connected = True
    dummy_app.current_context = "ctx-1"
    dummy_app.connector_features = {"skills_list", "skills_activate"}
    opened: list[tuple[str, bool, bool]] = []
    monkeypatch.setattr(
        dummy_app,
        "_open_command_palette",
        lambda *, initial_query="", from_slash=False, from_skill=False: opened.append(
            (initial_query, from_slash, from_skill)
        ),
    )

    input_widget = dummy_app._test_widgets["#message-input"]  # type: ignore[index]
    dummy_app.on_chat_input_value_changed(
        ChatInput.ValueChanged(value="$", input=input_widget)
    )
    dummy_app.on_chat_input_value_changed(
        ChatInput.ValueChanged(value="$a0-live", input=input_widget)
    )
    dummy_app.on_chat_input_value_changed(
        ChatInput.ValueChanged(value="$a0-live ", input=input_widget)
    )

    assert opened == [("$", False, True), ("$a0-live", False, True)]


def test_mid_input_dollar_tokens_auto_open_skill_palette(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dummy_app.connected = True
    dummy_app.current_context = "ctx-1"
    dummy_app.connector_features = {"skills_list", "skills_activate"}
    opened: list[tuple[str, bool, bool]] = []
    monkeypatch.setattr(
        dummy_app,
        "_open_command_palette",
        lambda *, initial_query="", from_slash=False, from_skill=False: opened.append(
            (initial_query, from_slash, from_skill)
        ),
    )

    input_widget = dummy_app._test_widgets["#message-input"]  # type: ignore[index]
    for value in (
        "example $",
        "example $ ",
        "example $a0-live",
        "line one\n$a0-live",
    ):
        dummy_app.on_chat_input_value_changed(
            ChatInput.ValueChanged(value=value, input=input_widget)
        )

    assert opened == [
        ("$", False, True),
        ("$", False, True),
        ("$a0-live", False, True),
        ("$a0-live", False, True),
    ]


def test_mid_input_dollar_tokens_require_token_boundary_and_skill_shape(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dummy_app.connected = True
    dummy_app.current_context = "ctx-1"
    dummy_app.connector_features = {"skills_list", "skills_activate"}
    opened: list[str] = []
    monkeypatch.setattr(
        dummy_app,
        "_open_command_palette",
        lambda *, initial_query="", from_slash=False, from_skill=False: opened.append(initial_query),
    )

    input_widget = dummy_app._test_widgets["#message-input"]  # type: ignore[index]
    for value in (
        "example$",
        "price is $100",
        "example $a0-live ",
        "example $a0-live now",
    ):
        dummy_app.on_chat_input_value_changed(
            ChatInput.ValueChanged(value=value, input=input_widget)
        )

    assert opened == []


def test_command_palette_close_removes_mid_input_trigger_token(
    dummy_app: DummyAgentZeroCLI,
) -> None:
    input_widget = dummy_app._test_widgets["#message-input"]  # type: ignore[index]

    input_widget.value = "example /pause"
    dummy_app._slash_palette_query = "/pause"
    dummy_app.on_command_palette_closed(SimpleNamespace())
    assert input_widget.value == "example "

    input_widget.value = "example $a0-live  "
    dummy_app._slash_palette_query = "$a0-live"
    dummy_app.on_command_palette_closed(SimpleNamespace())
    assert input_widget.value == "example "


async def test_skill_command_activates_exact_skill(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dummy_app.connected = True
    dummy_app.current_context = "ctx-1"
    dummy_app.connector_features = {"skills_list", "skills_activate"}
    skills = [
        {
            "name": "a0-live-e2e-tester",
            "description": "Live Agent Zero tester",
            "path": "/a0/skills/a0-live-e2e-tester",
            "origin": "Built-in",
        }
    ]
    calls: list[tuple[str, dict[str, object]]] = []
    notices: list[tuple[str, bool]] = []

    async def fake_list_skills(**kwargs) -> list[dict[str, object]]:
        assert kwargs["context_id"] == "ctx-1"
        return skills

    async def fake_activate_skill(context_id: str, skill: dict[str, object]) -> dict[str, object]:
        calls.append((context_id, dict(skill)))
        return {"ok": True, "skill": dict(skill)}

    monkeypatch.setattr(dummy_app.client, "list_skills", fake_list_skills)
    monkeypatch.setattr(dummy_app.client, "activate_skill", fake_activate_skill)
    monkeypatch.setattr(dummy_app, "_show_notice", lambda message, *, error=False: notices.append((message, error)))

    handled = await dummy_app._dispatch_skill_command("$a0-live-e2e-tester")

    assert handled is True
    assert calls == [
        (
            "ctx-1",
            {
                "name": "a0-live-e2e-tester",
                "path": "/a0/skills/a0-live-e2e-tester",
            },
        )
    ]
    assert notices == [("Skill activated for this chat: a0-live-e2e-tester.", False)]


async def test_skill_command_with_remainder_sends_message_after_activation(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dummy_app.connected = True
    dummy_app.current_context = "ctx-1"
    dummy_app.connector_features = {"skills_list", "skills_activate"}
    skills = [{"name": "imagegen", "path": "/a0/skills/imagegen"}]
    sent: list[tuple[str, str | None, list[str] | None]] = []

    async def fake_list_skills(**kwargs) -> list[dict[str, object]]:
        del kwargs
        return skills

    async def fake_activate_skill(context_id: str, skill: dict[str, object]) -> dict[str, object]:
        assert context_id == "ctx-1"
        return {"ok": True, "skill": dict(skill)}

    async def fake_send_message(
        text: str,
        context_id: str | None,
        attachments: list[str] | None = None,
    ) -> None:
        sent.append((text, context_id, attachments))

    monkeypatch.setattr(dummy_app.client, "list_skills", fake_list_skills)
    monkeypatch.setattr(dummy_app.client, "activate_skill", fake_activate_skill)
    monkeypatch.setattr(dummy_app.client, "send_message", fake_send_message)
    monkeypatch.setattr(dummy_app, "_show_notice", lambda *args, **kwargs: None)
    input_widget = dummy_app._test_widgets["#message-input"]  # type: ignore[index]

    await dummy_app.on_chat_input_submitted(
        ChatInput.Submitted(value="$imagegen make a graphite texture", input=input_widget)
    )

    assert sent == [("make a graphite texture", "ctx-1", [])]


def test_whitespace_only_input_does_not_auto_open_command_palette(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[str] = []
    monkeypatch.setattr(
        dummy_app,
        "_open_command_palette",
        lambda *, initial_query="", from_slash=False: opened.append(initial_query),
    )

    input_widget = dummy_app._test_widgets["#message-input"]  # type: ignore[index]
    dummy_app.on_chat_input_value_changed(
        ChatInput.ValueChanged(value="\n", input=input_widget)
    )

    assert opened == []


async def test_profile_command_dispatches_profile_menu(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dummy_app.connected = True
    dummy_app.current_context = "ctx-1"
    dummy_app.connector_features = {"settings_get", "agent_profile_set"}

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
    dummy_app.current_context = "ctx-1"
    dummy_app.connector_features = {"settings_get", "agent_profile_set", "chat_get"}

    calls: list[tuple[str, str]] = []
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

    async def fake_get_chat(context_id: str) -> dict[str, object]:
        assert context_id == "ctx-1"
        return {"agent_profile": "agent0"}

    async def fake_set_agent_profile(context_id: str, profile_key: str) -> dict[str, object]:
        calls.append((context_id, profile_key))
        return {
            "ok": True,
            "agent_profile": "developer",
            "agent_profile_label": "Developer",
        }

    monkeypatch.setattr(dummy_app.client, "get_settings", fake_get_settings)
    monkeypatch.setattr(dummy_app.client, "get_chat", fake_get_chat)
    monkeypatch.setattr(dummy_app.client, "set_agent_profile", fake_set_agent_profile)
    monkeypatch.setattr(dummy_app, "_show_notice", lambda message, *, error=False: notices.append((message, error)))

    await dummy_app._dispatch_command("/profile dev")

    assert calls == [("ctx-1", "developer")]
    assert notices == [("Agent profile set to Developer.", False)]


async def test_settings_snapshot_rehydrates_workspace_without_duplicate_refresh(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dummy_app.connector_features = {"settings_get"}
    payload = {
        "settings": {
            "agent_profile": "developer",
            "workdir_path": "/a0/workspaces/research",
        },
        "additional": {
            "agent_subdirs": [{"value": "developer", "label": "Developer"}],
        },
    }
    token_refreshes = 0

    async def fake_get_settings() -> dict[str, object]:
        return payload

    async def fake_refresh_token_usage(*args, **kwargs) -> None:
        nonlocal token_refreshes
        del args, kwargs
        token_refreshes += 1

    monkeypatch.setattr(dummy_app.client, "get_settings", fake_get_settings)
    monkeypatch.setattr(dummy_app, "_refresh_token_usage", fake_refresh_token_usage)

    changed = await dummy_app._refresh_settings_snapshot()
    unchanged = await dummy_app._refresh_settings_snapshot()

    assert changed is True
    assert unchanged is False
    assert dummy_app._remote_workspace == "/a0/workspaces/research"
    assert token_refreshes == 0


async def test_state_snapshot_applies_changed_model_switcher_state(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dummy_app.connected = True
    dummy_app.current_context = "ctx-1"
    dummy_app.connector_features = {"model_switcher"}
    payloads = [
        {
            "ok": True,
            "allowed": True,
            "override": {"preset_name": "fast"},
            "presets": [{"name": "fast", "label": "Fast"}],
            "main_model": {"provider": "openai", "name": "gpt-5.4"},
            "utility_model": {"provider": "openai", "name": "gpt-5.4-mini"},
        },
        {
            "ok": True,
            "allowed": True,
            "override": {"preset_name": "deep"},
            "presets": [{"name": "deep", "label": "Deep"}],
            "main_model": {"provider": "anthropic", "name": "claude-sonnet"},
            "utility_model": {"provider": "openai", "name": "gpt-5.4-mini"},
        },
    ]
    token_refreshes = 0

    async def fake_get_model_switcher(context_id: str) -> dict[str, object]:
        assert context_id == "ctx-1"
        return payloads[0]

    async def fake_refresh_token_usage(*args, **kwargs) -> None:
        nonlocal token_refreshes
        del args, kwargs
        token_refreshes += 1

    async def fake_refresh_goal_bar(*args, **kwargs) -> bool:
        del args, kwargs
        return False

    monkeypatch.setattr(dummy_app.client, "get_model_switcher", fake_get_model_switcher)
    monkeypatch.setattr(dummy_app, "_refresh_token_usage", fake_refresh_token_usage)
    monkeypatch.setattr(dummy_app, "_refresh_goal_bar", fake_refresh_goal_bar)

    await dummy_app._refresh_state_snapshot()
    await dummy_app._refresh_state_snapshot()
    payloads.pop(0)
    await dummy_app._refresh_state_snapshot()

    switcher = dummy_app._test_widgets["#model-switcher-bar"]  # type: ignore[index]
    assert len(switcher.state_calls) == 2
    assert switcher.state_calls[-1]["selected_preset"] == "deep"
    assert token_refreshes == 2


async def test_state_snapshot_ignores_preset_fields_the_switcher_does_not_render(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dummy_app.connected = True
    dummy_app.current_context = "ctx-1"
    dummy_app.connector_features = {"model_switcher"}
    payloads = [
        {
            "ok": True,
            "allowed": True,
            "override": {"preset_name": "Balanced"},
            "configured_preset": "Default",
            "effective_preset": "Balanced",
            "presets": [
                {
                    "name": "Balanced",
                    "chat": {"provider": "openai", "name": "gpt-5.4", "ctx_length": 128000},
                    "utility": {"provider": "openai", "name": "gpt-5.4-mini"},
                }
            ],
            "main_model": {"provider": "openai", "name": "gpt-5.4", "has_api_key": False},
        },
        {
            "ok": True,
            "allowed": True,
            "override": {"preset_name": "Balanced"},
            "configured_preset": "Default",
            "effective_preset": "Balanced",
            "presets": [
                {
                    "name": "Balanced",
                    "chat": {"provider": "openai", "name": "gpt-5.4", "ctx_length": 200000},
                    "utility": {"provider": "anthropic", "name": "claude-haiku-4-5"},
                    "embedding": {"provider": "openai", "name": "text-embedding-3-large"},
                }
            ],
            "main_model": {"provider": "openai", "name": "gpt-5.4", "has_api_key": True},
        },
    ]

    async def fake_get_model_switcher(context_id: str) -> dict[str, object]:
        assert context_id == "ctx-1"
        return payloads.pop(0)

    async def fake_refresh_goal_bar(*args: object, **kwargs: object) -> bool:
        return False

    monkeypatch.setattr(dummy_app.client, "get_model_switcher", fake_get_model_switcher)
    monkeypatch.setattr(dummy_app, "_refresh_goal_bar", fake_refresh_goal_bar)

    await dummy_app._refresh_state_snapshot()
    await dummy_app._refresh_state_snapshot()

    switcher = dummy_app._test_widgets["#model-switcher-bar"]  # type: ignore[index]
    assert len(switcher.state_calls) == 1


async def test_closed_client_preset_error_is_ignored_during_shutdown(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dummy_app.connected = True
    dummy_app.current_context = "ctx-1"
    dummy_app.connector_features = {"model_switcher"}
    calls = 0
    notices: list[str] = []

    async def closed_client(*args: object, **kwargs: object) -> dict[str, object]:
        nonlocal calls
        calls += 1
        raise RuntimeError("Cannot send a request, as the client has been closed.")

    dummy_app.client.http = SimpleNamespace(is_closed=True)
    monkeypatch.setattr(dummy_app.client, "set_model_preset", closed_client)
    monkeypatch.setattr(dummy_app, "_show_notice", lambda message, **_: notices.append(message))

    await model_commands.set_model_preset(dummy_app, None)

    switcher = dummy_app._test_widgets["#model-switcher-bar"]  # type: ignore[index]
    assert calls == 1
    assert switcher.busy is False
    assert notices == []


async def test_model_preset_response_matches_the_next_poll_signature(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dummy_app.connected = True
    dummy_app.current_context = "ctx-1"
    dummy_app.connector_features = {"model_switcher"}
    payload = {
        "ok": True,
        "allowed": True,
        "override": {"preset_name": "Balanced"},
        "configured_preset": "Default",
        "effective_preset": "Balanced",
        "presets": [{"name": "Balanced", "chat": {"provider": "openai", "name": "gpt-5.4"}}],
        "main_model": {"provider": "openai", "name": "gpt-5.4"},
    }

    async def fake_set_model_preset(*args: object, **kwargs: object) -> dict[str, object]:
        return payload

    async def fake_get_model_switcher(context_id: str) -> dict[str, object]:
        assert context_id == "ctx-1"
        return payload

    async def async_noop(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(dummy_app.client, "set_model_preset", fake_set_model_preset)
    monkeypatch.setattr(dummy_app.client, "get_model_switcher", fake_get_model_switcher)
    monkeypatch.setattr(dummy_app, "_refresh_token_usage", async_noop)
    monkeypatch.setattr(dummy_app, "_refresh_goal_bar", async_noop)

    await model_commands.set_model_preset(dummy_app, "Balanced")
    await dummy_app._refresh_state_snapshot()

    switcher = dummy_app._test_widgets["#model-switcher-bar"]  # type: ignore[index]
    assert len(switcher.state_calls) == 1


def test_show_notice_ignores_an_unmounted_chat_log(dummy_app: DummyAgentZeroCLI) -> None:
    dummy_app.connected = False
    log = dummy_app._test_widgets["#chat-log"]  # type: ignore[index]
    log.is_attached = False

    splash_helpers.show_notice(dummy_app, "This must not try to mount a message.", error=True)

    assert log.writes == []


async def test_model_runtime_main_change_does_not_pin_default_utility(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dummy_app.current_context = "ctx-1"
    dummy_app.connected = True
    dummy_app.connector_features = {"model_switcher"}
    dummy_app._model_switch_allowed = True

    default_utility = {"provider": "a0_venice", "name": "venice-uncensored-1-2"}
    default_chat = {"provider": "openai", "name": "gpt-5.4"}
    updated_main = {"provider": "codex_oauth", "name": "gpt-5.5"}
    saved_presets: list[list[dict[str, object]]] = []
    clear_calls: list[tuple[str, str | None]] = []

    async def fake_get_model_switcher(context_id: str) -> dict[str, object]:
        assert context_id == "ctx-1"
        return {
            "ok": True,
            "allowed": True,
            "override": None,
            "configured_preset": "Default",
            "effective_preset": "Default",
            "presets": [
                {
                    "name": "Default",
                    "chat": dict(default_chat),
                    "utility": dict(default_utility),
                }
            ],
            "main_model": dict(default_chat),
            "utility_model": dict(default_utility),
        }

    async def fake_push_screen_wait(self: object, screen: object) -> ModelRuntimeResult:
        del self, screen
        return ModelRuntimeResult(
            main_model=dict(updated_main),
            utility_model=dict(default_utility),
            main_changed=True,
            utility_changed=False,
        )

    async def fake_save_model_presets(presets: list[dict[str, object]]) -> dict[str, object]:
        saved_presets.append(presets)
        return {"ok": True}

    async def fake_set_model_preset(context_id: str, preset_name: str | None) -> dict[str, object]:
        clear_calls.append((context_id, preset_name))
        return {
            "ok": True,
            "allowed": True,
            "override": None,
            "configured_preset": "Default",
            "effective_preset": "Default",
            "presets": saved_presets[-1],
            "main_model": dict(updated_main),
            "utility_model": dict(default_utility),
        }

    async def async_noop(*args, **kwargs) -> None:
        del args, kwargs

    monkeypatch.setattr(dummy_app.client, "get_model_switcher", fake_get_model_switcher)
    monkeypatch.setattr(dummy_app.client, "save_model_presets", fake_save_model_presets)
    monkeypatch.setattr(dummy_app.client, "set_model_preset", fake_set_model_preset)
    monkeypatch.setattr(DummyAgentZeroCLI, "push_screen_wait", fake_push_screen_wait)
    monkeypatch.setattr(dummy_app, "_refresh_token_usage", async_noop)

    await dummy_app._cmd_models(focus_target="main")

    assert clear_calls == [("ctx-1", None)]
    assert len(saved_presets) == 1
    assert saved_presets[0][0]["chat"] == updated_main
    assert saved_presets[0][0]["utility"] == default_utility


@pytest.mark.parametrize("named_preset", [False, True], ids=["custom", "named-preset"])
async def test_model_runtime_main_change_preserves_other_models(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
    named_preset: bool,
) -> None:
    dummy_app.current_context = "ctx-1"
    dummy_app.connected = True
    dummy_app.connector_features = {"model_switcher"}
    dummy_app._model_switch_allowed = True

    utility_override = {"provider": "openai", "name": "gpt-5.4-mini"}
    embedding_override = {"provider": "openai", "name": "text-embedding-3-large"}
    default_chat = {"provider": "openai", "name": "gpt-5.4"}
    updated_main = {"provider": "codex_oauth", "name": "gpt-5.5"}
    saved_presets: list[list[dict[str, object]]] = []
    clear_calls: list[tuple[str, str | None]] = []

    async def fake_get_model_switcher(context_id: str) -> dict[str, object]:
        assert context_id == "ctx-1"
        return {
            "ok": True,
            "allowed": True,
            "override": (
                {"preset_name": "Power"}
                if named_preset
                else {
                    "utility": dict(utility_override),
                    "embedding": dict(embedding_override),
                }
            ),
            "configured_preset": "Default",
            "effective_preset": "Power" if named_preset else "Default",
            "presets": [
                {
                    "name": "Default",
                    "chat": dict(default_chat),
                    "utility": dict(utility_override),
                    "embedding": dict(embedding_override),
                },
                {"name": "Power"},
            ],
            "main_model": dict(default_chat),
            "utility_model": dict(utility_override),
            "embedding_model": dict(embedding_override),
        }

    async def fake_push_screen_wait(self: object, screen: object) -> ModelRuntimeResult:
        del self, screen
        return ModelRuntimeResult(
            main_model=dict(updated_main),
            utility_model=dict(utility_override),
            main_changed=True,
            utility_changed=False,
        )

    async def fake_save_model_presets(presets: list[dict[str, object]]) -> dict[str, object]:
        saved_presets.append(presets)
        return {"ok": True}

    async def fake_set_model_preset(context_id: str, preset_name: str | None) -> dict[str, object]:
        assert context_id == "ctx-1"
        clear_calls.append((context_id, preset_name))
        return {
            "ok": True,
            "allowed": True,
            "override": None,
            "configured_preset": "Default",
            "effective_preset": "Default",
            "presets": saved_presets[-1],
            "main_model": dict(updated_main),
            "utility_model": dict(utility_override),
            "embedding_model": dict(embedding_override),
        }

    async def async_noop(*args, **kwargs) -> None:
        del args, kwargs

    monkeypatch.setattr(dummy_app.client, "get_model_switcher", fake_get_model_switcher)
    monkeypatch.setattr(dummy_app.client, "save_model_presets", fake_save_model_presets)
    monkeypatch.setattr(dummy_app.client, "set_model_preset", fake_set_model_preset)
    monkeypatch.setattr(DummyAgentZeroCLI, "push_screen_wait", fake_push_screen_wait)
    monkeypatch.setattr(dummy_app, "_refresh_token_usage", async_noop)

    await dummy_app._cmd_models(focus_target="main")

    assert clear_calls == [("ctx-1", None)]
    assert len(saved_presets) == 1
    saved_default = saved_presets[0][0]
    assert saved_default["chat"] == updated_main
    assert saved_default["utility"] == utility_override
    assert saved_default["embedding"] == embedding_override


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


async def test_project_command_with_query_activates_matching_project(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dummy_app.connected = True
    dummy_app.current_context = "ctx-1"
    dummy_app.connector_features = {"projects"}

    projects_payload = {
        "ok": True,
        "projects": [
            {
                "name": "plugins_1",
                "title": "Plugins 1",
                "description": "",
                "color": "#00bbf9",
            }
        ],
        "current_project": None,
    }
    activate_calls: list[tuple[str, str]] = []

    async def fake_get_projects(context_id: str) -> dict[str, object]:
        assert context_id == "ctx-1"
        return dict(projects_payload)

    async def fake_activate_project(context_id: str, name: str) -> dict[str, object]:
        activate_calls.append((context_id, name))
        return {
            "ok": True,
            "projects": list(projects_payload["projects"]),
            "current_project": dict(projects_payload["projects"][0]),
        }

    monkeypatch.setattr(dummy_app.client, "get_projects", fake_get_projects)
    monkeypatch.setattr(dummy_app.client, "activate_project", fake_activate_project)

    await dummy_app._dispatch_command("/project plugins")

    assert activate_calls == [("ctx-1", "plugins_1")]
    assert dummy_app.current_project == {
        "name": "plugins_1",
        "title": "Plugins 1",
        "description": "",
        "color": "#00bbf9",
    }
    assert dummy_app._test_widgets["#message-input"].focused is True  # type: ignore[index]


async def test_project_command_with_clear_value_deactivates_active_project(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dummy_app.connected = True
    dummy_app.current_context = "ctx-1"
    dummy_app.connector_features = {"projects"}

    current_project = {
        "name": "plugins_1",
        "title": "Plugins 1",
        "description": "",
        "color": "#00bbf9",
    }
    deactivate_calls: list[str] = []

    async def fake_get_projects(context_id: str) -> dict[str, object]:
        assert context_id == "ctx-1"
        return {
            "ok": True,
            "projects": [dict(current_project)],
            "current_project": dict(current_project),
        }

    async def fake_deactivate_project(context_id: str) -> dict[str, object]:
        deactivate_calls.append(context_id)
        return {
            "ok": True,
            "projects": [dict(current_project)],
            "current_project": None,
        }

    monkeypatch.setattr(dummy_app.client, "get_projects", fake_get_projects)
    monkeypatch.setattr(dummy_app.client, "deactivate_project", fake_deactivate_project)

    await dummy_app._dispatch_command("/project none")

    assert deactivate_calls == ["ctx-1"]
    assert dummy_app.current_project is None


async def test_project_command_with_no_project_label_deactivates_active_project(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dummy_app.connected = True
    dummy_app.current_context = "ctx-1"
    dummy_app.connector_features = {"projects"}

    current_project = {
        "name": "plugins_1",
        "title": "Plugins 1",
        "description": "",
        "color": "#00bbf9",
    }
    deactivate_calls: list[str] = []

    async def fake_get_projects(context_id: str) -> dict[str, object]:
        assert context_id == "ctx-1"
        return {
            "ok": True,
            "projects": [dict(current_project)],
            "current_project": dict(current_project),
        }

    async def fake_deactivate_project(context_id: str) -> dict[str, object]:
        deactivate_calls.append(context_id)
        return {
            "ok": True,
            "projects": [dict(current_project)],
            "current_project": None,
        }

    monkeypatch.setattr(dummy_app.client, "get_projects", fake_get_projects)
    monkeypatch.setattr(dummy_app.client, "deactivate_project", fake_deactivate_project)

    await dummy_app._dispatch_command("/project No Project")

    assert deactivate_calls == ["ctx-1"]
    assert dummy_app.current_project is None


async def test_project_menu_deactivate_ignores_display_label(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dummy_app.connected = True
    dummy_app.current_context = "ctx-1"
    dummy_app.connector_features = {"projects"}

    deactivate_calls: list[str] = []

    async def fake_deactivate_project(context_id: str) -> dict[str, object]:
        deactivate_calls.append(context_id)
        return {
            "ok": True,
            "projects": [],
            "current_project": None,
        }

    monkeypatch.setattr(dummy_app.client, "deactivate_project", fake_deactivate_project)

    await dummy_app._handle_project_menu_action("deactivate", project_name_value="No Project")

    assert deactivate_calls == ["ctx-1"]
    assert dummy_app.current_project is None


async def test_project_command_reports_ambiguous_matches(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dummy_app.connected = True
    dummy_app.current_context = "ctx-1"
    dummy_app.connector_features = {"projects"}

    notices: list[tuple[str, bool]] = []

    async def fake_get_projects(context_id: str) -> dict[str, object]:
        assert context_id == "ctx-1"
        return {
            "ok": True,
            "projects": [
                {"name": "plugins_1", "title": "Plugins 1", "description": "", "color": "#00bbf9"},
                {"name": "plugins_2", "title": "Plugins 2", "description": "", "color": "#00f5d4"},
            ],
            "current_project": None,
        }

    monkeypatch.setattr(dummy_app.client, "get_projects", fake_get_projects)
    monkeypatch.setattr(
        dummy_app,
        "_show_notice",
        lambda message, *, error=False: notices.append((message, error)),
    )

    await dummy_app._dispatch_command("/project plugins")

    assert notices == [("Project name is ambiguous. Matches: Plugins 1 (plugins_1), Plugins 2 (plugins_2)", True)]


async def test_project_command_reports_missing_project(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dummy_app.connected = True
    dummy_app.current_context = "ctx-1"
    dummy_app.connector_features = {"projects"}

    notices: list[tuple[str, bool]] = []

    async def fake_get_projects(context_id: str) -> dict[str, object]:
        assert context_id == "ctx-1"
        return {
            "ok": True,
            "projects": [
                {"name": "plugins_1", "title": "Plugins 1", "description": "", "color": "#00bbf9"},
            ],
            "current_project": None,
        }

    monkeypatch.setattr(dummy_app.client, "get_projects", fake_get_projects)
    monkeypatch.setattr(
        dummy_app,
        "_show_notice",
        lambda message, *, error=False: notices.append((message, error)),
    )

    await dummy_app._dispatch_command("/project missing")

    assert notices == [("Project 'missing' was not found. Available projects: Plugins 1 (plugins_1)", True)]


async def test_remote_safety_toggles_update_local_permissions(
    dummy_app: DummyAgentZeroCLI,
) -> None:
    assert dummy_app._remote_files.allow_writes is True
    assert dummy_app._python_tty.enabled is False
    assert dummy_app._python_tty.allow_writes is True

    await dummy_app.action_toggle_remote_file_mode()
    await dummy_app.action_toggle_remote_exec()

    assert dummy_app._remote_file_write_enabled is False
    assert dummy_app._remote_exec_enabled is True
    assert dummy_app._remote_files.allow_writes is False
    assert dummy_app._python_tty.enabled is True
    assert dummy_app._python_tty.allow_writes is False


async def test_computer_use_slash_commands_update_notice_and_status(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dummy_app.connected = True
    dummy_app.current_context_has_messages = True
    notices: list[tuple[str, bool]] = []
    monkeypatch.setattr(dummy_app, "_show_notice", lambda message, *, error=False: notices.append((message, error)))

    await dummy_app._dispatch_command("/computer-use on")

    status = dummy_app._test_widgets["#connection-status"]  # type: ignore[index]
    banner = dummy_app._test_widgets["#computer-use-banner"]  # type: ignore[index]
    assert dummy_app._computer_use.enabled is True
    assert dummy_app._computer_use.trust_mode == "allow"
    assert dummy_app._computer_use.arm_calls == []
    assert dummy_app._computer_use.rearm_calls == [None]
    assert status.computer_use_status == "Active"
    assert banner.display is True
    assert banner.message == "Computer Use is active for this CLI session."
    assert notices == [("Computer use is active for this CLI session.", False)]

    await dummy_app._dispatch_command("/computer-use off")

    assert dummy_app._computer_use.enabled is False
    assert dummy_app._computer_use.disconnect_calls == 1
    assert status.computer_use_status == "Disabled"
    assert banner.display is False
    assert banner.message == ""


async def test_computer_use_slash_commands_refresh_hello_metadata_when_connected(
    dummy_app: DummyAgentZeroCLI,
) -> None:
    calls: list[dict[str, object]] = []

    async def fake_send_hello(
        *,
        context_id: str | None = None,
        computer_use: dict[str, object] | None = None,
        host_browser: dict[str, object] | None = None,
        remote_files: dict[str, object] | None = None,
        remote_exec: dict[str, object] | None = None,
    ) -> dict[str, object]:
        calls.append(
            {
                "context_id": context_id,
                "computer_use": dict(computer_use or {}),
                "host_browser": dict(host_browser or {}),
                "remote_files": dict(remote_files or {}),
                "remote_exec": dict(remote_exec or {}),
            }
        )
        return {"exec_config": {"version": 1}}

    dummy_app.client.connected = True
    dummy_app.current_context = "ctx-remote"
    dummy_app.client.send_hello = fake_send_hello  # type: ignore[method-assign]

    await dummy_app._dispatch_command("/computer-use on")
    await dummy_app._dispatch_command("/computer-use off")

    assert calls == [
        {
            "context_id": "ctx-remote",
            "computer_use": {
                "supported": True,
                "enabled": True,
                "trust_mode": "allow",
                "status": "active",
                "last_error": "",
                "restore_token_present": False,
                "artifact_root": "/a0/tmp/_a0_connector/computer_use",
            },
            "host_browser": _host_browser_metadata(False),
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
            "context_id": "ctx-remote",
            "computer_use": {
                "supported": True,
                "enabled": False,
                "trust_mode": "allow",
                "status": "disabled",
                "last_error": "",
                "restore_token_present": False,
                "artifact_root": "/a0/tmp/_a0_connector/computer_use",
            },
            "host_browser": _host_browser_metadata(False),
            "remote_files": {
                "enabled": True,
                "write_enabled": True,
                "mode": "read_write",
            },
            "remote_exec": {
                "enabled": False,
            },
        },
    ]


async def test_browser_host_on_repairs_missing_playwright(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host_browser = dummy_app._host_browser
    host_browser.playwright_available = False
    notices: list[tuple[str, bool]] = []
    monkeypatch.setattr(dummy_app, "_show_notice", lambda message, *, error=False: notices.append((message, error)))

    await dummy_app._dispatch_command("/browser host on")

    assert host_browser.enabled is True
    assert host_browser.install_calls == 1
    assert host_browser.playwright_available is True
    assert "Installing now: uv pip install --python /tmp/python playwright" in notices[0][0]
    assert "Python Playwright installed for host browser control" in notices[1][0]
    assert notices[-1][0].startswith("Host browser enabled.")


async def test_browser_host_on_skips_playwright_for_remote_debugging(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host_browser = dummy_app._host_browser
    host_browser.playwright_available = False
    host_browser.remote_debugging = True
    notices: list[tuple[str, bool]] = []
    monkeypatch.setattr(dummy_app, "_show_notice", lambda message, *, error=False: notices.append((message, error)))

    await dummy_app._dispatch_command("/browser host on")

    assert host_browser.enabled is True
    assert host_browser.install_calls == 0
    assert not any("Installing now" in notice[0] for notice in notices)


async def test_browser_host_on_off_syncs_agent_zero_browser_mode(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str | None, str]] = []
    notices: list[tuple[str, bool]] = []

    async def fake_set_browser_runtime(
        context_id: str | None,
        runtime_backend: str,
    ) -> dict[str, object]:
        calls.append((context_id, runtime_backend))
        return {
            "ok": True,
            "runtime_backend": runtime_backend,
            "project_name": "Research",
        }

    dummy_app.connector_features = {"browser_runtime_config"}
    dummy_app.current_context = "ctx-browser"
    dummy_app.client.set_browser_runtime = fake_set_browser_runtime  # type: ignore[method-assign]
    monkeypatch.setattr(
        dummy_app,
        "_show_notice",
        lambda message, *, error=False: notices.append((message, error)),
    )

    await dummy_app._dispatch_command("/browser host on")
    await dummy_app._dispatch_command("/browser host off")

    assert calls == [
        ("ctx-browser", "host_required"),
        ("ctx-browser", "container"),
    ]
    assert dummy_app._host_browser.enabled is False
    assert "Host browser enabled." in notices[0][0]
    assert "Browser set to Bring Your Own Browser for project Research." in notices[0][0]
    assert "Browser model-use settings" in notices[0][0]
    assert "Host browser disabled." in notices[1][0]
    assert "Browser set to Docker browser for project Research." in notices[1][0]


async def test_browser_repair_command_installs_missing_playwright(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host_browser = dummy_app._host_browser
    host_browser.playwright_available = False
    notices: list[tuple[str, bool]] = []
    monkeypatch.setattr(dummy_app, "_show_notice", lambda message, *, error=False: notices.append((message, error)))

    await dummy_app._dispatch_command("/browser repair")

    assert host_browser.install_calls == 1
    assert "Installing now: uv pip install --python /tmp/python playwright" in notices[0][0]
    assert notices[-1][0].startswith("Host browser repair completed.")


async def test_browser_runtime_commands_update_agent_zero_config(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str | None, str]] = []
    notices: list[tuple[str, bool]] = []

    async def fake_set_browser_runtime(
        context_id: str | None,
        runtime_backend: str,
    ) -> dict[str, object]:
        calls.append((context_id, runtime_backend))
        return {
            "ok": True,
            "runtime_backend": runtime_backend,
            "project_name": "Research",
        }

    dummy_app.connector_features = {"browser_runtime_config"}
    dummy_app.current_context = "ctx-browser"
    dummy_app.client.set_browser_runtime = fake_set_browser_runtime  # type: ignore[method-assign]
    monkeypatch.setattr(
        dummy_app,
        "_show_notice",
        lambda message, *, error=False: notices.append((message, error)),
    )

    await dummy_app._dispatch_command("/browser host")
    await dummy_app._dispatch_command("/browser container")

    assert dummy_app._host_browser.enabled is True
    assert calls == [
        ("ctx-browser", "host_required"),
        ("ctx-browser", "container"),
    ]
    assert "Browser set to Bring Your Own Browser for project Research." in notices[0][0]
    assert "Browser set to Docker browser for project Research." in notices[1][0]
    assert all("Browser model-use settings" in notice[0] for notice in notices)


async def test_browser_direct_command_sets_host_browser_selection(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str | None, str, str | None, str | None]] = []
    notices: list[tuple[str, bool]] = []

    async def fake_set_browser_runtime(
        context_id: str | None,
        runtime_backend: str,
        *,
        host_browser_selection: str | None = None,
        profile_mode: str | None = None,
    ) -> dict[str, object]:
        calls.append((context_id, runtime_backend, host_browser_selection, profile_mode))
        return {
            "ok": True,
            "runtime_backend": runtime_backend,
            "host_browser_selection": host_browser_selection,
            "project_name": "Research",
        }

    dummy_app.connector_features = {"browser_runtime_config"}
    dummy_app.current_context = "ctx-browser"
    dummy_app._host_browser.playwright_available = False
    dummy_app.client.set_browser_runtime = fake_set_browser_runtime  # type: ignore[method-assign]
    monkeypatch.setattr(
        dummy_app,
        "_show_notice",
        lambda message, *, error=False: notices.append((message, error)),
    )

    await dummy_app._dispatch_command("/browser list")
    await dummy_app._dispatch_command("/browser 1")
    dummy_app._host_browser.playwright_available = True
    await dummy_app._dispatch_command("/browser auto")

    assert "0 auto - Automatic" in notices[0][0]
    assert "1 ws://localhost:9222/devtools/browser/test" in notices[0][0]
    assert calls == [
        (
            "ctx-browser",
            "host_required",
            "ws://localhost:9222/devtools/browser/test",
            "existing",
        ),
        (
            "ctx-browser",
            "host_required",
            "",
            "existing",
        ),
    ]
    assert dummy_app._host_browser.enabled is True
    assert dummy_app._host_browser.install_calls == 0
    assert any(
        "Browser host target set to Chrome (allowed) - localhost:9222 for project Research." in notice[0]
        for notice in notices
    )
    assert "Browser host target set to Automatic (A0 CLI chooses) for project Research." in notices[-1][0]


def test_system_commands_include_computer_use_without_experimental_menu(
    dummy_app: DummyAgentZeroCLI,
) -> None:
    commands = list(dummy_app.get_system_commands(None))
    titles = {getattr(command, "title", getattr(command, "name", "")) for command in commands}

    assert "/experimental" not in titles
    assert "/computer-use" in titles
    assert all(not title.startswith("Computer Use: ") for title in titles)
    assert "Browser: Use Host" in titles
    assert "Browser: Docker Container" in titles
    assert "/clear" in titles
    assert "/pause" in titles
    assert "/resume" in titles
    assert "/nudge" in titles
    assert "Computer Use: Interactive" not in titles
    assert "Computer Use: Persistent" not in titles
    assert "Computer Use: Allow" not in titles


def test_system_commands_include_plugins_when_feature_available(
    dummy_app: DummyAgentZeroCLI,
) -> None:
    dummy_app.connected = True
    dummy_app.connector_features = {"installed_plugins"}

    commands = list(dummy_app.get_system_commands(None))
    titles = {getattr(command, "title", getattr(command, "name", "")) for command in commands}

    assert "/plugins" in titles


async def test_server_commands_extend_palette_without_overriding_local_commands(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dummy_app.connected = True
    dummy_app.current_context = "ctx-1"

    async def fake_list_commands(context_id: str) -> list[dict[str, object]]:
        assert context_id == "ctx-1"
        return [
            {
                "name": "compact",
                "description": "Server compact command.",
            },
            {
                "name": "compress",
                "description": "Force-run LLM context compression on the current chat.",
                "source_scope_label": "Plugin: compress_history",
            },
        ]

    monkeypatch.setattr(dummy_app.client, "list_commands", fake_list_commands)

    await dummy_app._load_server_commands()
    commands = list(dummy_app.get_system_commands(None))
    titles = [getattr(command, "title", getattr(command, "name", "")) for command in commands]

    assert titles.count("/compact") == 1
    assert "/compress" in titles


async def test_server_command_dispatch_uses_normal_chat_path(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dummy_app.connected = True
    dummy_app.current_context = "ctx-1"
    sent: list[tuple[str, str, list[object]]] = []

    async def fake_list_commands(context_id: str) -> list[dict[str, object]]:
        assert context_id == "ctx-1"
        return [{"name": "compress", "description": "Compress this chat."}]

    async def fake_send_chat_text(
        text: str,
        *,
        raw_text: str,
        attachments: list[object],
        input_widget: object = None,
    ) -> None:
        del input_widget
        sent.append((text, raw_text, attachments))

    monkeypatch.setattr(dummy_app.client, "list_commands", fake_list_commands)
    monkeypatch.setattr(dummy_app, "_send_chat_text", fake_send_chat_text)

    await dummy_app._dispatch_command("/compress now")

    assert sent == [("/compress now", "/compress now", [])]


async def test_plugins_command_opens_installed_plugins_screen(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class PluginClient:
        async def list_installed_plugins(self) -> list[dict[str, object]]:
            return [
                {
                    "name": "_browser",
                    "display_name": "Browser",
                    "enabled": True,
                    "toggleable": True,
                }
            ]

    captured: list[object] = []
    dummy_app.connected = True
    dummy_app.connector_features = {"installed_plugins"}
    dummy_app.client = PluginClient()  # type: ignore[assignment]

    async def fake_push_screen_wait(screen: object) -> None:
        captured.append(screen)
        return None

    monkeypatch.setattr(dummy_app, "push_screen_wait", fake_push_screen_wait)

    await dummy_app._dispatch_command("/plugins")

    assert len(captured) == 1
    assert isinstance(captured[0], InstalledPluginsScreen)


async def test_plugins_command_requires_connector_feature(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    notices: list[tuple[str, bool]] = []
    dummy_app.connected = True
    dummy_app.connector_features = set()
    monkeypatch.setattr(
        dummy_app,
        "_show_notice",
        lambda message, *, error=False: notices.append((message, error)),
    )

    await dummy_app._dispatch_command("/plugins")

    assert notices == [("This connector build does not advertise: installed_plugins.", True)]


def test_welcome_actions_keep_run_controls_out_of_splash(
    dummy_app: DummyAgentZeroCLI,
) -> None:
    dummy_app.connected = True
    dummy_app.current_context = "ctx-1"
    dummy_app.connector_features = {"pause", "nudge"}

    action_keys = {action.key for action in dummy_app._welcome_actions()}

    assert "/pause" not in action_keys
    assert "/resume" not in action_keys
    assert "/nudge" not in action_keys


async def test_computer_use_removed_mode_commands_show_usage_without_fallback(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    notices: list[tuple[str, bool]] = []
    monkeypatch.setattr(dummy_app, "_show_notice", lambda message, *, error=False: notices.append((message, error)))
    initial_mode = dummy_app._computer_use.trust_mode

    await dummy_app._dispatch_command("/computer-use allow")
    await dummy_app._dispatch_command("/computer confirm")
    await dummy_app._dispatch_command("/computer-use arm")

    assert dummy_app._computer_use.trust_mode == initial_mode
    assert dummy_app._computer_use.rearm_calls == []
    assert notices == [
        ("Usage: /computer-use on|off|status", True),
        ("Usage: /computer-use on|off|status", True),
        ("Usage: /computer-use on|off|status", True),
    ]


async def test_computer_use_status_command_reports_runtime_status(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    notices: list[tuple[str, bool]] = []
    monkeypatch.setattr(dummy_app, "_show_notice", lambda message, *, error=False: notices.append((message, error)))
    dummy_app._computer_use.enabled = True
    dummy_app._computer_use.trust_mode = "allow"
    dummy_app._computer_use.status_label = "active"

    await dummy_app._dispatch_command("/computer-use status")

    assert notices == [
        (
            "Computer use is enabled for this CLI session (Allow); "
            "status: Active. Use /computer-use on|off.",
            False,
        )
    ]


async def test_computer_use_on_forces_approval_prompt_for_current_chat(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    notices: list[tuple[str, bool]] = []
    monkeypatch.setattr(dummy_app, "_show_notice", lambda message, *, error=False: notices.append((message, error)))
    dummy_app.current_context = "ctx-cua"
    dummy_app._computer_use.arm_result = {"ok": True, "result": {"status": "active"}}

    await dummy_app._dispatch_command("/computer-use on")

    status = dummy_app._test_widgets["#connection-status"]  # type: ignore[index]
    assert dummy_app._computer_use.rearm_calls == ["ctx-cua"]
    assert dummy_app._computer_use.arm_calls == []
    assert dummy_app._computer_use.enabled is True
    assert dummy_app._computer_use.trust_mode == "allow"
    assert status.computer_use_status == "Active"
    assert notices == [("Computer use is active for this CLI session.", False)]


async def test_computer_use_on_reports_rearm_failure_without_compat_command(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    notices: list[tuple[str, bool]] = []
    monkeypatch.setattr(dummy_app, "_show_notice", lambda message, *, error=False: notices.append((message, error)))
    dummy_app.current_context = "ctx-cua"
    dummy_app._computer_use.rearm_result = {
        "ok": False,
        "code": "COMPUTER_USE_REARM_REQUIRED",
        "error": "Portal permission was denied.",
    }

    await dummy_app._dispatch_command("/computer-use on")

    status = dummy_app._test_widgets["#connection-status"]  # type: ignore[index]

    assert dummy_app._computer_use.arm_calls == []
    assert dummy_app._computer_use.rearm_calls == ["ctx-cua"]
    assert dummy_app._computer_use.enabled is True
    assert status.computer_use_status == "Rearm Required"
    assert status.computer_use_detail == "Portal permission was denied."
    assert notices == [
        (
            "Computer use enabled locally, but platform permission is not armed: Portal permission was denied.",
            True,
        ),
    ]


async def test_computer_use_on_rearms_after_runtime_marked_token_stale(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    notices: list[tuple[str, bool]] = []
    monkeypatch.setattr(dummy_app, "_show_notice", lambda message, *, error=False: notices.append((message, error)))
    dummy_app.current_context = "ctx-stale-token"
    dummy_app._computer_use.enabled = True
    dummy_app._computer_use.trust_mode = "allow"
    dummy_app._computer_use.status_label = "rearm required"
    dummy_app._computer_use.status_detail = "Silent restore was not available."
    dummy_app._computer_use.arm_result = {"ok": True, "result": {"status": "active"}}

    await dummy_app._dispatch_command("/computer-use on")

    status = dummy_app._test_widgets["#connection-status"]  # type: ignore[index]

    assert dummy_app._computer_use.arm_calls == []
    assert dummy_app._computer_use.rearm_calls == ["ctx-stale-token"]
    assert dummy_app._computer_use.enabled is True
    assert status.computer_use_status == "Active"
    assert notices == [("Computer use is active for this CLI session.", False)]


def test_connection_status_endpoint_indicator_omits_computer_use_summary() -> None:
    status = ConnectionStatus()
    status.status = "connected"
    status.url = "http://localhost:32080"
    status.set_computer_use_state("Allow", "")

    rendered = status._render_endpoint_indicator().plain

    assert rendered == "http://localhost:32080 •"
    assert "CU" not in rendered
    assert "Allow" not in rendered


def test_computer_use_banner_uses_windows_checking_copy_for_prompt_states() -> None:
    approval_message = computer_use_banner_mod._message_for_status(
        "Approval Required",
        enabled=True,
        backend_family="windows",
    )
    rearm_message = computer_use_banner_mod._message_for_status(
        "Rearm Required",
        enabled=True,
        backend_id="windows",
    )

    assert approval_message == "Computer Use is checking Windows desktop access."
    assert rearm_message == "Computer Use is checking Windows desktop access."
    assert "platform permission prompt" not in approval_message
    assert "restart the CLI" not in approval_message


def test_computer_use_banner_explains_deferred_permission_for_prompt_backends() -> None:
    message = computer_use_banner_mod._message_for_status(
        "Approval Required",
        enabled=True,
        backend_family="linux",
    )

    assert message == (
        "Computer Use is enabled. Ask Agent Zero to perform the desktop task; "
        "the system permission portal will appear."
    )


async def test_remote_safety_toggles_refresh_hello_metadata_when_connected(
    dummy_app: DummyAgentZeroCLI,
) -> None:
    calls: list[dict[str, object]] = []

    async def fake_send_hello(
        *,
        context_id: str | None = None,
        computer_use: dict[str, object] | None = None,
        host_browser: dict[str, object] | None = None,
        remote_files: dict[str, object] | None = None,
        remote_exec: dict[str, object] | None = None,
    ) -> dict[str, object]:
        calls.append(
            {
                "context_id": context_id,
                "computer_use": dict(computer_use or {}),
                "host_browser": dict(host_browser or {}),
                "remote_files": dict(remote_files or {}),
                "remote_exec": dict(remote_exec or {}),
            }
        )
        return {"exec_config": {"version": 1}}

    dummy_app.client.connected = True
    dummy_app.current_context = "ctx-remote"
    dummy_app.client.send_hello = fake_send_hello  # type: ignore[method-assign]

    await dummy_app.action_toggle_remote_file_mode()
    await dummy_app.action_toggle_remote_exec()

    computer_use_metadata = {
        "supported": True,
        "enabled": False,
        "trust_mode": "allow",
        "status": "disabled",
        "last_error": "",
        "restore_token_present": False,
        "artifact_root": "/a0/tmp/_a0_connector/computer_use",
    }
    assert calls == [
        {
            "context_id": "ctx-remote",
            "computer_use": computer_use_metadata,
            "host_browser": _host_browser_metadata(False),
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
            "context_id": "ctx-remote",
            "computer_use": computer_use_metadata,
            "host_browser": _host_browser_metadata(False),
            "remote_files": {
                "enabled": True,
                "write_enabled": False,
                "mode": "read_only",
            },
            "remote_exec": {
                "enabled": True,
            },
        },
    ]


async def test_computer_use_status_transition_refreshes_hello_metadata_when_connected(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []
    workers: list[tuple[object, dict[str, object]]] = []

    async def fake_send_hello(
        *,
        context_id: str | None = None,
        computer_use: dict[str, object] | None = None,
        host_browser: dict[str, object] | None = None,
        remote_files: dict[str, object] | None = None,
        remote_exec: dict[str, object] | None = None,
    ) -> dict[str, object]:
        calls.append(
            {
                "context_id": context_id,
                "computer_use": dict(computer_use or {}),
                "host_browser": dict(host_browser or {}),
                "remote_files": dict(remote_files or {}),
                "remote_exec": dict(remote_exec or {}),
            }
        )
        return {"exec_config": {"version": 1}}

    def fake_run_worker(awaitable: object, **kwargs: object) -> object:
        workers.append((awaitable, dict(kwargs)))
        return object()

    dummy_app.connected = True
    dummy_app.client.connected = True
    dummy_app.current_context = "ctx-cu"
    dummy_app.client.send_hello = fake_send_hello  # type: ignore[method-assign]
    monkeypatch.setattr(dummy_app, "run_worker", fake_run_worker)

    dummy_app._computer_use.enabled = True
    dummy_app._computer_use.status_label = "active"
    dummy_app._apply_computer_use_status("active", "")

    assert len(workers) == 1
    assert workers[0][1] == {
        "exclusive": True,
        "name": "computer-use-metadata-refresh",
    }
    await workers[0][0]  # type: ignore[misc]

    assert calls[-1]["context_id"] == "ctx-cu"
    assert calls[-1]["computer_use"] == {
        "supported": True,
        "enabled": True,
        "trust_mode": "allow",
        "status": "active",
        "last_error": "",
        "restore_token_present": False,
        "artifact_root": "/a0/tmp/_a0_connector/computer_use",
    }


async def test_computer_use_start_session_op_refreshes_active_metadata(
    dummy_app: DummyAgentZeroCLI,
) -> None:
    calls: list[dict[str, object]] = []

    async def fake_send_hello(
        *,
        context_id: str | None = None,
        computer_use: dict[str, object] | None = None,
        host_browser: dict[str, object] | None = None,
        remote_files: dict[str, object] | None = None,
        remote_exec: dict[str, object] | None = None,
    ) -> dict[str, object]:
        calls.append(
            {
                "context_id": context_id,
                "computer_use": dict(computer_use or {}),
                "host_browser": dict(host_browser or {}),
                "remote_files": dict(remote_files or {}),
                "remote_exec": dict(remote_exec or {}),
            }
        )
        return {"exec_config": {"version": 1}}

    dummy_app.client.connected = True
    dummy_app.current_context = "ctx-cu"
    dummy_app.client.send_hello = fake_send_hello  # type: ignore[method-assign]
    dummy_app._computer_use.enabled = True
    dummy_app._computer_use.status_label = "rearm required"

    result = await dummy_app._handle_computer_use_op(
        {"action": "start_session", "op_id": "op-cu"}
    )

    assert result == {"op_id": "op-cu", "ok": True, "result": {"status": "active"}}
    assert dummy_app._computer_use.handled_ops == [{"action": "start_session", "op_id": "op-cu"}]
    assert calls == []

    connection._refresh_metadata_after_computer_use_result(
        dummy_app,
        {"action": "start_session", "op_id": "op-cu"},
    )
    await asyncio.sleep(0)

    assert calls[-1]["context_id"] == "ctx-cu"
    assert calls[-1]["computer_use"] == {
        "supported": True,
        "enabled": True,
        "trust_mode": "allow",
        "status": "active",
        "last_error": "",
        "restore_token_present": False,
        "artifact_root": "/a0/tmp/_a0_connector/computer_use",
    }


async def test_remote_exec_toggle_warns_when_metadata_refresh_fails(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    notices: list[tuple[str, bool]] = []

    async def fake_send_hello(**_: object) -> dict[str, object]:
        raise RuntimeError("socket call failed")

    dummy_app.client.connected = True
    dummy_app.client.send_hello = fake_send_hello  # type: ignore[method-assign]
    monkeypatch.setattr(
        dummy_app,
        "_show_notice",
        lambda message, *, error=False: notices.append((message, error)),
    )

    await dummy_app.action_toggle_remote_exec()

    assert dummy_app._remote_exec_enabled is True
    assert dummy_app._python_tty.enabled is True
    assert notices == [
        (
            "Remote execution changed locally, but Agent Zero did not acknowledge "
            "the update: socket call failed",
            True,
        )
    ]


def test_sync_computer_use_status_surfaces_rearm_required_state(
    dummy_app: DummyAgentZeroCLI,
) -> None:
    dummy_app._computer_use.status_label = "rearm required"
    dummy_app._computer_use.status_detail = "COMPUTER_USE_REARM_REQUIRED"

    dummy_app._sync_computer_use_status()

    status = dummy_app._test_widgets["#connection-status"]  # type: ignore[index]
    banner = dummy_app._test_widgets["#computer-use-banner"]  # type: ignore[index]
    assert status.computer_use_status == "Rearm Required"
    assert status.computer_use_detail == "COMPUTER_USE_REARM_REQUIRED"
    assert banner.display is False
    assert banner.message == ""


def test_sync_computer_use_status_shows_active_banner_copy(
    dummy_app: DummyAgentZeroCLI,
) -> None:
    dummy_app.connected = True
    dummy_app.current_context_has_messages = True
    dummy_app._computer_use.enabled = True
    dummy_app._computer_use.status_label = "active"

    dummy_app._sync_computer_use_status()

    banner = dummy_app._test_widgets["#computer-use-banner"]  # type: ignore[index]
    assert banner.display is True
    assert banner.message == "Computer Use is active for this CLI session."


def test_sync_computer_use_status_shows_arming_banner_copy(
    dummy_app: DummyAgentZeroCLI,
) -> None:
    dummy_app.connected = True
    dummy_app.current_context_has_messages = True
    dummy_app._computer_use.enabled = True
    dummy_app._computer_use.status_label = "arming"

    dummy_app._sync_computer_use_status()

    status = dummy_app._test_widgets["#connection-status"]  # type: ignore[index]
    banner = dummy_app._test_widgets["#computer-use-banner"]  # type: ignore[index]
    assert status.computer_use_status == "Arming"
    assert banner.display is True
    assert banner.message == "Computer Use is checking host permissions."


def test_sync_computer_use_status_suppresses_windows_approval_prompt_copy(
    dummy_app: DummyAgentZeroCLI,
) -> None:
    dummy_app.connected = True
    dummy_app.current_context_has_messages = True
    dummy_app._computer_use.enabled = True
    dummy_app._computer_use.status_label = "approval required"
    dummy_app._computer_use.backend_id = "windows"
    dummy_app._computer_use.backend_family = "windows"

    dummy_app._sync_computer_use_status()

    banner = dummy_app._test_widgets["#computer-use-banner"]  # type: ignore[index]
    assert banner.display is True
    assert banner.message == "Computer Use is checking Windows desktop access."
    assert "platform permission prompt" not in banner.message
    assert "restart the CLI" not in banner.message


def test_sync_computer_use_status_keeps_portal_prompt_copy_for_non_windows(
    dummy_app: DummyAgentZeroCLI,
) -> None:
    dummy_app.connected = True
    dummy_app.current_context_has_messages = True
    dummy_app._computer_use.enabled = True
    dummy_app._computer_use.status_label = "approval required"
    dummy_app._computer_use.backend_id = "wayland"
    dummy_app._computer_use.backend_family = "linux"

    dummy_app._sync_computer_use_status()

    banner = dummy_app._test_widgets["#computer-use-banner"]  # type: ignore[index]
    assert banner.display is True
    assert banner.message == (
        "Computer Use is enabled. Ask Agent Zero to perform the desktop task; "
        "the system permission portal will appear."
    )


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
    monkeypatch.setattr(dummy_app, "_clear_goal_bar", lambda: None)
    monkeypatch.setattr(dummy_app, "_set_workspace_context", lambda remote_workspace="": None)
    monkeypatch.setattr(dummy_app, "_clear_model_switcher", lambda: None)
    monkeypatch.setattr(dummy_app, "_sync_body_mode", lambda: None)
    monkeypatch.setattr(dummy_app._python_tty, "close", async_noop)

    connection._reset_disconnected_state(dummy_app)
    await asyncio.sleep(0)

    assert dummy_app._computer_use.disconnect_calls == 1


async def test_recover_websocket_preserves_active_context(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RecoveringClient:
        def __init__(self) -> None:
            self.connected = False
            self.connect_calls = 0
            self.hello_calls: list[dict[str, object]] = []
            self.subscribe_calls: list[str] = []
            # A0Client exposes base_url; the recovery loop watches it to stop
            # when the user reconnects to a different host meanwhile.
            self.base_url = "http://agent.test"

        async def connect_websocket(self) -> None:
            self.connect_calls += 1
            self.connected = True

        async def send_hello(self, **payload: object) -> dict[str, object]:
            self.hello_calls.append(dict(payload))
            return {"exec_config": {"version": 1}}

        async def subscribe_context(self, context_id: str, **kwargs) -> dict[str, object]:
            del kwargs
            self.subscribe_calls.append(context_id)
            return {}

    class FakePythonTty:
        def __init__(self) -> None:
            self.exec_configs: list[object] = []

        def set_exec_config(self, config: object) -> None:
            self.exec_configs.append(config)

    client = RecoveringClient()
    tty = FakePythonTty()
    stops = 0
    starts = 0
    published: list[bool] = []

    async def fake_publish_remote_tree_snapshot(*, force: bool = False) -> None:
        published.append(force)

    def fake_stop_remote_tree_publisher() -> None:
        nonlocal stops
        stops += 1

    def fake_start_remote_tree_publisher() -> None:
        nonlocal starts
        starts += 1

    monkeypatch.setattr("agent_zero_cli.connection._RECOVERY_DELAYS_SECONDS", (0.0,))
    monkeypatch.setattr(dummy_app, "_stop_remote_tree_publisher", fake_stop_remote_tree_publisher)
    monkeypatch.setattr(dummy_app, "_start_remote_tree_publisher", fake_start_remote_tree_publisher)
    monkeypatch.setattr(dummy_app, "_publish_remote_tree_snapshot", fake_publish_remote_tree_snapshot)

    dummy_app.config.instance_url = "http://agent.test"
    dummy_app.client = client  # type: ignore[assignment]
    dummy_app._python_tty = tty  # type: ignore[assignment]
    dummy_app.connected = True
    dummy_app.agent_active = True
    dummy_app.current_context = "ctx-1"
    dummy_app.current_context_has_messages = True
    dummy_app._context_run_complete = False

    await connection._recover_websocket(dummy_app)

    input_widget = dummy_app._test_widgets["#message-input"]  # type: ignore[index]
    log = dummy_app._test_widgets["#chat-log"]  # type: ignore[index]
    status = dummy_app._test_widgets["#connection-status"]  # type: ignore[index]
    splash = dummy_app._test_widgets["#splash-view"]  # type: ignore[index]

    assert client.connect_calls == 1
    assert client.subscribe_calls == ["ctx-1"]
    assert client.hello_calls[-1]["context_id"] == "ctx-1"
    assert tty.exec_configs == [{"version": 1}]
    assert published == [True]
    assert stops == 1
    assert starts == 1
    assert dummy_app.connected is True
    assert dummy_app.agent_active is False
    assert dummy_app.current_context == "ctx-1"
    assert dummy_app.current_context_has_messages is True
    assert dummy_app._context_run_complete is True
    assert dummy_app._websocket_recovery_task is None
    assert input_widget.disabled is False
    assert log.cleared is False
    assert status.status == "connected"
    assert splash.state.stage == "ready"
    assert splash.state.message == "Reconnected."


async def test_recover_websocket_retries_past_bounded_delays(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FlakyClient:
        def __init__(self) -> None:
            self.base_url = "http://agent.test"
            self.connect_calls = 0

        async def connect_websocket(self) -> None:
            self.connect_calls += 1
            if self.connect_calls < 4:
                raise ConnectionError("refused")

        async def send_hello(self, **payload: object) -> dict[str, object]:
            return {"exec_config": {"version": 1}}

        async def subscribe_context(self, context_id: str, **kwargs) -> dict[str, object]:
            del kwargs
            return {}

    class FakePythonTty:
        def set_exec_config(self, config: object) -> None:
            del config

    client = FlakyClient()
    monkeypatch.setattr("agent_zero_cli.connection._RECOVERY_DELAYS_SECONDS", (0.0, 0.0))
    monkeypatch.setattr("agent_zero_cli.connection._RECOVERY_STEADY_DELAY_SECONDS", 0.0)
    monkeypatch.setattr(dummy_app, "_stop_remote_tree_publisher", lambda: None)
    monkeypatch.setattr(dummy_app, "_start_remote_tree_publisher", lambda: None)

    async def fake_publish_remote_tree_snapshot(**kwargs: object) -> None:
        return None

    monkeypatch.setattr(
        dummy_app, "_publish_remote_tree_snapshot", fake_publish_remote_tree_snapshot
    )

    dummy_app.config.instance_url = "http://agent.test"
    dummy_app.client = client  # type: ignore[assignment]
    dummy_app._python_tty = FakePythonTty()  # type: ignore[assignment]
    dummy_app.connected = True
    dummy_app.agent_active = True
    dummy_app.current_context = "ctx-1"
    dummy_app.current_context_has_messages = True
    dummy_app._context_run_complete = False

    await connection._recover_websocket(dummy_app)

    # Two bounded delays are configured, but recovery must keep trying on the
    # steady cadence instead of giving up after exhausting them.
    assert client.connect_calls == 4
    assert dummy_app.connected is True


async def test_recover_websocket_stops_when_host_changes(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class HostChangingClient:
        def __init__(self) -> None:
            self.base_url = "http://agent.test"
            self.connect_calls = 0
            self.hello_calls = 0

        async def connect_websocket(self) -> None:
            self.connect_calls += 1
            # The user connected to a different host meanwhile; that
            # connection owns the client now.
            self.base_url = "http://other.test"
            raise ConnectionError("stale connection")

        async def send_hello(self, **payload: object) -> dict[str, object]:
            self.hello_calls += 1
            return {}

    client = HostChangingClient()
    monkeypatch.setattr("agent_zero_cli.connection._RECOVERY_DELAYS_SECONDS", (0.0,))
    monkeypatch.setattr("agent_zero_cli.connection._RECOVERY_STEADY_DELAY_SECONDS", 0.0)
    monkeypatch.setattr(dummy_app, "_stop_remote_tree_publisher", lambda: None)

    dummy_app.config.instance_url = "http://agent.test"
    dummy_app.client = client  # type: ignore[assignment]
    dummy_app.connected = False
    dummy_app.agent_active = False
    dummy_app.current_context = "ctx-1"
    dummy_app.current_context_has_messages = True
    dummy_app._context_run_complete = False

    await connection._recover_websocket(dummy_app)

    assert client.connect_calls == 1
    assert client.hello_calls == 0
    assert dummy_app._websocket_recovery_task is None


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


def test_copy_visible_chat_action_uses_visible_transcript(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    copied: list[str] = []
    notifications: list[tuple[str, dict[str, object]]] = []
    log = dummy_app._test_widgets["#chat-log"]  # type: ignore[index]
    log.copy_text = "first visible line\nsecond visible line"

    monkeypatch.setattr(dummy_app, "copy_to_clipboard", lambda text: copied.append(text))
    monkeypatch.setattr(
        dummy_app,
        "notify",
        lambda message, **kwargs: notifications.append((message, kwargs)),
    )

    dummy_app.action_copy_visible_chat()

    assert log.copy_visible_only is True
    assert copied == ["first visible line\nsecond visible line"]
    assert notifications == [
        (
            "Copied visible transcript to clipboard.",
            {
                "title": "Clipboard",
                "severity": "information",
                "timeout": 3,
                "markup": False,
            },
        )
    ]


def test_copy_visible_chat_action_reports_empty_transcript(
    dummy_app: DummyAgentZeroCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    copied: list[str] = []
    notices: list[tuple[str, bool]] = []
    log = dummy_app._test_widgets["#chat-log"]  # type: ignore[index]
    log.copy_text = ""

    monkeypatch.setattr(dummy_app, "copy_to_clipboard", lambda text: copied.append(text))
    monkeypatch.setattr(
        dummy_app,
        "_show_notice",
        lambda message, *, error=False: notices.append((message, error)),
    )

    dummy_app.action_copy_visible_chat()

    assert copied == []
    assert notices == [("Nothing visible to copy.", True)]


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


async def test_chat_log_caches_rendered_content_until_updated() -> None:
    app = TranscriptSelectionApp()

    async with app.run_test() as pilot:
        log = app.query_one("#chat-log", ChatLog)
        log.append_or_update(1, Panel("First version", padding=(0, 1)))
        await pilot.pause()

        widget = log._seq_to_widget[1]
        first = widget.render()
        assert widget.render() is first
        assert hasattr(widget._render_cache, "lines")

        widget.update(Panel("Second version", padding=(0, 1)))
        assert widget.render() is not first
        await pilot.pause()
        assert hasattr(widget._render_cache, "lines")


async def test_chat_log_prepends_older_history_before_loaded_entries() -> None:
    app = TranscriptSelectionApp()

    async with app.run_test() as pilot:
        log = app.query_one("#chat-log", ChatLog)
        log.append_or_update(3, Panel("newer message", padding=(0, 1)))
        for sequence in reversed((1, 2)):
            log.append_or_update(
                sequence,
                Panel(f"older message {sequence}", padding=(0, 1)),
                prepend=True,
            )
        await pilot.pause()

        timeline = [child for child in log.children if isinstance(child, SelectableStatic)]
        assert "older message 1" in timeline[0].copy_text()
        assert "older message 2" in timeline[1].copy_text()
        assert "newer message" in timeline[2].copy_text()


async def test_chat_log_requests_another_history_page_only_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = TranscriptSelectionApp()

    async with app.run_test() as pilot:
        log = app.query_one("#chat-log", ChatLog)
        for sequence in range(10):
            log.append_or_update(sequence, Panel(f"message {sequence}", padding=(0, 1)))
        await pilot.pause()

        requests: list[int] = []
        original_post_message = log.post_message

        def capture_history_request(message):
            if isinstance(message, ChatLog.HistoryRequested):
                requests.append(message.before)
            return original_post_message(message)

        monkeypatch.setattr(log, "post_message", capture_history_request)
        log.set_history_page(before=100, has_more=True)
        log.action_scroll_home()
        log.action_scroll_home()

        assert requests == [100]


async def test_chat_log_copyable_text_prefers_visible_children() -> None:
    app = TranscriptSelectionApp()

    async with app.run_test() as pilot:
        log = app.query_one("#chat-log", ChatLog)
        for sequence in range(8):
            log.append_or_update(
                sequence,
                Panel(f"copyable row {sequence}", border_style="#555555", padding=(0, 1)),
            )
        await pilot.pause()

        visible_text = log.copyable_text(visible_only=True)
        all_text = log.copyable_text(visible_only=False)

        assert "copyable row 7" in visible_text
        assert "copyable row 0" not in visible_text
        assert "copyable row 0" in all_text
        assert not any(line.endswith(" ") for line in visible_text.splitlines())


async def test_chat_log_nested_plain_strings_render_brackets_literally() -> None:
    app = TranscriptSelectionApp()
    path_like_text = "[/a0/tests/test_a0_connector_prompt_gating.py]"

    async with app.run_test() as pilot:
        log = app.query_one("#chat-log", ChatLog)
        log.append_or_update(
            1,
            Panel(path_like_text, border_style="#555555", padding=(0, 1)),
        )
        await pilot.pause()

        widget = log._seq_to_widget[1]
        assert path_like_text in widget.render().plain


async def test_connector_events_render_markup_sensitive_text_literally() -> None:
    app = TranscriptSelectionApp()
    path_like_text = "[/a0/tests/test_a0_connector_prompt_gating.py]"
    events = [
        ("user_message", {"text": path_like_text}),
        ("warning", {"heading": "Warning", "text": path_like_text}),
        ("error", {"heading": "Error", "text": path_like_text}),
        ("info", {"heading": "Info", "text": path_like_text}),
        ("util_message", {"heading": "Utility", "text": path_like_text}),
    ]

    async with app.run_test() as pilot:
        log = app.query_one("#chat-log", ChatLog)
        for sequence, (event_type, data) in enumerate(events, start=1):
            rendered = render_connector_event(
                log,
                {
                    "event": event_type,
                    "sequence": sequence,
                    "data": data,
                },
            )
            assert rendered is True
        await pilot.pause()

        transcript = "\n".join(
            widget.render().plain for widget in log._seq_to_widget.values()
        )

    assert transcript.count(path_like_text) == len(events)
    assert "[yellow]" not in transcript
    assert "[red]" not in transcript
    assert "[dim]" not in transcript


async def test_connector_code_event_renders_compact_details() -> None:
    app = TranscriptSelectionApp()

    async with app.run_test(size=(88, 20)) as pilot:
        log = app.query_one("#chat-log", ChatLog)
        rendered = render_connector_event(
            log,
            {
                "event": "code_output",
                "sequence": 1,
                "data": {
                    "heading": "icon://terminal [0] sed -n",
                    "text": '<div align="center">',
                    "meta": {"code": "sed -n '1,2p' /a0/README.md"},
                },
            },
        )
        assert rendered is True
        await pilot.pause()

        widget = log._seq_to_widget[1]
        transcript = widget.render().plain

    assert "Running code" in transcript
    assert "sed -n '1,2p' /a0/README.md" in transcript
    assert '<div align="center">' in transcript
    assert len(transcript.splitlines()) <= 5


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
