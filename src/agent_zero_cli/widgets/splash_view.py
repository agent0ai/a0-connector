from __future__ import annotations

from dataclasses import dataclass, replace
from urllib.parse import urlparse
from typing import Literal, Sequence, TypeAlias

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import Button, Checkbox, Input, LoadingIndicator, Static

from agent_zero_cli.client import DEFAULT_HOST
from agent_zero_cli.instance_discovery import DiscoveredInstance, DiscoveryStatus
from agent_zero_cli.widgets.chat_log import build_agent_zero_banner_widget


SplashStage: TypeAlias = Literal["host", "login", "connecting", "ready", "error"]

_STAGE_ORDER: tuple[SplashStage, ...] = ("host", "login", "connecting", "ready", "error")
_STAGE_LABELS: dict[SplashStage, str] = {
    "host": "Connect",
    "login": "",
    "connecting": "Connecting",
    "ready": "Ready",
    "error": "Connection issue",
}


def _connection_target_summary(host: str) -> tuple[str, str, bool]:
    normalized_host = _normalize_connection_target(host) or DEFAULT_HOST
    try:
        parsed = urlparse(normalized_host)
    except ValueError:
        # urlparse can raise for malformed values (for example invalid IPv6
        # bracket syntax). Keep the splash resilient and never crash render.
        is_secure = normalized_host.lower().startswith("https://")
        return "Connector endpoint", normalized_host, is_secure
    scheme = (parsed.scheme or "http").lower()
    hostname = (parsed.hostname or "").strip()
    try:
        port = parsed.port
    except ValueError:
        # Keep the splash resilient when users type malformed ports; this should
        # never crash the login surface.
        port = None

    if hostname in {"127.0.0.1", "localhost", "::1"}:
        label = "Local connector"
    elif hostname:
        label = hostname
    else:
        label = "Connector endpoint"

    if port and hostname and hostname not in {"127.0.0.1", "localhost", "::1"}:
        label = f"{label}:{port}"

    return label, normalized_host, scheme == "https"


def _normalize_connection_target(host: str) -> str:
    raw_host = host.strip()
    if not raw_host:
        return ""
    if raw_host.startswith("//"):
        return f"http:{raw_host}".rstrip("/")
    if "://" not in raw_host:
        return f"http://{raw_host}".rstrip("/")
    return raw_host.rstrip("/")


def _validate_connection_target(host: str) -> tuple[bool, str]:
    raw_host = host.strip()
    if not raw_host:
        return True, ""
    normalized_host = _normalize_connection_target(raw_host)

    try:
        parsed = urlparse(normalized_host)
    except ValueError:
        return False, "Invalid URL format. Use http://host[:port] or https://host[:port]."

    scheme = (parsed.scheme or "").lower()
    if scheme not in {"http", "https"}:
        return False, "Invalid URL format. Include http:// or https://."

    if not parsed.hostname:
        return False, "Invalid URL format. Include a hostname or IP."

    try:
        _ = parsed.port
    except ValueError:
        return False, "Invalid URL format. Port must be numeric."

    if normalized_host != raw_host:
        return True, f"Will connect to {normalized_host}"

    return True, ""


@dataclass(frozen=True)
class SplashAction:
    key: str
    title: str
    description: str = ""
    enabled: bool = True
    disabled_reason: str = ""


@dataclass(frozen=True)
class SplashState:
    stage: SplashStage
    message: str = ""
    detail: str = ""
    host: str = ""
    username: str = ""
    password: str = ""
    remember_host: bool = False
    login_error: str = ""
    actions: Sequence[SplashAction] = ()
    discovered_instances: Sequence[DiscoveredInstance] = ()
    discovery_status: DiscoveryStatus = "loading"
    discovery_detail: str = ""
    selected_host_url: str = ""
    manual_entry_expanded: bool = False
    local_workspace: str = ""
    remote_workspace: str = ""


