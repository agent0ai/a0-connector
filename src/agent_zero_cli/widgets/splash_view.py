from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Sequence, TypeAlias

from rich.console import Group
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Grid, Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import Button, Checkbox, Input, LoadingIndicator, Static

from agent_zero_cli.widgets.chat_log import build_agent_zero_banner_widget


SplashStage: TypeAlias = Literal["host", "login", "connecting", "ready", "error"]

_STAGE_ORDER: tuple[SplashStage, ...] = ("host", "login", "connecting", "ready", "error")
_STAGE_LABELS: dict[SplashStage, str] = {
    "host": "Connect",
    "login": "Sign in",
    "connecting": "Connecting",
    "ready": "Ready",
    "error": "Connection issue",
}
_DEFAULT_HOST = "http://127.0.0.1:5080"
_DEFAULT_ACTION_KEYS: tuple[str, ...] = ("chats", "compact", "pause", "nudge")


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
        self._title = Static("Agent Zero URL", classes="splash-panel-title")
        self._copy = Static(
            "Local or remote endpoint.",
            classes="splash-panel-copy",
        )
        self._host = Input(placeholder=_DEFAULT_HOST, id="splash-host-input")
        self._button = Button("Connect", id="splash-host-submit", variant="primary")
        self._hint = Static(
            "Press Enter to use the default or connect.",
            classes="splash-panel-hint",
        )

    def compose(self) -> ComposeResult:
        yield self._title
        yield self._copy
        yield self._host
        yield self._button
        yield self._hint

    def set_host(self, host: str) -> None:
        host_text = host.strip()
        if self._host.has_focus and self._host.value.strip():
            self._host.placeholder = _DEFAULT_HOST
            return
        self._host.value = "" if host_text == _DEFAULT_HOST else host_text
        self._host.placeholder = _DEFAULT_HOST

    def focus_input(self) -> None:
        self._host.focus()

    @property
    def host(self) -> str:
        return self._host.value.strip() or _DEFAULT_HOST


class SplashLoginPanel(Vertical):
    """Username/password panel shown when the connector advertises login auth."""

    DEFAULT_CSS = """
    SplashLoginPanel {
        layout: vertical;
    }
    """

    def __init__(self) -> None:
        super().__init__(id="splash-login-panel")
        self._title = Static("Sign in", classes="splash-panel-title")
        self._copy = Static(
            "Connector login.",
            classes="splash-panel-copy",
        )
        self._username = Input(placeholder="Username", id="splash-login-username")
        self._password = Input(placeholder="Password", password=True, id="splash-login-password")
        self._save_credentials = Checkbox("Save credentials", id="splash-save-credentials")
        self._button = Button("Log in", id="splash-login-submit", variant="primary")
        self._hint = Static(
            "Save writes the token to the local `.env` file.",
            classes="splash-panel-hint",
        )

    def compose(self) -> ComposeResult:
        yield self._title
        yield self._copy
        yield self._username
        yield self._password
        yield self._save_credentials
        yield self._button
        yield self._hint

    def set_credentials(self, username: str = "", password: str = "", *, save: bool = False) -> None:
        self._username.value = username
        self._password.value = password
        self._save_credentials.value = save

    def focus_input(self) -> None:
        if self._username.value:
            self._password.focus()
        else:
            self._username.focus()

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

    def __init__(self, key: str) -> None:
        super().__init__(id=f"splash-action-{key}", classes="splash-action-card")
        self.key = key
        self._button = Button("", id=f"splash-action-{key}-button")
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
        self._cards = {key: SplashActionCard(key) for key in _DEFAULT_ACTION_KEYS}

    def compose(self) -> ComposeResult:
        for key in _DEFAULT_ACTION_KEYS:
            yield self._cards[key]

    def set_actions(self, actions: Sequence[SplashAction]) -> None:
        by_key = {action.key: action for action in actions}
        for key, card in self._cards.items():
            card.set_action(by_key.get(key))

    def action_for_button_id(self, button_id: str) -> str | None:
        prefix = "splash-action-"
        suffix = "-button"
        if not button_id.startswith(prefix) or not button_id.endswith(suffix):
            return None
        key = button_id[len(prefix) : -len(suffix)]
        if key in self._cards and self._cards[key].enabled:
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

    def _apply_action_defaults(self) -> None:
        self._actions.set_actions(self._default_actions())

    def _default_actions(self) -> Sequence[SplashAction]:
        return (
            SplashAction(
                key="chats",
                title="Chats",
                description="Open chat history.",
            ),
            SplashAction(
                key="compact",
                title="Compact",
                description="Compact this chat.",
                enabled=False,
                disabled_reason="Available when compaction stats are ready.",
            ),
            SplashAction(
                key="pause",
                title="Pause",
                description="Pause the active run.",
                enabled=False,
                disabled_reason="Available while a run is active.",
            ),
            SplashAction(
                key="nudge",
                title="Nudge",
                description="Continue the current run.",
                enabled=False,
                disabled_reason="Available while the agent is active.",
            ),
        )

    def _sync_state(self) -> None:
        self._apply_stage(self._state.stage)
        self._stage_label.update(Text(_STAGE_LABELS.get(self._state.stage, self._state.stage.title()), style="bold"))
        self._message.update(self._state.message)
        self._detail.update(self._state.detail)
        self._host_panel.set_host(self._state.host)
        self._login_panel.set_credentials(
            self._state.username,
            self._state.password,
            save=self._state.save_credentials,
        )

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
            self.post_message(
                self.SubmitRequested(
                    stage="host",
                    host=self._host_panel.host,
                    username="",
                    password="",
                    save_credentials=False,
                )
            )
            return

        if button_id == "splash-login-submit":
            self.post_message(
                self.SubmitRequested(
                    stage="login",
                    host=self._host_panel.host,
                    username=self._login_panel.username,
                    password=self._login_panel.password,
                    save_credentials=self._login_panel.save_credentials,
                )
            )
            return

        if button_id == "splash-status-retry":
            self.post_message(self.ActionRequested("retry"))
            return

        action_key = self._actions.action_for_button_id(button_id)
        if action_key is not None:
            self.post_message(self.ActionRequested(action_key))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "splash-host-input":
            self.post_message(
                self.SubmitRequested(
                    stage="host",
                    host=self._host_panel.host,
                    username="",
                    password="",
                    save_credentials=False,
                )
            )
            return

        if event.input.id == "splash-login-password":
            self.post_message(
                self.SubmitRequested(
                    stage="login",
                    host=self._host_panel.host,
                    username=self._login_panel.username,
                    password=self._login_panel.password,
                    save_credentials=self._login_panel.save_credentials,
                )
            )
