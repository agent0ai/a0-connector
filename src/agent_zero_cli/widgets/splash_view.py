from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse
from typing import Literal, Sequence, TypeAlias

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import Button, Checkbox, Input, LoadingIndicator, Static

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
_DEFAULT_HOST = "http://127.0.0.1:5080"


def _connection_target_summary(host: str) -> tuple[str, str, bool]:
    normalized_host = host.strip() or _DEFAULT_HOST
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


def _validate_connection_target(host: str) -> tuple[bool, str]:
    raw_host = host.strip()
    if not raw_host:
        return True, ""

    try:
        parsed = urlparse(raw_host)
    except ValueError:
        return False, "Invalid URL format. Use http://host[:port]."

    scheme = (parsed.scheme or "").lower()
    if scheme not in {"http", "https"}:
        return False, "Invalid URL format. Include http:// or https://."

    if not parsed.hostname:
        return False, "Invalid URL format. Include a hostname or IP."

    try:
        port = parsed.port
    except ValueError:
        return False, "Invalid URL format. Port must be numeric."

    if port is None:
        return False, "Missing port. Include :port (for example :5080)."

    return True, "URL format looks valid."


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
    save_credentials: bool = False
    login_error: str = ""
    actions: Sequence[SplashAction] = ()


class SplashHostPanel(Vertical):
    """Connection host entry panel for the staged splash."""

    DEFAULT_CSS = """
    SplashHostPanel {
        layout: vertical;
    }
    """

    def __init__(self) -> None:
        super().__init__(id="splash-host-panel")
        self._title = Static("Agent Zero WebUI URL", classes="splash-panel-title")
        self._copy = Static(
            "Local or remote endpoint.",
            classes="splash-panel-copy",
        )
        self._host_valid = True
        self._host = Input(placeholder=_DEFAULT_HOST, id="splash-host-input")
        self._validation = Static("", id="splash-host-validation")
        self._button = Button("Connect", id="splash-host-submit", variant="primary")
        self._hint = Static(
            "Press Enter to connect.",
            classes="splash-panel-hint",
        )

    def compose(self) -> ComposeResult:
        yield self._title
        yield self._copy
        yield self._host
        yield self._validation
        yield self._button
        yield self._hint

    def on_mount(self) -> None:
        self.refresh_validation()

    def set_host(self, host: str) -> None:
        host_text = host.strip()
        if self._host.has_focus and self._host.value.strip():
            self._host.placeholder = _DEFAULT_HOST
            self.refresh_validation()
            return
        self._host.value = "" if host_text == _DEFAULT_HOST else host_text
        self._host.placeholder = _DEFAULT_HOST
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

    def refresh_validation(self) -> None:
        valid, message = _validate_connection_target(self._host.value)
        self._host_valid = valid

        if message.strip():
            color = "#79d18a" if valid else "#ff8b6b"
            self._safe_update(self._validation, Text(message, style=color))
        self._validation.display = bool(message.strip())
        self._button.disabled = not valid

    def focus_input(self) -> None:
        self._host.focus()

    @property
    def host(self) -> str:
        return self._host.value.strip() or _DEFAULT_HOST

    @property
    def is_valid(self) -> bool:
        return self._host_valid