class SplashHostRow(Vertical):
    can_focus = True

    class Highlighted(Message):
        def __init__(self, *, host: str) -> None:
            super().__init__()
            self.host = host

    class Selected(Message):
        def __init__(self, *, host: str) -> None:
            super().__init__()
            self.host = host

    def __init__(self, instance: DiscoveredInstance) -> None:
        super().__init__(classes="splash-host-row")
        self._instance = instance
        self.tooltip = Text(instance.status_text or instance.url)

    def compose(self) -> ComposeResult:
        with Vertical(classes="splash-host-row-body"):
            yield Static(self._instance.name or "Agent Zero", classes="splash-host-row-title", markup=False)
            yield Static(self._instance.url, classes="splash-host-row-meta", markup=False)

    @property
    def host(self) -> str:
        return self._instance.url

    def on_focus(self) -> None:
        self.add_class("-highlight")
        self.post_message(self.Highlighted(host=self.host))

    def on_blur(self) -> None:
        self.remove_class("-highlight")

    def on_click(self, event: events.Click) -> None:
        self.add_class("-highlight")
        self.post_message(self.Selected(host=self.host))
        event.stop()

    def on_key(self, event: events.Key) -> None:
        if event.key in {"up", "down"} and self.parent is not None:
            rows = [row for row in self.parent.children if isinstance(row, SplashHostRow)]
            index = rows.index(self)
            next_index = max(0, min(index + (-1 if event.key == "up" else 1), len(rows) - 1))
            rows[next_index].focus()
            event.prevent_default()
            event.stop()
            return

        if event.key in {"enter", "space"}:
            self.post_message(self.Selected(host=self.host))
            event.stop()


