from __future__ import annotations

import asyncio
import os
from typing import Any, Iterable, Mapping

from textual import events
from textual.app import App, ComposeResult, SystemCommand
from textual.binding import Binding
from textual.command import CommandPalette
from textual.css.query import NoMatches
from textual.geometry import Offset
from textual.reactive import reactive
from textual.theme import Theme
from textual.widgets import ContentSwitcher

from agent_zero_cli import (
    availability,
    chat_commands,
    compaction,
    connection,
    event_handlers,
    project_commands,
    splash_helpers,
)
from agent_zero_cli.client import A0Client, DEFAULT_HOST
from agent_zero_cli.commands import CommandAvailability, CommandSpec
from agent_zero_cli.config import CLIConfig, load_config
from agent_zero_cli.instance_discovery import DiscoveryResult, discover_local_instances
from agent_zero_cli.remote_exec import PythonTTYManager
from agent_zero_cli.remote_files import RemoteFileUtility
from agent_zero_cli.project_utils import normalize_project_list, normalize_project_summary
from agent_zero_cli.widgets.command_palette import (
    AgentCommandPalette,
    OrderedSystemCommandsProvider,
)
from agent_zero_cli.widgets import (
    ChatInput,
    ConnectionStatus,
    DynamicFooter,
    ModelSwitcherBar,
    ProjectMenuItem,
    ProjectMenuPopover,
    SplashAction,
    SplashState,
    SplashView,
)
from agent_zero_cli.widgets.chat_log import ChatLog
from agent_zero_cli.model_commands import (
    cmd_model_presets,
    cmd_models,
    set_model_preset,
    refresh_model_switcher,
    clear_model_switcher,
)
from agent_zero_cli.token_usage import (
    start_token_refresh,
    stop_token_refresh,
    refresh_token_usage,
)