class SplashLoginPanel(Vertical):
    """Username/password panel shown when the connector advertises login auth."""

    DEFAULT_CSS = """
    SplashLoginPanel {
        layout: vertical;
    }
    """

    def __init__(self) -> None:
        super().__init__(id="splash-login-panel")
        _, target_host, target_secure = _connection_target_summary(_DEFAULT_HOST)
        self._target_host = target_host
        self._target_secure = target_secure
        self._login_error = ""
        self._title = Static("Sign in", classes="splash-panel-title")
        self._copy = Static(
            "Authenticate with the connector endpoint below.",
            classes="splash-panel-copy",
        )
        self._target_summary = Static("", id="splash-login-target-summary")
        self._target_url = Static("", id="splash-login-target-url")
        self._username = Input(placeholder="Username", id="splash-login-username")
        self._password = Input(placeholder="Password", password=True, id="splash-login-password")
        self._save_credentials = Checkbox("Save credentials", id="splash-save-credentials")
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
        yield self._save_credentials
        yield self._button

    def on_mount(self) -> None:
        self._render_target_context()

    def set_credentials(self, username: str = "", password: str = "", *, save: bool = False) -> None:
        self._username.value = username
        self._password.value = password
        self._save_credentials.value = save

    def _safe_focus(self, widget: Input) -> None:
        try:
            widget.focus()
        except Exception:
            pass

    def set_target(self, host: str) -> None:
        _, normalized_host, is_secure = _connection_target_summary(host)
        self._target_host = normalized_host
        self._target_secure = is_secure
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
            security_tag = "[secure]" if self._target_secure else "[insecure]"
            security_style = "#79d18a" if self._target_secure else "#f0b54d"
            summary = ""
            summary_visible = False
            target_render = Text.assemble(
                (self._target_host, "#9aa7b4"),
                (f"  {security_tag}", security_style),
            )

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
    def save_credentials(self) -> bool:
        return bool(self._save_credentials.value)


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
        self._button = Button("Try again", id="splash-status-retry", variant="primary")
        self._spinner.display = True
        self._button.display = False

    def compose(self) -> ComposeResult:
        yield self._spinner
        yield self._title
        yield self._detail
        yield self._button

    def set_connecting(self, message: str, detail: str = "") -> None:
        self._spinner.display = True
        self._button.display = False
        self._title.display = True
        self._detail.display = True
        self._title.update(Text(message or "Connecting to the connector...", style="bold"))
        self._detail.update(detail)

    def set_error(self, message: str, detail: str = "") -> None:
        self._spinner.display = False
        self._button.display = True
        self._title.display = False
        self._detail.display = False
        self._title.update("")
        self._detail.update("")


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
        def __init__(self, *, stage: SplashStage, host: str, username: str, password: str, save_credentials: bool) -> None:
            super().__init__()
            self.stage = stage
            self.host = host
            self.username = username
            self.password = password
            self.save_credentials = save_credentials

    class ActionRequested(Message):
        def __init__(self, action: str) -> None:
            super().__init__()
            self.action = action

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

    def compose(self) -> ComposeResult:
        yield self._hero
        yield self._stage_label
        yield self._message
        yield self._detail
        yield self._host_panel
        yield self._login_panel
        yield self._status_panel
        yield self._actions

    def on_mount(self) -> None:
        self._sync_state()
        self.focus_primary()

    def _apply_stage(self, stage: SplashStage) -> None:
        for value in _STAGE_ORDER:
            self.remove_class(f"stage-{value}")
        self.add_class(f"stage-{stage}")

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
        self._host_panel.set_host(self._state.host)
        self._login_panel.set_credentials(
            self._state.username,
            self._state.password,
            save=self._state.save_credentials,
        )
        self._login_panel.set_target(self._state.host)
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
        self._state = state
        if self.is_mounted:
            self._sync_state()
            if state.stage in {"host", "login"}:
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
        save_credentials: bool = False,
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
                save_credentials=save_credentials,
                login_error=login_error,
                actions=actions or (self._default_actions() if stage == "ready" else ()),
            )
        )

    def set_actions(self, actions: Sequence[SplashAction]) -> None:
        self._state = SplashState(
            stage=self._state.stage,
            message=self._state.message,
            detail=self._state.detail,
            host=self._state.host,
            username=self._state.username,
            password=self._state.password,
            save_credentials=self._state.save_credentials,
            login_error=self._state.login_error,
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

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""

        if button_id == "splash-host-submit":
            self._submit_host()
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
            return

        if event.input.id in {"splash-login-username", "splash-login-password"}:
            if self._is_login_state_sync_event(event):
                return
            self._login_panel.clear_error()

    def on_key(self, event: events.Key) -> None:
        if event.key == "escape" and self._state.stage == "login":
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
                host=self._host_panel.host,
                username=username,
                password=password,
                save_credentials=self._login_panel.save_credentials,
            )
        )

    def _submit_host(self) -> None:
        self._host_panel.refresh_validation()
        if not self._host_panel.is_valid:
            self._host_panel.focus_input()
            return
        self.post_message(
            self.SubmitRequested(
                stage="host",
                host=self._host_panel.host,
                username="",
                password="",
                save_credentials=False,
            )
        )

    def _request_back_to_host(self) -> None:
        self.post_message(self.ActionRequested("back"))