class SplashHostPanel(Vertical):
    """Docker-backed connection picker with manual URL fallback."""

    DEFAULT_CSS = """
    SplashHostPanel {
        layout: vertical;
    }
    """

    def __init__(self) -> None:
        super().__init__(id="splash-host-panel")
        self._state = SplashState(stage="host")
        self._title = Static("Choose a host", classes="splash-panel-title")
        self._rendered_instances: tuple[DiscoveredInstance, ...] | None = None
        self._host_valid = True
        self._loading = LoadingIndicator(id="splash-host-loading")
        self._status = Static("", id="splash-host-status")
        self._instances_mount = Vertical(id="splash-host-list-mount")
        self._host = Input(placeholder=DEFAULT_HOST, id="splash-host-input")
        self._validation = Static("", id="splash-host-validation")
        self._button = Button("Connect", id="splash-host-submit", variant="primary")
        self._remember_host = Checkbox("Remember this host", id="splash-host-remember")
        self._refresh = Button("Refresh", id="splash-host-refresh")
        self._manual_toggle = Button("Enter a URL", id="splash-host-toggle-manual")
        self._hint = Static(
            "↑ ↓ Select   Enter Connect   Tab Move",
            classes="splash-panel-hint",
        )
        self._manual_section = Vertical(id="splash-manual-section")

    def compose(self) -> ComposeResult:
        with Horizontal(id="splash-host-header"):
            yield self._title
            yield self._refresh
        yield self._loading
        yield self._status
        yield self._instances_mount
        with self._manual_section:
            yield self._host
            yield self._validation
        with Horizontal(id="splash-host-primary-actions"):
            yield self._remember_host
            yield self._button
        yield self._hint
        yield self._manual_toggle

    def on_mount(self) -> None:
        self._sync_ui()

    def set_state(self, state: SplashState) -> None:
        self._state = state
        if self.is_mounted:
            self._sync_ui()

    def _sync_ui(self) -> None:
        self._title.update("Connect by URL" if self._state.manual_entry_expanded else "Choose a host")
        self.set_host(self._state.host)
        self._remember_host.value = bool(self._state.remember_host)
        self._sync_discovery_status()
        self._rebuild_instance_list()
        self._manual_section.display = bool(self._state.manual_entry_expanded)
        self._manual_toggle.label = "Choose a local host" if self._state.manual_entry_expanded else "Enter a URL"
        self._hint.update(
            "Enter Connect   Tab Move" if self._state.manual_entry_expanded
            else "↑ ↓ Select   Enter Connect   Tab Move"
        )
        self.refresh_validation()
        self._sync_connect_button()

    def set_host(self, host: str) -> None:
        host_text = host.strip()
        if self._host.has_focus and self._host.value.strip():
            self._host.placeholder = DEFAULT_HOST
            self.refresh_validation()
            return
        self._host.value = "" if host_text == DEFAULT_HOST else host_text
        self._host.placeholder = DEFAULT_HOST
        self.refresh_validation()

    def _safe_update(self, widget: Static, renderable: Text | str) -> None:
        if not self.is_mounted:
            return
        try:
            widget.update(renderable)
        except Exception:
            # During splash transitions widgets can briefly be unmounted.
            # Ignore transient update failures and let the next sync repaint.
            pass

    def _sync_discovery_status(self) -> None:
        status = self._state.discovery_status
        self._loading.display = status == "loading" and not self._state.manual_entry_expanded
        self._safe_update(self._status, self._status_message())
        self._status.display = status != "ready" and (
            not self._state.manual_entry_expanded or status in {"empty", "unavailable", "error"}
        )

    def _status_message(self) -> Text | str:
        status = self._state.discovery_status
        detail = self._state.discovery_detail.strip()
        if status == "loading":
            return Text("Looking for local instances…", style="#9aa7b4")
        if status == "ready":
            return ""
        if status == "empty":
            message = detail or "No local Agent Zero Docker instances were found."
            return Text(f"{message} Install Agent Zero: http://agent-zero.ai", style="#9aa7b4")
        if status == "unavailable":
            return Text(detail or "Docker is unavailable. Enter a URL manually.", style="#f0b54d")
        return Text(detail or "Docker discovery failed. Enter a URL manually.", style="#ff8b6b")

    def _rebuild_instance_list(self) -> None:
        instances = tuple(self._state.discovered_instances)
        self._instances_mount.display = bool(instances) and not self._state.manual_entry_expanded
        if instances != self._rendered_instances:
            self._rendered_instances = instances
            for child in list(self._instances_mount.children):
                child.remove()
            for instance in instances:
                row = SplashHostRow(instance)
                row.set_class(instance.url == self.selected_host_url, "-selected")
                self._instances_mount.mount(row)
        for row in self._instances_mount.query(SplashHostRow):
            row.set_class(row.host == self.selected_host_url, "-selected")
        self._instances_mount.styles.height = min(len(instances), 3) * 3

    def _sync_connect_button(self) -> None:
        if self._state.manual_entry_expanded:
            self._button.disabled = not self._host_valid or not self._host.value.strip()
            return
        if self._state.discovered_instances:
            self._button.disabled = not bool(self.selected_host_url)
            return
        self._button.disabled = True

    def refresh_validation(self) -> None:
        valid, message = _validate_connection_target(self._host.value)
        self._host_valid = valid

        if message.strip():
            color = "#9aa7b4" if valid else "#ff8b6b"
            self._safe_update(self._validation, Text(message, style=color))
        else:
            self._safe_update(self._validation, "Local or remote URL · port optional")
        self._validation.display = self._state.manual_entry_expanded
        self._sync_connect_button()

    def focus_input(self) -> None:
        if self._state.manual_entry_expanded:
            self._host.focus()
            return
        try:
            selected_host = self.selected_host_url
            if selected_host:
                for row in self.query(SplashHostRow):
                    if row.host == selected_host:
                        row.focus()
                        return
            self.query_one(SplashHostRow).focus()
        except Exception:
            self._manual_toggle.focus()

    @property
    def host(self) -> str:
        return _normalize_connection_target(self._host.value) or DEFAULT_HOST

    @property
    def is_valid(self) -> bool:
        return self._host_valid

    @property
    def selected_host_url(self) -> str:
        selected = self._state.selected_host_url.strip()
        if selected:
            return selected
        if self._state.discovered_instances:
            return str(self._state.discovered_instances[0].url)
        return ""

    @property
    def connect_host(self) -> str:
        if self._state.manual_entry_expanded:
            return _normalize_connection_target(self._host.value)
        return self.selected_host_url

    @property
    def remember_host(self) -> bool:
        return bool(self._remember_host.value)