class AgentZeroCLI(App):
    """Agent Zero CLI - terminal-native connector shell."""

    CSS_PATH = "styles/app.tcss"
    TITLE = "Agent Zero CLI"
    BINDINGS = [
        Binding("Ctrl+C", "Quit", "Exit", show=True),
        Binding("F3", "toggle_remote_file_mode", "Read&Write", show=True, priority=True),
        Binding("F4", "toggle_remote_exec", "Code-exec on", show=True, priority=True),
        Binding("F5", "clear_chat", "Clear", show=True, priority=True),
        Binding("F6", "list_chats", "Chats", show=True, priority=True),
        Binding("F7", "nudge_agent", "Nudge", show=True, priority=True),
        Binding("F8", "pause_agent", "Pause", show=True, priority=True),
        Binding(
            "Ctrl+P",
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
        base_url = self.config.instance_url or DEFAULT_HOST
        self.client = A0Client(base_url, api_key=self.config.api_key)
        self.capabilities: dict[str, Any] = {}
        self.connector_features: set[str] = set()
        self.project_list: list[dict[str, str]] = []
        self.current_project: dict[str, str] | None = None
        self.current_context: str | None = None
        self.current_context_has_messages = False
        self._response_delivered = False
        self._context_run_complete = False
        self._chat_intro_pending = True
        self._remote_file_write_enabled = False
        self._remote_exec_enabled = False
        self._remote_files = RemoteFileUtility(
            scan_root=os.getcwd(),
            allow_writes=self._remote_file_write_enabled,
        )
        self._python_tty = PythonTTYManager(
            cwd=self._remote_files.scan_root,
            enabled=self._remote_exec_enabled,
        )
        self._local_workspace = self._remote_files.scan_root
        self._remote_workspace = ""
        self._token_refresh_task: asyncio.Task[None] | None = None
        self._splash_state = SplashState(
            stage="host",
            host=self.config.instance_url or DEFAULT_HOST,
            local_workspace=self._local_workspace,
            remote_workspace=self._remote_workspace,
        )
        self._command_registry = self._build_command_registry()
        self._command_lookup = {
            name: spec
            for spec in self._command_registry
            for name in spec.names()
        }
        self._remote_tree_task: asyncio.Task[None] | None = None
        self._last_remote_tree_hash = ""
        self._model_switch_allowed = False
        self._pause_latched = False
        self._slash_palette_query: str | None = None
        self._compaction_refresh_context: str | None = None
        self._project_menu_popover: ProjectMenuPopover | None = None
        self._instance_discovery_generation = 0

    def compose(self) -> ComposeResult:
        yield ConnectionStatus(id="connection-status")
        with ContentSwitcher(initial="splash-view", id="body-switcher"):
            yield SplashView()
            yield ChatLog(id="chat-log")
        yield ModelSwitcherBar(id="model-switcher-bar")
        yield ChatInput(id="message-input")
        yield DynamicFooter()

    async def on_mount(self) -> None:
        input_widget = self.query_one("#message-input", ChatInput)
        input_widget.disabled = True
        self.query_one("#model-switcher-bar", ModelSwitcherBar).clear()
        self.query_one("#splash-view", SplashView).set_state(self._splash_state)
        self._sync_workspace_widgets()
        self.query_one("#connection-status", ConnectionStatus).clear_token_usage()
        self._clear_project_state()
        self._sync_composer_visibility()

        log = self.query_one("#chat-log", ChatLog)
        self.set_interval(0.1, log.advance_shimmer)
        self._sync_connection_status("disconnected", self.config.instance_url or "")
        self._sync_body_mode()
        self._focus_splash_primary()
        self.run_worker(self._startup(), exclusive=True, name="startup")

    def get_system_commands(self, screen) -> Iterable[SystemCommand]:
        del screen  # unused; provider iterates App-level ordered commands.
        for spec, _ in self._iter_ui_commands():
            command = spec.canonical_name
            worker_name = f"palette-{command.lstrip('/').replace('/', '-')}"
            yield SystemCommand(
                command,
                spec.description,
                lambda command=command, worker_name=worker_name: self.run_worker(
                    self._dispatch_command(command),
                    exclusive=True,
                    name=worker_name,
                ),
            )

    def _build_command_registry(self) -> tuple[CommandSpec, ...]:
        return (
            CommandSpec(
                "/new",
                (),
                "Create a brand-new empty chat context.",
                lambda app: availability.require_features(app, "chat_create"),
                lambda app: chat_commands.cmd_new(app),
            ),
            CommandSpec(
                "/chats",
                (),
                "List previous chats and switch contexts.",
                lambda app: availability.require_features(app, "chats_list"),
                lambda app: chat_commands.cmd_chats(app),
            ),
            CommandSpec(
                "/project",
                ("/projects",),
                "Open the project menu and edit current project instructions.",
                lambda app: availability.project_availability(app),
                lambda app: project_commands.cmd_project(app),
            ),
            CommandSpec(
                "/compact",
                (),
                "Open the connector-backed compaction confirmation flow.",
                lambda app: availability.compact_availability(app),
                lambda app: compaction.cmd_compact(app),
            ),
            CommandSpec(
                "/presets",
                (),
                "Open preset picker with Main/Utility model details.",
                lambda app: availability.model_presets_availability(app),
                lambda app: app._cmd_model_presets(),
            ),
            CommandSpec(
                "/models",
                (),
                "Open Main/Utility model runtime editor.",
                lambda app: availability.model_runtime_availability(app),
                lambda app: app._cmd_models(),
            ),
            CommandSpec(
                "/keys",
                (),
                "Show or hide key and widget help.",
                lambda app: CommandAvailability(True),
                lambda app: chat_commands.cmd_keys(app),
            ),
            CommandSpec(
                "/disconnect",
                (),
                "Disconnect and return to the current host sign-in screen.",
                lambda app: availability.require_connection(app),
                lambda app: chat_commands.cmd_disconnect(app),
            ),
            CommandSpec(
                "/help",
                (),
                "Show the commands available in this shell.",
                lambda app: CommandAvailability(True),
                lambda app: chat_commands.cmd_help(app),
            ),
            CommandSpec(
                "/quit",
                (),
                "Disconnect and exit the CLI.",
                lambda app: CommandAvailability(True),
                lambda app: chat_commands.cmd_quit(app),
            ),
        )

    def action_command_palette(self) -> None:
        self._open_command_palette()

    def _is_command_palette_open(self) -> bool:
        try:
            return CommandPalette.is_open(self)
        except Exception:
            return False

    def _open_command_palette(self, *, initial_query: str = "", from_slash: bool = False) -> None:
        if not self.use_command_palette or self._is_command_palette_open():
            return

        self._slash_palette_query = initial_query if from_slash else None
        self.push_screen(
            AgentCommandPalette(
                providers=[OrderedSystemCommandsProvider],
                id="--command-palette",
                initial_query=initial_query,
            )
        )

    def _iter_ui_commands(self) -> tuple[tuple[CommandSpec, CommandAvailability], ...]:
        rows: list[tuple[CommandSpec, CommandAvailability]] = []
        for spec in self._command_registry:
            availability = spec.availability(self)
            if spec.canonical_name in {"/presets", "/models", "/project", "/disconnect"} and not availability.available:
                continue
            rows.append((spec, availability))
        return tuple(rows)

    def _sync_connection_status(self, status: str, url: str | None = None) -> None:
        widget = self.query_one("#connection-status", ConnectionStatus)
        widget.status = status
        if url is not None:
            widget.url = url
        widget.set_project_enabled(
            self.connected and bool(self.current_context) and "projects" in self.connector_features
        )

    def _set_token_usage(self, token_count: object, token_limit: object = None) -> None:
        self.query_one("#connection-status", ConnectionStatus).set_token_usage(token_count, token_limit)

    def _clear_token_usage(self) -> None:
        self.query_one("#connection-status", ConnectionStatus).clear_token_usage()

    def _apply_projects_payload(self, payload: Mapping[str, Any] | None) -> None:
        if not isinstance(payload, Mapping):
            self._clear_project_state()
            return

        self.project_list = normalize_project_list(payload.get("projects"))
        self.current_project = normalize_project_summary(payload.get("current_project"))
        self._sync_project_header()

    def _clear_project_state(self) -> None:
        self.project_list = []
        self.current_project = None
        self._sync_project_header()

    def _sync_project_header(self) -> None:
        widget = self.query_one("#connection-status", ConnectionStatus)
        widget.set_project_state(
            self.current_project,
            enabled=self.connected and bool(self.current_context) and "projects" in self.connector_features,
        )

    def _is_project_menu_open(self) -> bool:
        return self._project_menu_popover is not None

    def _stop_token_refresh(self) -> None:
        stop_token_refresh(self)

    def _start_token_refresh(self) -> None:
        start_token_refresh(self)

    async def _refresh_token_usage(self, *, context_id: str | None = None, silent: bool = True) -> None:
        await refresh_token_usage(self, context_id=context_id, silent=silent)

    async def _refresh_projects(self, *, context_id: str | None = None, silent: bool = True) -> None:
        target_context = context_id or self.current_context
        if "projects" not in self.connector_features or not target_context:
            self._clear_project_state()
            return

        try:
            payload = await self.client.get_projects(target_context)
        except Exception as exc:
            if not silent:
                self._show_notice(f"Failed to refresh projects: {exc}", error=True)
            return

        if not isinstance(payload, Mapping):
            self._clear_project_state()
            return
        if not payload.get("ok"):
            if not silent:
                self._show_notice(str(payload.get("error") or "Project state unavailable."), error=True)
            self._clear_project_state()
            return

        self._apply_projects_payload(payload)

    async def _refresh_workspace_from_settings(self) -> None:
        await splash_helpers.refresh_workspace_from_settings(self)

    async def _open_project_menu(self) -> None:
        await self._refresh_projects(context_id=self.current_context, silent=False)
        if self._project_menu_popover is not None:
            self.call_after_refresh(self._position_project_menu_popover)
            self.call_after_refresh(self._project_menu_popover.focus_first_item)
            return

        popover = ProjectMenuPopover(
            self.project_list,
            current_project=self.current_project,
            id="project-menu-popover",
        )
        self._project_menu_popover = popover
        await self.mount(popover)
        self.call_after_refresh(self._position_project_menu_popover)
        self.call_after_refresh(popover.focus_first_item)

    async def _toggle_project_menu(self) -> None:
        if self._project_menu_popover is not None:
            await self._hide_project_menu()
            return
        await self._open_project_menu()

    async def _hide_project_menu(self) -> None:
        popover = self._project_menu_popover
        self._project_menu_popover = None
        if popover is None:
            return
        await popover.remove()

    def _position_project_menu_popover(self) -> None:
        popover = self._project_menu_popover
        if popover is None:
            return

        try:
            status = self.query_one("#connection-status", ConnectionStatus)
        except NoMatches:
            return

        menu_width = popover.region.width or popover.outer_size.width or 38
        screen_width = self.screen.size.width
        x = max(0, screen_width - menu_width - 2)
        y = max(0, status.region.y + status.region.height)
        popover.absolute_offset = Offset(x, y)
        popover.refresh(layout=True)

    async def _handle_project_menu_action(self, action: str, project_name_value: str | None = None) -> None:
        await self._hide_project_menu()
        await project_commands.handle_project_menu_action(
            self,
            action,
            project_name_value=project_name_value,
        )

    def on_resize(self, event: events.Resize) -> None:
        del event
        self._position_project_menu_popover()

    def _splash_host(self) -> str:
        return splash_helpers.splash_host(self)

    def _normalize_host(self, host: str) -> str:
        return splash_helpers.normalize_host(host)

    def _set_splash_state(self, **changes: Any) -> None:
        splash_helpers.set_splash_state(self, **changes)

    def _sync_workspace_widgets(self) -> None:
        splash_helpers.sync_workspace_widgets(self)

    def _set_workspace_context(
        self,
        *,
        local_workspace: str | None = None,
        remote_workspace: str | None = None,
    ) -> None:
        splash_helpers.set_workspace_context(
            self,
            local_workspace=local_workspace,
            remote_workspace=remote_workspace,
        )

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
        login_error: str | None = None,
        actions: tuple[SplashAction, ...] | None = None,
    ) -> None:
        splash_helpers.set_splash_stage(
            self,
            stage,
            message=message,
            detail=detail,
            host=host,
            username=username,
            password=password,
            save_credentials=save_credentials,
            login_error=login_error,
            actions=actions,
        )

    def _sync_ready_actions(self) -> None:
        splash_helpers.sync_ready_actions(self)

    def _set_pause_latched(self, value: bool) -> None:
        if self._pause_latched == value:
            return
        self._pause_latched = value
        self.refresh_bindings()
        self._sync_ready_actions()

    def _set_remote_file_write_enabled(self, value: bool) -> None:
        if self._remote_file_write_enabled == value:
            return
        self._remote_file_write_enabled = value
        self._remote_files.set_write_enabled(value)
        self.refresh_bindings()

    def _set_remote_exec_enabled(self, value: bool) -> None:
        if self._remote_exec_enabled == value:
            return
        self._remote_exec_enabled = value
        self._python_tty.set_enabled(value)
        self.refresh_bindings()

    def _sync_body_mode(self) -> None:
        splash_helpers.sync_body_mode(self)

    def _sync_composer_visibility(self) -> None:
        splash_helpers.sync_composer_visibility(self)

    def _set_activity(self, label: str, detail: str = "") -> None:
        self.query_one("#message-input", ChatInput).set_activity(label, detail)

    def _set_idle(self) -> None:
        self.query_one("#message-input", ChatInput).set_idle()
        try:
            self.query_one("#chat-log", ChatLog).dim_active_status()
        except Exception:
            pass

    def _focus_splash_primary(self) -> None:
        splash_helpers.focus_splash_primary(self)

    def _focus_message_input(self) -> None:
        splash_helpers.focus_message_input(self)

    def _show_notice(self, message: str, *, error: bool = False) -> None:
        splash_helpers.show_notice(self, message, error=error)

    def get_binding_description(self, binding: Binding) -> str:
        if binding.action == "toggle_remote_file_mode":
            return "Read&Write" if self._remote_file_write_enabled else "Read-only"
        if binding.action == "toggle_remote_exec":
            return "Code-exec ON" if self._remote_exec_enabled else "Code-exec OFF"
        if binding.action == "pause_agent":
            return "Resume" if self._pause_latched else "Pause"
        return binding.description

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
        clear_model_switcher(self)

    def _apply_model_switcher_state(self, payload: dict[str, Any]) -> None:
        from agent_zero_cli.model_config import apply_model_switcher_state
        allowed, state_kwargs = apply_model_switcher_state(payload)
        self._model_switch_allowed = allowed
        try:
            widget = self.query_one("#model-switcher-bar", ModelSwitcherBar)
            widget.set_state(**state_kwargs)
        except Exception:
            pass

    async def _refresh_model_switcher(self, *, silent: bool = True) -> None:
        await refresh_model_switcher(self, silent=silent)

    def _command_display(self, spec: CommandSpec) -> str:
        if not spec.aliases:
            return spec.canonical_name
        aliases = ", ".join(spec.aliases)
        return f"{spec.canonical_name} ({aliases})"

    def _available_help_lines(self) -> tuple[list[str], list[str]]:
        return splash_helpers.available_help_lines(self)

    def _surface_help(self) -> None:
        splash_helpers.surface_help(self)

    def _run_on_ui(self, func: Any, *args: Any) -> None:
        app_loop = getattr(self, "loop", None)
        if app_loop is None:
            func(*args)
        else:
            app_loop.call_soon_threadsafe(func, *args)

    def _set_connected(self, value: bool) -> None:
        connection.set_connected(self, value)

    def _require_connection(self) -> CommandAvailability:
        return availability.require_connection(self)

    def _require_features(self, *features: str) -> CommandAvailability:
        return availability.require_features(self, *features)

    def _compact_availability(self) -> CommandAvailability:
        return availability.compact_availability(self)

    def _pause_availability(self) -> CommandAvailability:
        return availability.pause_availability(self)

    def _resume_availability(self) -> CommandAvailability:
        return availability.resume_availability(self)

    def _nudge_availability(self) -> CommandAvailability:
        return availability.nudge_availability(self)

    def _project_availability(self) -> CommandAvailability:
        return availability.project_availability(self)

    def _model_presets_availability(self) -> CommandAvailability:
        return availability.model_presets_availability(self)

    def _model_runtime_availability(self) -> CommandAvailability:
        return availability.model_runtime_availability(self)

    def _welcome_actions(self) -> tuple[SplashAction, ...]:
        return splash_helpers.welcome_actions(self)

    def _start_instance_discovery(self, *, auto_connect_single: bool = False) -> None:
        self._instance_discovery_generation += 1
        generation = self._instance_discovery_generation
        self._set_splash_state(
            discovery_status="loading",
            discovery_detail="",
        )
        self.run_worker(
            self._discover_local_instances(generation, auto_connect_single=auto_connect_single),
            exclusive=False,
            name=f"instance-discovery-{generation}",
        )

    async def _discover_local_instances(self, generation: int, *, auto_connect_single: bool = False) -> None:
        result = await discover_local_instances()
        if generation != self._instance_discovery_generation:
            return
        auto_connect_host = self._apply_instance_discovery_result(
            result,
            auto_connect_single=auto_connect_single,
        )
        if auto_connect_host:
            await self._begin_connection(auto_connect_host)

    def _apply_instance_discovery_result(
        self,
        result: DiscoveryResult,
        *,
        auto_connect_single: bool = False,
    ) -> str:
        instances = tuple(result.instances)
        discovered_urls = {instance.url for instance in instances}
        preferred_host = (self._splash_state.host or self.config.instance_url or "").strip()
        selected_host_url = self._splash_state.selected_host_url.strip()

        if selected_host_url in discovered_urls:
            resolved_selection = selected_host_url
        elif preferred_host in discovered_urls:
            resolved_selection = preferred_host
        elif instances:
            resolved_selection = instances[0].url
        else:
            resolved_selection = ""

        manual_entry_expanded = self._splash_state.manual_entry_expanded
        if not instances:
            manual_entry_expanded = True
        elif preferred_host and preferred_host != DEFAULT_HOST and preferred_host not in discovered_urls:
            manual_entry_expanded = True

        self._set_splash_state(
            host=preferred_host if manual_entry_expanded else (resolved_selection or preferred_host),
            discovered_instances=instances,
            discovery_status=result.status,
            discovery_detail=result.detail,
            selected_host_url=resolved_selection,
            manual_entry_expanded=manual_entry_expanded,
        )
        if auto_connect_single and len(instances) == 1 and resolved_selection and not manual_entry_expanded:
            return resolved_selection
        return ""

    async def _startup(self) -> None:
        await connection.startup(self)

    async def _fetch_capabilities(self) -> tuple[dict[str, Any] | None, bool, str]:
        return await connection.fetch_capabilities(self)

    def _validate_capabilities(self, capabilities: dict[str, Any]) -> None:
        connection.validate_capabilities(capabilities)

    async def _begin_connection(
        self,
        host: str,
        *,
        username: str = "",
        password: str = "",
        save_credentials_flag: bool = False,
    ) -> None:
        await connection.begin_connection(
            self,
            host,
            username=username,
            password=password,
            save_credentials_flag=save_credentials_flag,
        )

    def _handle_context_snapshot(self, data: dict[str, Any]) -> None:
        event_handlers.handle_context_snapshot(self, data)

    def _handle_context_event(self, data: dict[str, Any]) -> None:
        event_handlers.handle_context_event(self, data)

    def _handle_context_complete(self, data: dict[str, Any]) -> None:
        event_handlers.handle_context_complete(self, data)

    def _handle_connector_error(self, data: dict[str, Any]) -> None:
        event_handlers.handle_connector_error(self, data)

    def _handle_file_op(self, data: dict[str, Any]) -> dict[str, Any]:
        return event_handlers.handle_file_op(self, data)

    async def _handle_exec_op(self, data: dict[str, Any]) -> dict[str, Any]:
        return await event_handlers.handle_exec_op(self, data)

    def _start_remote_tree_publisher(self) -> None:
        event_handlers.start_remote_tree_publisher(self)

    def _stop_remote_tree_publisher(self) -> None:
        event_handlers.stop_remote_tree_publisher(self)

    async def _remote_tree_publish_loop(self) -> None:
        await event_handlers.remote_tree_publish_loop(self)

    async def _publish_remote_tree_snapshot(self) -> None:
        await event_handlers.publish_remote_tree_snapshot(self)

    def _slash_query(self, text: str) -> str | None:
        if not text:
            return None
        token = text.split(maxsplit=1)[0]
        if not token.startswith("/"):
            return None
        if len(text) != len(token):
            return None
        return token.lower()

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
            self._slash_palette_query = None
            return

        if text.startswith("/"):
            token = text.split(maxsplit=1)[0].strip().lower().lstrip("/") or "command"
            worker_name = f"slash-{token.replace('/', '-')}"
            self.run_worker(
                self._dispatch_command(text),
                exclusive=True,
                name=worker_name,
            )
            return

        if not self.current_context:
            self._show_notice("No active chat context.", error=True)
            return

        self._set_pause_latched(False)
        self._mark_context_has_messages()
        self._response_delivered = False
        self._context_run_complete = False
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
            return

        self._open_command_palette(initial_query=query, from_slash=True)

    def on_command_palette_closed(self, event: CommandPalette.Closed) -> None:
        del event
        query = self._slash_palette_query
        self._slash_palette_query = None
        if query is None:
            return

        try:
            input_widget = self.query_one("#message-input", ChatInput)
        except Exception:
            return
        if input_widget.value.strip().lower() == query:
            input_widget.value = ""

    async def on_model_switcher_bar_preset_changed(self, event: ModelSwitcherBar.PresetChanged) -> None:
        await self._set_model_preset(event.value or None, bar=event.bar)

    def on_model_switcher_bar_model_config_requested(
        self,
        event: ModelSwitcherBar.ModelConfigRequested,
    ) -> None:
        worker_name = f"cmd-models-{event.target}"
        self.run_worker(
            self._cmd_models(focus_target=event.target),
            exclusive=True,
            name=worker_name,
        )

    async def _set_model_preset(
        self,
        preset_name: str | None,
        *,
        bar: ModelSwitcherBar | None = None,
    ) -> None:
        await set_model_preset(self, preset_name, bar=bar)

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

    def on_splash_view_host_state_changed(self, event: SplashView.HostStateChanged) -> None:
        if self._splash_state.stage != "host":
            return
        self._set_splash_state(
            host=event.host,
            selected_host_url=event.selected_host_url,
            manual_entry_expanded=event.manual_entry_expanded,
        )

    def on_splash_view_action_requested(self, event: SplashView.ActionRequested) -> None:
        if event.action == "back":
            self._set_splash_stage(
                "host",
                message="",
                detail="",
                host=self._splash_host(),
                username=self._splash_state.username,
                password="",
                save_credentials=self._splash_state.save_credentials,
                login_error="",
            )
            self._start_instance_discovery(auto_connect_single=False)
            self._focus_splash_primary()
            return

        if event.action == "refresh-hosts":
            self._start_instance_discovery()
            return

        if event.action == "toggle-manual-host":
            self._set_splash_state(manual_entry_expanded=not self._splash_state.manual_entry_expanded)
            self._focus_splash_primary()
            return

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

        if not event.action.startswith("/"):
            return

        worker_name = f"splash-{event.action.lstrip('/').replace('/', '-')}"
        self.run_worker(self._dispatch_command(event.action), exclusive=True, name=worker_name)

    def on_connection_status_project_requested(self, event: ConnectionStatus.ProjectRequested) -> None:
        del event
        self.run_worker(self._toggle_project_menu(), exclusive=True, name="toggle-project-menu")

    def on_project_menu_popover_dismiss_requested(self, event: ProjectMenuPopover.DismissRequested) -> None:
        del event
        self.run_worker(self._hide_project_menu(), exclusive=True, name="hide-project-menu")

    def on_project_menu_item_selected(self, event: ProjectMenuItem.Selected) -> None:
        self.run_worker(
            self._handle_project_menu_action(event.action, event.project_name),
            exclusive=True,
            name=f"project-menu-{event.action}",
        )

    async def _cmd_clear(self) -> None:
        await chat_commands.cmd_clear(self)

    async def _switch_context(self, context_id: str, *, has_messages_hint: bool) -> None:
        await chat_commands.switch_context(self, context_id, has_messages_hint=has_messages_hint)

    def _cancel_compaction_refresh(self) -> None:
        compaction.cancel_compaction_refresh(self)

    def _finalize_compaction_refresh(self, context_id: str) -> None:
        compaction.finalize_compaction_refresh(self, context_id)

    def _begin_compaction_refresh(self, context_id: str) -> None:
        compaction.begin_compaction_refresh(self, context_id)

    async def _wait_for_compaction_and_reload(self, context_id: str) -> None:
        await compaction.wait_for_compaction_and_reload(self, context_id)

    async def _cmd_chats(self) -> None:
        await chat_commands.cmd_chats(self)

    async def _cmd_new(self) -> None:
        await chat_commands.cmd_new(self)

    async def _cmd_project(self) -> None:
        await project_commands.cmd_project(self)

    async def _cmd_model_presets(self) -> None:
        await cmd_model_presets(self)

    async def _cmd_models(self, *, focus_target: str = "main") -> None:
        await cmd_models(self, focus_target=focus_target)

    async def _cmd_compact(self) -> None:
        await compaction.cmd_compact(self)

    async def _cmd_pause(self) -> None:
        await chat_commands.cmd_pause(self)

    async def _cmd_resume(self) -> None:
        await chat_commands.cmd_resume(self)

    async def _cmd_nudge(self) -> None:
        await chat_commands.cmd_nudge(self)

    async def _disconnect_and_exit(self) -> None:
        await connection.disconnect_and_exit(self)

    async def _disconnect_to_login(self) -> None:
        await connection.disconnect_to_login(self)

    async def action_clear_chat(self) -> None:
        await self._cmd_clear()

    async def action_toggle_remote_file_mode(self) -> None:
        self._set_remote_file_write_enabled(not self._remote_file_write_enabled)
        mode = "Read&Write" if self._remote_file_write_enabled else "Read only"
        self._show_notice(f"Local access: {mode}.")

    async def action_toggle_remote_exec(self) -> None:
        self._set_remote_exec_enabled(not self._remote_exec_enabled)
        mode = "enabled" if self._remote_exec_enabled else "disabled"
        self._show_notice(f"Remote execution {mode} for this CLI session.")

    async def action_list_chats(self) -> None:
        self.run_worker(self._cmd_chats(), exclusive=True, name="cmd-chats")

    async def action_nudge_agent(self) -> None:
        await self._cmd_nudge()

    async def action_pause_agent(self) -> None:
        if self._pause_latched:
            await self._cmd_resume()
            return
        await self._cmd_pause()

    async def action_quit(self) -> None:
        await self._disconnect_and_exit()

    async def action_disconnect(self) -> None:
        await self._disconnect_to_login()
