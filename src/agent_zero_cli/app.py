from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from typing import Any, Iterable

from textual.app import App, ComposeResult, SystemCommand
from textual.binding import Binding
from textual.reactive import reactive
from textual.theme import Theme
from textual.widgets import ContentSwitcher, Footer

from agent_zero_cli.client import A0Client, A0ConnectorPluginMissingError
from agent_zero_cli.commands import CommandAvailability, CommandSpec
from agent_zero_cli.config import CLIConfig, load_config, save_env
from agent_zero_cli.rendering import (
    _EVENT_CATEGORY,
    _STATUS_LABEL,
    extract_detail,
    render_connector_event,
)
from agent_zero_cli.screens.chat_list import ChatListScreen
from agent_zero_cli.screens.compact_modal import CompactResult, CompactScreen
from agent_zero_cli.screens.settings_modal import SettingsResult, SettingsScreen
from agent_zero_cli.screens.skills_modal import (
    SkillsDeleteRequested,
    SkillsFilters,
    SkillsFiltersChanged,
    SkillsRefreshRequested,
    SkillsScreen,
)
from agent_zero_cli.widgets import (
    ChatInput,
    ConnectionStatus,
    ModelSwitcherBar,
    SlashCommand,
    SlashCommandMenu,
    SplashAction,
    SplashState,
    SplashView,
)
from agent_zero_cli.widgets.chat_log import ChatLog


_DEFAULT_HOST = "http://127.0.0.1:5080"
_PROTOCOL_VERSION = "a0-connector.v1"
_WS_NAMESPACE = "/ws"
_WS_HANDLER = "plugins/a0_connector/ws_connector"