class SplashLoginPanel(Vertical):
    """Username/password panel shown when the selected host requires web login."""

    DEFAULT_CSS = """
    SplashLoginPanel {
        layout: vertical;
    }
    """

    def __init__(self) -> None:
        super().__init__(id="splash-login-panel")
        _, target_host, target_secure = _connection_target_summary(DEFAULT_HOST)
        self._target_host = target_host
        self._target_secure = target_secure
        self._target_detected_label = ""
        self._login_error = ""
        self._title = Static("Ready to login", classes="splash-panel-title")
        self._copy = Static(
            "Sign in to the Agent Zero instance below.",
            classes="splash-panel-copy",
        )
        self._target_summary = Static("", id="splash-login-target-summary")
        self._target_url = Static("", id="splash-login-target-url")
        self._username = Input(placeholder="Username", id="splash-login-username")
        self._password = Input(placeholder="Password", password=True, id="splash-login-password")
        self._remember_host = Checkbox("Remember this host", id="splash-login-remember")
        self._back_button = Button("Change URL", id="splash-login-back")
        self._button = Button("Login", id="splash-login-submit", variant="primary")

    def compose(self) -> ComposeResult:
        yield self._title
        yield self._copy
        yield self._target_summary
        with Horizontal(id="splash-login-target-row"):
            yield self._target_url
            yield self._back_button
        yield self._username
        yield self._password
        yield self._remember_host
        yield self._button

    def on_mount(self) -> None:
        self._render_target_context()

    def set_credentials(self, username: str = "", password: str = "", *, remember_host: bool = False) -> None:
        self._username.value = username
        self._password.value = password
        self._remember_host.value = remember_host

    def _safe_focus(self, widget: Input) -> None:
        try:
            widget.focus()
        except Exception:
            pass

    def set_target(self, host: str, *, discovered_instance: DiscoveredInstance | None = None) -> None:
        _, normalized_host, is_secure = _connection_target_summary(host)
        self._target_host = normalized_host
        self._target_secure = is_secure
        self._target_detected_label = (
            (discovered_instance.status_text or discovered_instance.name).strip() if discovered_instance else ""
        )
        self._render_target_context()

    def set_error(self, message: str = "") -> None:
        self._login_error = message.strip()
        self._render_target_context()

    def clear_error(self) -> None:
        self.set_error("")

    def _safe_update(self, widget: Static, renderable: Text | str) -> None:
        if not self.is_mounted:
            return
        try:
            widget.update(renderable)
        except Exception:
            # During splash transitions widgets can briefly be unmounted.
            # Ignore transient update failures and let the next sync repaint.
            pass

    def _render_target_context(self) -> None:
        target_render: Text | str = self._target_host
        summary_visible = True
        if self._login_error:
            summary: Text | str = Text.assemble(
                ("Login issue ", "bold #ff8b6b"),
                (self._login_error, "#ff8b6b"),
            )
        else:
            security_tag = "[TLS unverified]" if self._target_secure else "[insecure]"
            security_style = "#f0b54d"
            if self._target_detected_label:
                summary = Text.assemble(
                    ("Detected A0 instance ", "bold #79d18a"),
                    (self._target_detected_label, "#9ecfff"),
                )
            else:
                summary = ""
                summary_visible = False
            target_render = Text.assemble(
                (self._target_host, "#9aa7b4"),
                (f"  {security_tag}", security_style),
            )

        copy_text = (
            "Use the same username and password you use in the Agent Zero Web UI."
            if self._target_detected_label
            else "Login with the Agent Zero endpoint below."
        )
        self._safe_update(self._copy, copy_text)
        self._safe_update(self._target_summary, summary)
        self._safe_update(self._target_url, target_render)
        if not self.is_mounted:
            return
        try:
            self._target_summary.display = summary_visible
        except Exception:
            pass

    @property
    def error_message(self) -> str:
        return self._login_error

    @property
    def target_host(self) -> str:
        return self._target_host

    def focus_input(self) -> None:
        if self._username.value:
            self._safe_focus(self._password)
        else:
            self._safe_focus(self._username)

    def focus_missing_field(self) -> None:
        if not self.username:
            self._safe_focus(self._username)
            return
        if not self.password:
            self._safe_focus(self._password)

    def focus_password(self) -> None:
        self._safe_focus(self._password)

    @property
    def username(self) -> str:
        return self._username.value.strip()

    @property
    def password(self) -> str:
        return self._password.value

    @property
    def remember_host(self) -> bool:
        return bool(self._remember_host.value)


class SplashStatusPanel(Vertical):
    """Connecting / error status panel."""

    DEFAULT_CSS = """
    SplashStatusPanel {
        layout: vertical;
    }
    """

    def __init__(self) -> None:
        super().__init__(id="splash-status-panel")
        self._spinner = LoadingIndicator(id="splash-status-spinner")
        self._title = Static("", id="splash-status-title")
        self._detail = Static("", id="splash-status-detail")
        self._actions = Horizontal(id="splash-status-actions")
        self._back_button = Button("Back", id="splash-status-back")
        self._button = Button("Try again", id="splash-status-retry", variant="primary")
        self._spinner.display = True
        self._actions.display = False
        self._back_button.display = False
        self._button.display = False

    def compose(self) -> ComposeResult:
        yield self._spinner
        yield self._title
        yield self._detail
        with self._actions:
            yield self._back_button
            yield self._button

    def set_connecting(self, message: str, detail: str = "") -> None:
        self._spinner.display = True
        self._actions.display = False
        self._back_button.display = False
        self._button.display = False
        self._title.display = True
        self._detail.display = True
        self._title.update(Text(message or "Connecting to the connector...", style="bold"))
        self._detail.update(detail)

    def set_error(self, message: str, detail: str = "") -> None:
        self._spinner.display = False
        self._actions.display = True
        self._back_button.display = True
        self._button.display = True
        self._title.display = False
        self._detail.display = False
        self._title.update("")
        self._detail.update("")

    def focus_primary(self) -> None:
        try:
            if self._back_button.display:
                self._back_button.focus()
                return
            if self._button.display:
                self._button.focus()
        except Exception:
            pass


class SplashActionCard(Vertical):
    """A reusable action card with a button and two lines of explanation."""

    DEFAULT_CSS = """
    SplashActionCard {
        layout: vertical;
    }
    """

    def __init__(self, key: str, *, index: int) -> None:
        super().__init__(id=f"splash-action-{index}", classes="splash-action-card")
        self.key = key
        self._button = Button("", id=f"splash-action-button-{index}")
        self._description = Static("", classes="splash-action-description")
        self._reason = Static("", classes="splash-action-reason")

    def compose(self) -> ComposeResult:
        yield self._button
        yield self._description
        yield self._reason

    def set_action(self, action: SplashAction | None) -> None:
        if action is None:
            self.display = False
            return

        self.display = True
        self._button.label = action.title
        self._button.disabled = not action.enabled
        self._description.update(action.description)
        self._reason.update(action.disabled_reason)
        self._reason.display = bool(action.disabled_reason)

    @property
    def enabled(self) -> bool:
        return not self._button.disabled