class AgentZeroCLI(App):
    """Agent Zero CLI - terminal-native connector shell."""

    CSS_PATH = "styles/app.tcss"
    TITLE = "Agent Zero CLI"
    BINDINGS = [
        Binding("Ctrl+C", "Quit", "Exit", show=True),
        Binding("F5", "clear_chat", "Clear", show=True, priority=True),
        Binding("F6", "list_chats", "Chats", show=True, priority=True),
        Binding("F7", "nudge_agent", "Nudge", show=True, priority=True),
        Binding("F8", "pause_agent", "Pause", show=True, priority=True),
        Binding(
            "ctrl+p",
            "command_palette",
            "Commands",
            show=False,
            priority=True,
            key_display="^P",
            tooltip="Open the command palette",
        ),
    ]

    connected = reactive(False)
    agent_active = reactive(False)

    def __init__(self, config: CLIConfig | None = None) -> None:
        super().__init__()
        self.register_theme(
            Theme(
                name="a0-dark",
                primary="#0178D4",
                secondary="#004578",
                accent="#00b4ff",
                foreground="#e0e0e0",
                dark=True,
            )
        )
        self.theme = "a0-dark"
        self.config = config or load_config()
        base_url = self.config.instance_url or _DEFAULT_HOST
        self.client = A0Client(base_url, api_key=self.config.api_key)
        self.capabilities: dict[str, Any] = {}
        self.connector_features: set[str] = set()
        self.current_context: str | None = None
        self.current_context_has_messages = False
        self._response_delivered = False
        self._chat_intro_pending = True
        self._splash_state = SplashState(stage="host", host=self.config.instance_url or _DEFAULT_HOST)
        self._command_registry = self._build_command_registry()
        self._command_lookup = {
            name: spec
            for spec in self._command_registry
            for name in spec.names()
        }

    def compose(self) -> ComposeResult:
        yield ConnectionStatus(id="connection-status")
        with ContentSwitcher(initial="splash-view", id="body-switcher"):
            yield SplashView()
            yield ChatLog(id="chat-log")
        yield SlashCommandMenu()
        yield ModelSwitcherBar(id="model-switcher-bar")
        yield ChatInput(id="message-input")
        yield Footer()

    async def on_mount(self) -> None:
        input_widget = self.query_one("#message-input", ChatInput)
        input_widget.disabled = True
        slash_menu = self.query_one("#slash-menu", SlashCommandMenu)
        slash_menu.display = False
        self.query_one("#model-switcher-bar", ModelSwitcherBar).clear()
        self.query_one("#splash-view", SplashView).set_state(self._splash_state)

        log = self.query_one("#chat-log", ChatLog)
        self.set_interval(0.1, log.advance_shimmer)
        self._sync_connection_status("disconnected", self.config.instance_url or "")
        self._sync_body_mode()
        self._focus_splash_primary()
        self.run_worker(self._startup(), exclusive=True, name="startup")

    def get_system_commands(self, screen) -> Iterable[SystemCommand]:
        yield from super().get_system_commands(screen)
        for spec in self._command_registry:
            availability = spec.availability(self)
            if not availability.available:
                continue
            yield SystemCommand(
                spec.canonical_name,
                spec.description,
                lambda name=spec.canonical_name: self.run_worker(
                    self._dispatch_command(name),
                    exclusive=True,
                    name=f"palette-{name[1:]}",
                ),
            )

    def _build_command_registry(self) -> tuple[CommandSpec, ...]:
        return (
            CommandSpec(
                "/help",
                (),
                "Show the commands available in this shell.",
                lambda app: CommandAvailability(True),
                lambda app: app._cmd_help(),
            ),
            CommandSpec(
                "/clear",
                (),
                "Clear the visible chat log without resetting the current context.",
                lambda app: CommandAvailability(True),
                lambda app: app._cmd_clear(),
            ),
            CommandSpec(
                "/new",
                (),
                "Create a brand-new empty chat context.",
                lambda app: app._require_features("chat_create"),
                lambda app: app._cmd_new(),
            ),
            CommandSpec(
                "/chats",
                (),
                "List previous chats and switch contexts.",
                lambda app: app._require_features("chats_list"),
                lambda app: app._cmd_chats(),
            ),
            CommandSpec(
                "/settings",
                (),
                "Open the curated connector-backed settings editor.",
                lambda app: app._require_features("settings_get", "settings_set"),
                lambda app: app._cmd_settings(),
            ),
            CommandSpec(
                "/skills",
                (),
                "Browse connector-backed skills with filters and delete support.",
                lambda app: app._require_features("skills_list", "agents_list"),
                lambda app: app._cmd_skills(),
            ),
            CommandSpec(
                "/compact",
                (),
                "Open the connector-backed compaction confirmation flow.",
                lambda app: app._compact_availability(),
                lambda app: app._cmd_compact(),
            ),
            CommandSpec(
                "/pause",
                (),
                "Pause the current run when pause support is available.",
                lambda app: app._pause_availability(),
                lambda app: app._cmd_pause(),
            ),
            CommandSpec(
                "/nudge",
                (),
                "Send a continuation nudge to the current context.",
                lambda app: app._nudge_availability(),
                lambda app: app._cmd_nudge(),
            ),
            CommandSpec(
                "/exit",
                ("/q",),
                "Disconnect and exit the CLI.",
                lambda app: CommandAvailability(True),
                lambda app: app._cmd_exit(),
            ),
        )

    def _sync_connection_status(self, status: str, url: str | None = None) -> None:
        widget = self.query_one("#connection-status", ConnectionStatus)
        widget.status = status
        if url is not None:
            widget.url = url

    def _splash_host(self) -> str:
        return self._splash_state.host or self.config.instance_url or _DEFAULT_HOST

    def _normalize_host(self, host: str) -> str:
        return host.strip() or _DEFAULT_HOST

    def _set_splash_state(self, **changes: Any) -> None:
        self._splash_state = replace(self._splash_state, **changes)
        try:
            self.query_one("#splash-view", SplashView).set_state(self._splash_state)
        except Exception:
            pass

    def _set_splash_stage(
        self,
        stage: str,
        *,
        message: str = "",
        detail: str = "",
        host: str | None = None,
        username: str | None = None,
        password: str | None = None,
        save_credentials: bool | None = None,
        actions: tuple[SplashAction, ...] | None = None,
    ) -> None:
        updates: dict[str, Any] = {
            "stage": stage,
            "message": message,
            "detail": detail,
        }
        if host is not None:
            updates["host"] = host
        if username is not None:
            updates["username"] = username
        if password is not None:
            updates["password"] = password
        if save_credentials is not None:
            updates["save_credentials"] = save_credentials
        if actions is not None:
            updates["actions"] = actions
        self._set_splash_state(**updates)

    def _sync_ready_actions(self) -> None:
        if self._splash_state.stage != "ready":
            return
        self._set_splash_state(actions=self._welcome_actions())

    def _sync_body_mode(self) -> None:
        body = self.query_one("#body-switcher", ContentSwitcher)
        if self.connected and self.current_context_has_messages:
            body.current = "chat-log"
        else:
            body.current = "splash-view"
            self._sync_ready_actions()

    def _set_activity(self, label: str, detail: str = "") -> None:
        self.query_one("#message-input", ChatInput).set_activity(label, detail)

    def _set_idle(self) -> None:
        self.query_one("#message-input", ChatInput).set_idle()
        try:
            self.query_one("#chat-log", ChatLog).dim_active_status()
        except Exception:
            pass

    def _focus_splash_primary(self) -> None:
        callback = lambda: self.query_one("#splash-view", SplashView).focus_primary()
        if self.is_running:
            self.call_after_refresh(callback)
        else:
            callback()

    def _focus_message_input(self) -> None:
        callback = lambda: self.query_one("#message-input", ChatInput).focus()
        if self.is_running:
            self.call_after_refresh(callback)
        else:
            callback()

    def _show_notice(self, message: str, *, error: bool = False) -> None:
        if self.connected and not self.current_context_has_messages:
            splash_message = self._splash_state.message
            if self._splash_state.stage == "ready":
                splash_message = message if error else "Ready when you are."
            self._set_splash_state(
                message=splash_message,
                detail=message,
                actions=self._welcome_actions() if self._splash_state.stage == "ready" else self._splash_state.actions,
            )
            return

        log = self.query_one("#chat-log", ChatLog)
        log.write(f"[red]{message}[/red]" if error else message)

    def _message_flag_for_event(self, event_type: str) -> bool:
        return event_type in {"user_message", "assistant_message", "assistant_delta"}

    def _mark_context_has_messages(self) -> None:
        if self.current_context_has_messages:
            return
        self.current_context_has_messages = True
        self._sync_body_mode()

    def _show_chat_intro(self, log: ChatLog, category: str) -> None:
        if not self._chat_intro_pending or category not in {"user", "response"}:
            return
        log.ensure_intro_banner()
        self._chat_intro_pending = False

    def _clear_model_switcher(self) -> None:
        try:
            self.query_one("#model-switcher-bar", ModelSwitcherBar).clear()
        except Exception:
            pass

    def _apply_model_switcher_state(self, payload: dict[str, Any]) -> None:
        presets = payload.get("presets") if isinstance(payload.get("presets"), list) else []
        override = payload.get("override") if isinstance(payload.get("override"), dict) else {}
        selected_preset = str(override.get("preset_name") or "").strip()
        override_label = ""

        if override and not selected_preset:
            override_label = str(override.get("name") or override.get("provider") or "Custom override").strip()
        elif selected_preset:
            preset_names = {
                str(item.get("name") or item.get("value") or "").strip()
                for item in presets
                if isinstance(item, dict)
            }
            if selected_preset not in preset_names:
                override_label = f"Preset: {selected_preset}"

        widget = self.query_one("#model-switcher-bar", ModelSwitcherBar)
        widget.set_state(
            main_model=payload.get("main_model"),
            utility_model=payload.get("utility_model"),
            presets=presets,
            allowed=bool(payload.get("allowed")),
            selected_preset=selected_preset,
            override_label=override_label,
        )

    async def _refresh_model_switcher(self, *, silent: bool = True) -> None:
        if "model_switcher" not in self.connector_features or not self.current_context:
            self._clear_model_switcher()
            return

        widget = self.query_one("#model-switcher-bar", ModelSwitcherBar)
        widget.set_busy(True)
        try:
            payload = await self.client.get_model_switcher(self.current_context)
        except Exception as exc:
            self._clear_model_switcher()
            if not silent:
                self._show_notice(f"Failed to load model switcher: {exc}", error=True)
            return

        self._apply_model_switcher_state(payload)
        widget.set_busy(False)

    def _command_display(self, spec: CommandSpec) -> str:
        if not spec.aliases:
            return spec.canonical_name
        aliases = ", ".join(spec.aliases)
        return f"{spec.canonical_name} ({aliases})"

    def _available_help_lines(self) -> tuple[list[str], list[str]]:
        available: list[str] = []
        unavailable: list[str] = []
        for spec in self._command_registry:
            availability = spec.availability(self)
            line = f"{self._command_display(spec)} - {spec.description}"
            if availability.available:
                available.append(line)
            else:
                reason = availability.reason or "Unavailable right now."
                unavailable.append(f"{line} [{reason}]")
        return available, unavailable

    def _surface_help(self) -> None:
        available, unavailable = self._available_help_lines()
        if self.connected and not self.current_context_has_messages:
            lines = ["Available commands:"]
            lines.extend(f"- {line}" for line in available)
            if unavailable:
                lines.append("")
                lines.append("Unavailable right now:")
                lines.extend(f"- {line}" for line in unavailable)
            self._set_splash_state(
                message="Available commands",
                detail="\n".join(lines),
                actions=self._welcome_actions(),
            )
            return

        log = self.query_one("#chat-log", ChatLog)
        log.write("[bold]Available commands:[/bold]")
        for line in available:
            log.write(line)
        if unavailable:
            log.write("[dim]Unavailable right now:[/dim]")
            for line in unavailable:
                log.write(line)

    def _run_on_ui(self, func: Any, *args: Any) -> None:
        app_loop = getattr(self, "loop", None)
        if app_loop is None:
            func(*args)
        else:
            app_loop.call_soon_threadsafe(func, *args)

    def _set_connected(self, value: bool) -> None:
        self.connected = value
        self._sync_connection_status("connected" if value else "disconnected")
        input_widget = self.query_one("#message-input", ChatInput)
        input_widget.disabled = not value
        if not value:
            self._clear_model_switcher()
            self._set_splash_stage(
                "error",
                message="Connection lost",
                detail=self.config.instance_url or self._splash_host(),
                host=self._splash_host(),
            )
            self._sync_body_mode()

    def _require_connection(self) -> CommandAvailability:
        if not self.connected:
            return CommandAvailability(False, "Connect to an Agent Zero instance first.")
        return CommandAvailability(True)

    def _require_context(self) -> CommandAvailability:
        base = self._require_connection()
        if not base.available:
            return base
        if not self.current_context:
            return CommandAvailability(False, "Create or open a chat context first.")
        return CommandAvailability(True)

    def _require_features(self, *features: str) -> CommandAvailability:
        base = self._require_connection()
        if not base.available:
            return base
        missing = [feature for feature in features if feature not in self.connector_features]
        if missing:
            joined = ", ".join(missing)
            return CommandAvailability(False, f"This connector build does not advertise: {joined}.")
        return CommandAvailability(True)

    def _compact_availability(self) -> CommandAvailability:
        base = self._require_features("compact_chat", "model_presets")
        if not base.available:
            return base
        if not self.current_context:
            return CommandAvailability(False, "Open or create a chat before compacting it.")
        if not self.current_context_has_messages:
            return CommandAvailability(False, "Start a conversation before compacting it.")
        if self.agent_active:
            return CommandAvailability(False, "Wait for the current run to finish before compacting.")
        return CommandAvailability(True)

    def _pause_availability(self) -> CommandAvailability:
        base = self._require_connection()
        if not base.available:
            return base
        if not hasattr(self.client, "pause_agent"):
            return CommandAvailability(False, "Pause is not exposed by this connector build.")
        if not self.current_context:
            return CommandAvailability(False, "Open or create a chat context first.")
        if not self.agent_active:
            return CommandAvailability(False, "Pause becomes available while the agent is running.")
        return CommandAvailability(True)

    def _nudge_availability(self) -> CommandAvailability:
        base = self._require_context()
        if not base.available:
            return base
        if not self.current_context_has_messages:
            return CommandAvailability(False, "Start a conversation before nudging it forward.")
        if self.agent_active:
            return CommandAvailability(False, "Wait for the current run to finish before nudging.")
        return CommandAvailability(True)

    def _welcome_actions(self) -> tuple[SplashAction, ...]:
        def action(key: str, title: str, description: str, availability: CommandAvailability) -> SplashAction:
            return SplashAction(
                key=key,
                title=title,
                description=description,
                enabled=availability.available,
                disabled_reason="" if availability.available else (availability.reason or ""),
            )

        return (
            action("chats", "Chats", "Open chat history.", self._require_features("chats_list")),
            action("settings", "Settings", "Edit connector settings.", self._require_features("settings_get", "settings_set")),
            action("skills", "Skills", "Browse loaded skills.", self._require_features("skills_list", "agents_list")),
            action("compact", "Compact", "Compact this chat.", self._compact_availability()),
            action("pause", "Pause", "Pause the active run.", self._pause_availability()),
            action("nudge", "Nudge", "Continue the current run.", self._nudge_availability()),
        )

    async def _startup(self) -> None:
        host = self.config.instance_url.strip()
        if not host:
            self._set_splash_stage(
                "host",
                message="Enter an Agent Zero URL.",
                detail="",
                host=_DEFAULT_HOST,
            )
            self._sync_connection_status("disconnected", "")
            self._focus_splash_primary()
            return

        await self._begin_connection(host)

    async def _fetch_capabilities(self) -> tuple[dict[str, Any] | None, bool, str]:
        try:
            return await self.client.fetch_capabilities(), False, ""
        except A0ConnectorPluginMissingError as exc:
            return None, True, str(exc)
        except Exception as exc:
            return None, False, str(exc)

    def _validate_capabilities(self, capabilities: dict[str, Any]) -> None:
        protocol = capabilities.get("protocol")
        namespace = capabilities.get("websocket_namespace")
        handlers = capabilities.get("websocket_handlers") or []
        auth_modes = capabilities.get("auth") or []

        if protocol != _PROTOCOL_VERSION:
            raise ValueError(f"Unsupported connector protocol: expected {_PROTOCOL_VERSION}, got {protocol!r}")
        if namespace != _WS_NAMESPACE:
            raise ValueError(f"Unsupported WebSocket namespace: expected {_WS_NAMESPACE}, got {namespace!r}")
        if not isinstance(handlers, list) or _WS_HANDLER not in handlers:
            raise ValueError(f"Connector handler activation is missing {_WS_HANDLER!r} in capabilities")
        if "api_key" not in auth_modes:
            raise ValueError("Connector capabilities do not advertise API-key auth")

    async def _begin_connection(
        self,
        host: str,
        *,
        username: str = "",
        password: str = "",
        save_credentials_flag: bool = False,
    ) -> None:
        normalized_host = self._normalize_host(host)
        self.config.instance_url = normalized_host
        self.client.base_url = normalized_host.rstrip("/")
        self.client.api_key = self.config.api_key
        self._sync_connection_status("connecting", normalized_host)
        self.query_one("#message-input", ChatInput).disabled = True
        self._close_slash_menu()
        self._set_splash_stage(
            "connecting",
            message="Probing connector capabilities...",
            detail=normalized_host,
            host=normalized_host,
            username=username,
            password=password,
            save_credentials=save_credentials_flag,
        )

        capabilities, plugin_missing, capability_error = await self._fetch_capabilities()
        if capabilities is None:
            message = "Connector unavailable" if not plugin_missing else "Connector plugin missing"
            self._sync_connection_status("disconnected", normalized_host)
            self._set_splash_stage(
                "error",
                message=message,
                detail=capability_error or normalized_host,
                host=normalized_host,
                username=username,
                password="",
                save_credentials=save_credentials_flag,
            )
            self._focus_splash_primary()
            return

        try:
            self._validate_capabilities(capabilities)
        except ValueError as exc:
            self._sync_connection_status("disconnected", normalized_host)
            self._set_splash_stage(
                "error",
                message="Connector contract mismatch",
                detail=str(exc),
                host=normalized_host,
                username=username,
                password="",
                save_credentials=save_credentials_flag,
            )
            return

        self.capabilities = capabilities
        self.connector_features = set(capabilities.get("features") or [])
        auth_modes = capabilities.get("auth") or []

        if not self.config.api_key and "login" in auth_modes and username and password:
            self._set_splash_stage(
                "connecting",
                message="Signing in...",
                detail=normalized_host,
                host=normalized_host,
                username=username,
                password=password,
                save_credentials=save_credentials_flag,
            )
            api_key = await self.client.login(username, password)
            if not api_key:
                self._sync_connection_status("disconnected", normalized_host)
                self._set_splash_stage(
                    "login",
                    message="Sign in to continue",
                    detail="Invalid credentials. Try again.",
                    host=normalized_host,
                    username=username,
                    password="",
                    save_credentials=save_credentials_flag,
                )
                self._focus_splash_primary()
                return

            self.config.api_key = api_key
            self.client.api_key = api_key
            if save_credentials_flag:
                save_env("AGENT_ZERO_HOST", normalized_host)
                save_env("AGENT_ZERO_API_KEY", api_key)

        elif not self.config.api_key and "login" in auth_modes:
            self._sync_connection_status("disconnected", normalized_host)
            self._set_splash_stage(
                "login",
                message="Sign in to continue",
                detail=normalized_host,
                host=normalized_host,
                username=username,
                password="",
                save_credentials=save_credentials_flag,
            )
            self._focus_splash_primary()
            return

        elif not self.config.api_key and "api_key" in auth_modes:
            self._sync_connection_status("disconnected", normalized_host)
            self._set_splash_stage(
                "error",
                message="No API key available",
                detail="Set AGENT_ZERO_API_KEY or connect to a server that supports login auth.",
                host=normalized_host,
            )
            return

        if self.config.api_key:
            try:
                api_key_ok = await self.client.verify_api_key()
            except Exception as exc:
                self._sync_connection_status("disconnected", normalized_host)
                self._set_splash_stage(
                    "error",
                    message="API-key verification failed",
                    detail=str(exc),
                    host=normalized_host,
                )
                return

            if not api_key_ok:
                self.config.api_key = ""
                self.client.api_key = ""
                self._sync_connection_status("disconnected", normalized_host)
                if "login" in auth_modes:
                    self._set_splash_stage(
                        "login",
                        message="Saved API key was rejected",
                        detail="Sign in again to refresh the connector token.",
                        host=normalized_host,
                        username=username,
                        password="",
                        save_credentials=save_credentials_flag,
                    )
                    self._focus_splash_primary()
                else:
                    self._set_splash_stage(
                        "error",
                        message="API key rejected",
                        detail="The connector rejected the configured API key.",
                        host=normalized_host,
                    )
                return

        self.client.on_connect = lambda: self._run_on_ui(self._set_connected, True)
        self.client.on_disconnect = lambda: self._run_on_ui(self._set_connected, False)
        self.client.on_context_snapshot = lambda data: self._run_on_ui(self._handle_context_snapshot, data)
        self.client.on_context_event = lambda data: self._run_on_ui(self._handle_context_event, data)
        self.client.on_context_complete = lambda data: self._run_on_ui(self._handle_context_complete, data)
        self.client.on_error = lambda data: self._run_on_ui(self._handle_connector_error, data)
        self.client.on_file_op = self._handle_file_op

        try:
            await self.client.connect_websocket()
            await self.client.send_hello()
        except Exception as exc:
            self._sync_connection_status("disconnected", normalized_host)
            self._set_splash_stage(
                "error",
                message="WebSocket connection failed",
                detail=str(exc),
                host=normalized_host,
            )
            return

        try:
            context_id = await self.client.create_chat()
        except Exception as exc:
            self._sync_connection_status("disconnected", normalized_host)
            self._set_splash_stage(
                "error",
                message="Failed to create the initial chat",
                detail=str(exc),
                host=normalized_host,
            )
            return

        self.current_context = context_id
        self.current_context_has_messages = False
        self._response_delivered = False
        self._chat_intro_pending = True
        self.query_one("#chat-log", ChatLog).clear()
        self._set_idle()

        try:
            await self.client.subscribe_context(context_id)
        except Exception as exc:
            self._sync_connection_status("disconnected", normalized_host)
            self._set_splash_stage(
                "error",
                message="Failed to subscribe to the initial chat",
                detail=str(exc),
                host=normalized_host,
            )
            return

        self.connected = True
        self._sync_connection_status("connected", normalized_host)
        input_widget = self.query_one("#message-input", ChatInput)
        input_widget.disabled = False
        self._set_splash_stage(
            "ready",
            message="Ready when you are.",
            detail=normalized_host,
            host=normalized_host,
            actions=self._welcome_actions(),
        )
        await self._refresh_model_switcher()
        self._sync_body_mode()
        self._focus_message_input()

    def _handle_context_snapshot(self, data: dict[str, Any]) -> None:
        context_id = data.get("context_id", "")
        if context_id != self.current_context:
            return

        log = self.query_one("#chat-log", ChatLog)
        events = data.get("events", [])

        for event in events:
            event_type = event.get("event", "")
            category = _EVENT_CATEGORY.get(event_type, "info")

            if self._message_flag_for_event(event_type):
                self._mark_context_has_messages()

            if category in ("user", "response", "warning", "error", "code"):
                self._show_chat_intro(log, category)
                render_connector_event(log, event)
            else:
                label = _STATUS_LABEL.get(event_type)
                if label:
                    event_data = event.get("data", {})
                    detail = extract_detail(event_type, event_data)
                    seq = event.get("sequence", -1)
                    log.append_or_update(seq, f"[dim]{label}{f' [{detail}]' if detail else ''}[/dim]")

        self._sync_body_mode()

    def _handle_context_event(self, data: dict[str, Any]) -> None:
        context_id = data.get("context_id", "")
        if context_id != self.current_context:
            return

        event_type = data.get("event", "")
        if self._message_flag_for_event(event_type):
            self._mark_context_has_messages()

        category = _EVENT_CATEGORY.get(event_type, "info")
        log = self.query_one("#chat-log", ChatLog)

        if self._response_delivered and category != "response":
            if category in ("error", "warning"):
                render_connector_event(log, data)
            return

        self.agent_active = True
        self._sync_ready_actions()
        input_widget = self.query_one("#message-input", ChatInput)
        input_widget.disabled = True

        if category == "response":
            self._response_delivered = True
            input_widget.disabled = False
            self._focus_message_input()
            self._set_idle()
            self._show_chat_intro(log, category)
            render_connector_event(log, data)
            return

        label = _STATUS_LABEL.get(event_type)
        if label:
            event_data = data.get("data", {})
            detail = extract_detail(event_type, event_data)
            self._set_activity(label, detail)
            log.set_active_status(data.get("sequence", -1), label, detail)

        if category in ("warning", "error", "user", "code"):
            self._show_chat_intro(log, category)
            if render_connector_event(log, data):
                if log._active_seq == data.get("sequence"):
                    log.stop_active_status()

    def _handle_context_complete(self, data: dict[str, Any]) -> None:
        context_id = data.get("context_id", "")
        if context_id != self.current_context:
            return

        self.agent_active = False
        self._sync_ready_actions()
        input_widget = self.query_one("#message-input", ChatInput)
        input_widget.disabled = False
        self._focus_message_input()
        self._set_idle()

    def _handle_connector_error(self, data: dict[str, Any]) -> None:
        code = data.get("code", "ERROR")
        message = data.get("message", "Unknown error")
        self._show_notice(f"{code}: {message}", error=True)

    def _handle_file_op(self, data: dict[str, Any]) -> dict[str, Any]:
        op_id = data.get("op_id", "")
        op = data.get("op", "")
        path = data.get("path", "")

        try:
            if op == "read":
                return self._file_op_read(op_id, path, data)
            if op == "write":
                return self._file_op_write(op_id, path, data)
            if op == "patch":
                return self._file_op_patch(op_id, path, data)
            return {"op_id": op_id, "ok": False, "error": f"Unknown op: {op}"}
        except Exception as exc:
            return {"op_id": op_id, "ok": False, "error": str(exc)}

    def _file_op_read(self, op_id: str, path: str, data: dict[str, Any]) -> dict[str, Any]:
        line_from = data.get("line_from")
        line_to = data.get("line_to")

        if not os.path.isfile(path):
            return {"op_id": op_id, "ok": False, "error": f"File not found: {path}"}

        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()

        total = len(lines)
        start = (line_from - 1) if line_from and line_from > 0 else 0
        end = line_to if line_to and line_to <= total else total
        selected = lines[start:end]

        content = ""
        for index, line in enumerate(selected, start=start + 1):
            content += f"{index:>4} | {line}"

        return {
            "op_id": op_id,
            "ok": True,
            "result": {
                "content": content,
                "total_lines": total,
                "line_from": start + 1,
                "line_to": end,
            },
        }

    def _file_op_write(self, op_id: str, path: str, data: dict[str, Any]) -> dict[str, Any]:
        content = data.get("content", "")
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)
        return {"op_id": op_id, "ok": True, "result": {"path": path}}

    def _file_op_patch(self, op_id: str, path: str, data: dict[str, Any]) -> dict[str, Any]:
        edits = data.get("edits", [])
        if not os.path.isfile(path):
            return {"op_id": op_id, "ok": False, "error": f"File not found: {path}"}

        with open(path, "r", encoding="utf-8") as handle:
            lines = handle.readlines()

        sorted_edits = sorted(edits, key=lambda item: item.get("from", 0), reverse=True)
        for edit in sorted_edits:
            fr = edit.get("from", 1)
            to = edit.get("to")
            content = edit.get("content")
            idx = fr - 1

            if to is None and content is not None:
                lines[idx:idx] = content.splitlines(True)
            elif content is None:
                to_idx = to if to else fr
                del lines[idx:to_idx]
            else:
                to_idx = to if to else fr
                lines[idx:to_idx] = content.splitlines(True)

        with open(path, "w", encoding="utf-8") as handle:
            handle.writelines(lines)

        return {"op_id": op_id, "ok": True, "result": {"path": path}}

    def _slash_query(self, text: str) -> str | None:
        if not text:
            return None
        token = text.split(maxsplit=1)[0]
        if not token.startswith("/"):
            return None
        if len(text) != len(token):
            return None
        return token.lower()

    def _slash_matches(self, query: str) -> list[SlashCommand]:
        matches: list[SlashCommand] = []
        for spec in self._command_registry:
            availability = spec.availability(self)
            if not availability.available:
                continue
            names = spec.names()
            if query != "/" and not any(name.startswith(query) for name in names):
                continue
            matches.append(
                SlashCommand(
                    canonical=spec.canonical_name,
                    aliases=spec.aliases,
                    description=spec.description,
                )
            )
        return matches

    def _close_slash_menu(self) -> None:
        menu = self.query_one("#slash-menu", SlashCommandMenu)
        menu.display = False
        self.query_one("#message-input", ChatInput).set_slash_menu_active(False)

    def _open_slash_menu(self, commands: list[SlashCommand]) -> None:
        menu = self.query_one("#slash-menu", SlashCommandMenu)
        menu.display = True
        menu.set_visible_commands(commands)
        self.query_one("#message-input", ChatInput).set_slash_menu_active(True)

    def _insert_slash_command(self, command: SlashCommand | None) -> None:
        if command is None:
            return
        input_widget = self.query_one("#message-input", ChatInput)
        input_widget.value = f"{command.canonical} "
        input_widget.focus()
        self._close_slash_menu()

    async def _dispatch_command(self, text: str) -> None:
        token = text.split()[0].lower()
        spec = self._command_lookup.get(token)
        if spec is None:
            self._show_notice(f"Unknown command: {token}. Type /help for available commands.", error=True)
            return

        availability = spec.availability(self)
        if not availability.available:
            self._show_notice(availability.reason or f"{token} is unavailable right now.", error=True)
            return

        await spec.handler(self)
        self._sync_ready_actions()

    async def on_chat_input_submitted(self, event: ChatInput.Submitted) -> None:
        text = event.value.strip()
        if not text:
            self._close_slash_menu()
            return

        if text.startswith("/"):
            await self._dispatch_command(text)
            return

        if not self.current_context:
            self._show_notice("No active chat context.", error=True)
            return

        self._mark_context_has_messages()
        self._response_delivered = False
        event.input.disabled = True
        self.agent_active = True
        self._sync_ready_actions()

        try:
            await self.client.send_message(text, self.current_context)
        except Exception as exc:
            self._show_notice(f"Error sending message: {exc}", error=True)
            event.input.disabled = False
            self.agent_active = False
            self._sync_ready_actions()

    def on_chat_input_value_changed(self, event: ChatInput.ValueChanged) -> None:
        query = self._slash_query(event.value)
        if query is None:
            self._close_slash_menu()
            return

        self._open_slash_menu(self._slash_matches(query))

    async def on_chat_input_slash_navigation(self, event: ChatInput.SlashNavigation) -> None:
        menu = self.query_one("#slash-menu", SlashCommandMenu)
        if not menu.display:
            return

        if event.key == "up":
            menu.action_cursor_up()
            return
        if event.key == "down":
            menu.action_cursor_down()
            return
        if event.key == "tab":
            self._insert_slash_command(menu.highlighted_command)
            return
        if event.key == "escape":
            self._close_slash_menu()
            return
        if event.key == "enter":
            command = menu.highlighted_command
            if command is None:
                return
            self._close_slash_menu()
            await self._dispatch_command(command.canonical)

    def on_slash_command_menu_command_selected(self, event: SlashCommandMenu.CommandSelected) -> None:
        self._insert_slash_command(event.command)

    async def on_model_switcher_bar_preset_changed(self, event: ModelSwitcherBar.PresetChanged) -> None:
        if "model_switcher" not in self.connector_features or not self.current_context:
            event.bar.set_busy(False)
            return

        event.bar.set_busy(True)
        try:
            payload = await self.client.set_model_preset(self.current_context, event.value or None)
        except Exception as exc:
            event.bar.set_busy(False)
            await self._refresh_model_switcher()
            self._show_notice(f"Failed to update model preset: {exc}", error=True)
            return

        self._apply_model_switcher_state(payload)
        event.bar.set_busy(False)

    def on_splash_view_submit_requested(self, event: SplashView.SubmitRequested) -> None:
        self.run_worker(
            self._begin_connection(
                event.host or self._splash_host(),
                username=event.username,
                password=event.password,
                save_credentials_flag=event.save_credentials,
            ),
            exclusive=True,
            name="splash-submit",
        )

    def on_splash_view_action_requested(self, event: SplashView.ActionRequested) -> None:
        if event.action == "retry":
            self.run_worker(
                self._begin_connection(
                    self._splash_host(),
                    username=self._splash_state.username,
                    password=self._splash_state.password,
                    save_credentials_flag=self._splash_state.save_credentials,
                ),
                exclusive=True,
                name="splash-retry",
            )
            return

        command = {
            "chats": "/chats",
            "settings": "/settings",
            "skills": "/skills",
            "compact": "/compact",
            "pause": "/pause",
            "nudge": "/nudge",
        }.get(event.action)
        if command:
            self.run_worker(self._dispatch_command(command), exclusive=True, name=f"splash-{event.action}")

    async def _cmd_help(self) -> None:
        self._surface_help()

    async def _cmd_clear(self) -> None:
        self.query_one("#chat-log", ChatLog).clear()
        self._set_idle()

    async def _switch_context(self, context_id: str, *, has_messages_hint: bool) -> None:
        if self.current_context:
            await self.client.unsubscribe_context(self.current_context)

        self.current_context = context_id
        self.current_context_has_messages = has_messages_hint
        self._response_delivered = False
        log = self.query_one("#chat-log", ChatLog)
        log.clear()
        self._set_idle()
        self._sync_body_mode()
        await self.client.subscribe_context(context_id, from_seq=0)
        await self._refresh_model_switcher()

    async def _cmd_chats(self) -> None:
        try:
            contexts = await self.client.list_chats()
        except Exception as exc:
            self._show_notice(f"Error listing chats: {exc}", error=True)
            return

        if not contexts:
            self._show_notice("No previous chats found.")
            return

        result = await self.push_screen_wait(ChatListScreen(contexts))
        if not result:
            return

        selected = next((context for context in contexts if str(context.get("id")) == result), {})
        has_messages_hint = bool(selected.get("last_message"))
        if not has_messages_hint and "chat_get" in self.connector_features:
            try:
                metadata = await self.client.get_chat(result)
            except Exception:
                metadata = {}
            has_messages_hint = bool(metadata.get("last_message") or metadata.get("log_entries"))

        await self._switch_context(result, has_messages_hint=has_messages_hint)

    async def _cmd_new(self) -> None:
        try:
            context_id = await self.client.create_chat()
        except Exception as exc:
            self._show_notice(f"Failed to create a new chat: {exc}", error=True)
            return

        await self._switch_context(context_id, has_messages_hint=False)
        self._set_splash_stage(
            "ready",
            message="Ready when you are.",
            detail=self.config.instance_url or self._splash_host(),
            host=self._splash_host(),
            actions=self._welcome_actions(),
        )
        self._focus_message_input()

    async def _cmd_settings(self) -> None:
        try:
            payload = await self.client.get_settings()
        except Exception as exc:
            self._show_notice(f"Failed to load settings: {exc}", error=True)
            return

        settings = payload.get("settings", payload)
        result = await self.push_screen_wait(SettingsScreen(settings))
        if result is None:
            return

        if not isinstance(result, SettingsResult):
            raise TypeError(f"Unexpected settings result: {result!r}")

        if not result.changed_keys:
            return

        try:
            await self.client.set_settings(result.settings)
        except Exception as exc:
            self._show_notice(f"Failed to save settings: {exc}", error=True)
            return

        self._show_notice("Settings saved.")

    async def _load_skills_snapshot(
        self,
        filters: SkillsFilters,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        projects_task = self.client.list_projects()
        agents_task = self.client.list_agents()
        skills_task = self.client.list_skills(
            project_name=filters.project_name,
            agent_profile=filters.agent_profile,
        )
        projects, agents, skills = await asyncio.gather(projects_task, agents_task, skills_task)
        agent_options = [
            {"value": item.get("key", ""), "label": item.get("label", item.get("key", ""))}
            for item in agents
            if isinstance(item, dict)
        ]
        return projects, agent_options, skills

    async def _refresh_skills_screen(self, screen: SkillsScreen, filters: SkillsFilters) -> None:
        screen.set_busy(True, "Refreshing skills...")
        try:
            projects, agent_options, skills = await self._load_skills_snapshot(filters)
        except Exception as exc:
            screen.set_busy(False)
            screen.set_error(str(exc))
            return

        screen.set_filter_options(
            project_options=projects,
            agent_profile_options=agent_options,
        )
        screen.set_filters(filters)
        screen.set_skills(skills)
        screen.set_busy(False)

    async def _cmd_skills(self) -> None:
        filters = SkillsFilters()
        screen = SkillsScreen(filters=filters)
        await self._refresh_skills_screen(screen, filters)
        await self.push_screen_wait(screen)

    async def _cmd_compact(self) -> None:
        availability = self._compact_availability()
        if not availability.available:
            self._show_notice(availability.reason or "Compaction is unavailable.", error=True)
            return

        try:
            stats_payload, presets = await asyncio.gather(
                self.client.get_compaction_stats(self.current_context or ""),
                self.client.get_model_presets(),
            )
        except Exception as exc:
            self._show_notice(f"Failed to load compaction data: {exc}", error=True)
            return

        available = bool(stats_payload.get("ok"))
        reason = "" if available else str(stats_payload.get("message") or "Compaction is unavailable.")
        screen = CompactScreen(
            stats=stats_payload.get("stats"),
            presets=presets,
            available=available,
            reason=reason,
        )
        result = await self.push_screen_wait(screen)
        if result is None:
            return

        if not isinstance(result, CompactResult):
            raise TypeError(f"Unexpected compact result: {result!r}")

        response = await self.client.compact_chat(
            self.current_context or "",
            use_chat_model=result.use_chat_model,
            preset_name=result.preset_name,
        )
        if not response.get("ok"):
            self._show_notice(str(response.get("message") or "Compaction failed."), error=True)
            return

        self._show_notice(str(response.get("message") or "Compaction started."))

    async def _cmd_pause(self) -> None:
        availability = self._pause_availability()
        if not availability.available:
            self._show_notice(availability.reason or "Pause is unavailable.", error=True)
            return

        try:
            await self.client.pause_agent(self.current_context)
        except Exception as exc:
            self._show_notice(f"Pause failed: {exc}", error=True)
            return

        self._show_notice("Agent paused.")

    async def _cmd_nudge(self) -> None:
        availability = self._nudge_availability()
        if not availability.available:
            self._show_notice(availability.reason or "Nudge is unavailable.", error=True)
            return

        input_widget = self.query_one("#message-input", ChatInput)
        input_widget.disabled = True
        self.agent_active = True
        self._response_delivered = False
        self._sync_ready_actions()
        try:
            await self.client.send_message(".", self.current_context or "")
        except Exception as exc:
            self._show_notice(f"Nudge failed: {exc}", error=True)
            input_widget.disabled = False
            self.agent_active = False
            self._sync_ready_actions()

    async def _cmd_exit(self) -> None:
        await self.client.disconnect()
        self.exit()

    async def on_skills_refresh_requested(self, event: SkillsRefreshRequested) -> None:
        if isinstance(self.screen, SkillsScreen):
            await self._refresh_skills_screen(self.screen, event.filters)

    async def on_skills_filters_changed(self, event: SkillsFiltersChanged) -> None:
        if isinstance(self.screen, SkillsScreen):
            await self._refresh_skills_screen(self.screen, event.filters)

    async def on_skills_delete_requested(self, event: SkillsDeleteRequested) -> None:
        if not isinstance(self.screen, SkillsScreen):
            return

        self.screen.set_busy(True, f"Deleting {event.skill.name}...")
        try:
            await self.client.delete_skill(event.skill.path)
        except Exception as exc:
            self.screen.set_busy(False)
            self.screen.set_error(str(exc))
            return

        await self._refresh_skills_screen(self.screen, event.filters)

    async def action_clear_chat(self) -> None:
        await self._cmd_clear()

    async def action_list_chats(self) -> None:
        self.run_worker(self._cmd_chats(), exclusive=True, name="cmd-chats")

    async def action_nudge_agent(self) -> None:
        await self._cmd_nudge()

    async def action_pause_agent(self) -> None:
        await self._cmd_pause()