class SplashActionDeck(Grid):
    """Grid of welcome / command actions."""

    DEFAULT_CSS = """
    SplashActionDeck {
        layout: grid;
        grid-size: 2;
    }
    """

    def __init__(self) -> None:
        super().__init__(id="splash-actions-grid")
        self._cards: dict[str, SplashActionCard] = {}
        self._button_to_key: dict[str, str] = {}
        self._last_actions: tuple[SplashAction, ...] = ()

    def compose(self) -> ComposeResult:
        yield from ()

    def set_actions(self, actions: Sequence[SplashAction]) -> None:
        self._last_actions = tuple(actions)
        if not self.is_mounted:
            return
        self._rebuild_cards(self._last_actions)

    def on_mount(self) -> None:
        self._rebuild_cards(self._last_actions)

    def _rebuild_cards(self, actions: Sequence[SplashAction]) -> None:
        self._button_to_key.clear()
        for child in list(self.children):
            child.remove()
        self._cards.clear()

        for index, action in enumerate(actions):
            card = SplashActionCard(action.key, index=index)
            self._cards[action.key] = card
            self.mount(card)
            card.set_action(action)
            button_id = f"splash-action-button-{index}"
            if button_id:
                self._button_to_key[button_id] = action.key

    def action_for_button_id(self, button_id: str) -> str | None:
        key = self._button_to_key.get(button_id)
        if key and key in self._cards and self._cards[key].enabled:
            return key
        return None


class SplashView(VerticalScroll):
    """Staged splash / welcome surface driven by app state."""

    DEFAULT_CSS = """
    SplashView {
        layout: vertical;
        overflow-y: auto;
    }
    """

    class SubmitRequested(Message):
        def __init__(self, *, stage: SplashStage, host: str, username: str, password: str, remember_host: bool) -> None:
            super().__init__()
            self.stage = stage
            self.host = host
            self.username = username
            self.password = password
            self.remember_host = remember_host

    class ActionRequested(Message):
        def __init__(self, action: str) -> None:
            super().__init__()
            self.action = action

    class HostStateChanged(Message):
        def __init__(self, *, host: str, selected_host_url: str, manual_entry_expanded: bool, remember_host: bool) -> None:
            super().__init__()
            self.host = host
            self.selected_host_url = selected_host_url
            self.manual_entry_expanded = manual_entry_expanded
            self.remember_host = remember_host

    class RememberHostChanged(Message):
        def __init__(self, *, remember_host: bool) -> None:
            super().__init__()
            self.remember_host = remember_host

    def __init__(self) -> None:
        super().__init__(id="splash-view")
        self._hero = build_agent_zero_banner_widget(id="splash-hero")
        self._stage_label = Static("", id="splash-stage-label")
        self._message = Static("", id="splash-message")
        self._detail = Static("", id="splash-detail")
        self._host_panel = SplashHostPanel()
        self._login_panel = SplashLoginPanel()
        self._status_panel = SplashStatusPanel()
        self._actions = SplashActionDeck()
        self._state = SplashState(stage="host")
        self._suppress_host_state_events = False

    def compose(self) -> ComposeResult:
        with Vertical(id="splash-content"):
            yield self._hero
            yield self._stage_label
            yield self._message
            yield self._detail
            yield self._host_panel
            yield self._login_panel
            yield self._status_panel
            yield self._actions

    def on_resize(self, event: events.Resize) -> None:
        self.update_classes({"compact": event.size.height < 28, "narrow": event.size.width < 56})

    def on_mount(self) -> None:
        self._sync_state()
        self.focus_primary()

    def _apply_stage(self, stage: SplashStage) -> None:
        self.update_classes({f"stage-{value}": value == stage for value in _STAGE_ORDER})

        self._host_panel.display = stage == "host"
        self._login_panel.display = stage == "login"
        self._status_panel.display = stage in {"connecting", "error"}
        self._actions.display = stage == "ready"

    def _default_actions(self) -> Sequence[SplashAction]:
        return ()

    def _sync_state(self) -> None:
        self._apply_stage(self._state.stage)
        show_header_copy = self._state.stage not in {"host", "login"}
        stage_label_text = _STAGE_LABELS.get(self._state.stage, self._state.stage.title())
        self._stage_label.update(Text(stage_label_text, style="bold"))
        self._stage_label.display = show_header_copy and bool(stage_label_text.strip())

        self._message.update(self._state.message)
        self._message.display = show_header_copy and bool(self._state.message.strip())
        self._detail.update(self._state.detail)
        self._detail.display = show_header_copy and bool(self._state.detail.strip())
        self._suppress_host_state_events = True
        try:
            self._host_panel.set_state(self._state)
        finally:
            self._suppress_host_state_events = False
        self._login_panel.set_credentials(
            self._state.username,
            self._state.password,
            remember_host=self._state.remember_host,
        )
        discovered_instance = next(
            (instance for instance in self._state.discovered_instances if instance.url == self._state.host),
            None,
        )
        self._login_panel.set_target(self._state.host, discovered_instance=discovered_instance)
        self._login_panel.set_error(self._state.login_error)

        if self._state.stage == "connecting":
            self._status_panel.set_connecting(
                self._state.message or _STAGE_LABELS[self._state.stage],
                self._state.detail,
            )
        elif self._state.stage == "error":
            self._status_panel.set_error(
                self._state.message or _STAGE_LABELS[self._state.stage],
                self._state.detail,
            )

        self._actions.set_actions(
            self._state.actions or (self._default_actions() if self._state.stage == "ready" else ())
        )

    def set_state(self, state: SplashState) -> None:
        refocus = (
            state.stage != self._state.stage
            or state.manual_entry_expanded != self._state.manual_entry_expanded
            or state.discovered_instances != self._state.discovered_instances
        )
        self._state = state
        if self.is_mounted:
            self._sync_state()
            if refocus and state.stage in {"host", "login", "error"}:
                self.focus_primary()

    def set_stage(
        self,
        stage: SplashStage,
        *,
        message: str = "",
        detail: str = "",
        host: str = "",
        username: str = "",
        password: str = "",
        remember_host: bool = False,
        login_error: str = "",
        actions: Sequence[SplashAction] | None = None,
    ) -> None:
        self.set_state(
            SplashState(
                stage=stage,
                message=message,
                detail=detail,
                host=host,
                username=username,
                password=password,
                remember_host=remember_host,
                login_error=login_error,
                actions=actions or (self._default_actions() if stage == "ready" else ()),
                discovered_instances=self._state.discovered_instances,
                discovery_status=self._state.discovery_status,
                discovery_detail=self._state.discovery_detail,
                selected_host_url=self._state.selected_host_url,
                manual_entry_expanded=self._state.manual_entry_expanded,
                local_workspace=self._state.local_workspace,
                remote_workspace=self._state.remote_workspace,
            )
        )

    def set_actions(self, actions: Sequence[SplashAction]) -> None:
        self._state = replace(
            self._state,
            actions=actions or (self._default_actions() if self._state.stage == "ready" else ()),
        )
        if self.is_mounted:
            self._sync_state()

    def focus_primary(self) -> None:
        if not self.is_mounted:
            return
        if self.is_running:
            self.call_after_refresh(self._focus_primary_now)
        else:
            self._focus_primary_now()

    def _focus_primary_now(self) -> None:
        if not self.is_mounted:
            return
        if self._state.stage == "host":
            self._host_panel.focus_input()
        elif self._state.stage == "login":
            self._login_panel.focus_input()
        elif self._state.stage == "error":
            self._status_panel.focus_primary()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""

        if button_id == "splash-host-submit":
            self._submit_host()
            return

        if button_id == "splash-host-refresh":
            self.post_message(self.ActionRequested("refresh-hosts"))
            return

        if button_id == "splash-host-toggle-manual":
            self.post_message(self.ActionRequested("toggle-manual-host"))
            return

        if button_id == "splash-login-submit":
            self._submit_login()
            return

        if button_id == "splash-login-back":
            self._request_back_to_host()
            return

        if button_id == "splash-status-retry":
            self.post_message(self.ActionRequested("retry"))
            return

        if button_id == "splash-status-back":
            self._request_back_to_host()
            return

        action_key = self._actions.action_for_button_id(button_id)
        if action_key is not None:
            self.post_message(self.ActionRequested(action_key))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "splash-host-input":
            self._submit_host()
            return

        if event.input.id == "splash-login-username" and self._login_panel.username:
            self._login_panel.focus_password()
            return

        if event.input.id == "splash-login-password":
            self._submit_login()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "splash-host-input":
            self._host_panel.refresh_validation()
            self._sync_host_state()
            return

        if event.input.id in {"splash-login-username", "splash-login-password"}:
            if self._is_login_state_sync_event(event):
                return
            self._login_panel.clear_error()

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        checkbox_id = event.checkbox.id or ""
        if checkbox_id == "splash-host-remember":
            self._sync_host_state(remember_host=bool(event.value))
            return
        if checkbox_id == "splash-login-remember":
            remember_host = bool(event.value)
            if remember_host == self._state.remember_host:
                return
            self._state = replace(self._state, remember_host=remember_host)
            self.post_message(self.RememberHostChanged(remember_host=remember_host))

    def on_splash_host_row_highlighted(self, event: SplashHostRow.Highlighted) -> None:
        if self._state.stage != "host":
            return
        self._sync_host_state(selected_host_url=event.host)

    def on_splash_host_row_selected(self, event: SplashHostRow.Selected) -> None:
        host = event.host.strip()
        if not host:
            return
        self._sync_host_state(selected_host_url=host)
        self.post_message(
            self.SubmitRequested(
                stage="host",
                host=host,
                username="",
                password="",
                remember_host=self._host_panel.remember_host,
            )
        )

    def on_key(self, event: events.Key) -> None:
        if event.key == "escape" and self._state.stage in {"login", "error"}:
            self._request_back_to_host()
            event.stop()

    def _is_login_state_sync_event(self, event: Input.Changed) -> bool:
        if self._state.stage != "login":
            return False

        if event.input.id == "splash-login-username":
            return event.value == self._state.username

        if event.input.id == "splash-login-password":
            return event.value == self._state.password

        return False

    def _submit_login(self) -> None:
        username = self._login_panel.username
        password = self._login_panel.password
        if not username or not password:
            self._login_panel.set_error("Username and password are required.")
            self._login_panel.focus_missing_field()
            return

        self._login_panel.clear_error()
        self.post_message(
            self.SubmitRequested(
                stage="login",
                host=self._login_panel.target_host,
                username=username,
                password=password,
                remember_host=self._login_panel.remember_host,
            )
        )

    def _submit_host(self) -> None:
        manual_mode = self._state.manual_entry_expanded
        if manual_mode:
            self._host_panel.refresh_validation()
            if not self._host_panel.is_valid:
                self._host_panel.focus_input()
                return
            if not self._host_panel.connect_host:
                self._host_panel.focus_input()
                return
        elif not self._host_panel.selected_host_url:
            self._host_panel.focus_input()
            return
        self.post_message(
            self.SubmitRequested(
                stage="host",
                host=self._host_panel.connect_host,
                username="",
                password="",
                remember_host=self._host_panel.remember_host,
            )
        )

    def _request_back_to_host(self) -> None:
        self.post_message(self.ActionRequested("back"))

    def _sync_host_state(
        self,
        *,
        selected_host_url: str | None = None,
        remember_host: bool | None = None,
    ) -> None:
        if self._suppress_host_state_events or self._state.stage != "host":
            return

        next_selected_host = selected_host_url
        if next_selected_host is None:
            next_selected_host = self._host_panel.selected_host_url
        next_remember_host = self._host_panel.remember_host if remember_host is None else remember_host
        if self._state.manual_entry_expanded:
            next_host = self._host_panel._host.value.strip()
        else:
            next_host = self._host_panel.host
        if not self._state.manual_entry_expanded and next_selected_host:
            next_host = next_selected_host

        next_state = replace(
            self._state,
            host=next_host,
            selected_host_url=next_selected_host or "",
            remember_host=next_remember_host,
        )
        if next_state == self._state:
            return
        self._state = next_state
        self._host_panel.set_state(next_state)
        self.post_message(
            self.HostStateChanged(
                host=self._state.host,
                selected_host_url=self._state.selected_host_url,
                manual_entry_expanded=self._state.manual_entry_expanded,
                remember_host=self._state.remember_host,
            )
        )
