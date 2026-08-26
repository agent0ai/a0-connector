from __future__ import annotations

import asyncio
import os
from functools import partial
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
    __version__,
    availability,
    browser_commands,
    chat_commands,
    compaction,
    connection,
    event_handlers,
    goal_commands,
    permissions_commands,
    plugin_commands,
    profile_commands,
    project_commands,
    self_update,
    splash_helpers,
    state_sync,
)
from agent_zero_cli.client import A0Client, DEFAULT_HOST
from agent_zero_cli.attachments import (
    AttachmentError,
    create_clipboard_image_upload,
)
from agent_zero_cli.clipboard import (
    copy_text_to_windows_clipboard,
    should_use_native_windows_clipboard,
)
from agent_zero_cli.computer_use import ComputerUseManager
from agent_zero_cli.host_browser import HostBrowserManager
from agent_zero_cli.commands import CommandAvailability, CommandSpec
from agent_zero_cli.config import CLIConfig, load_config, save_last_context
from agent_zero_cli.instance_discovery import DiscoveryResult, discover_local_instances
from agent_zero_cli.image_render import ImageRenderer
from agent_zero_cli.image_store import ImageStore, ImageUnavailableError
from agent_zero_cli.media_refs import ImageReference
from agent_zero_cli.remote_exec import PythonTTYManager
from agent_zero_cli.remote_files import RemoteFileUtility
from agent_zero_cli.project_utils import (
    normalize_project_list,
    normalize_project_summary,
    project_color,
    project_name,
)
from agent_zero_cli.widgets.command_palette import (
    AgentCommandPalette,
    OrderedSystemCommandsProvider,
    is_raw_skill_command,
    reference_query_at_cursor,
)
from agent_zero_cli.widgets import (
    ChatInput,
    ComputerUseBanner,
    ConnectionStatus,
    ContextTab,
    ContextTabs,
    DynamicFooter,
    GoalBar,
    MessageQueueBar,
    ModelSwitcherBar,
    ProfileMenuItem,
    ProfileMenuPopover,
    ProjectMenuItem,
    ProjectMenuPopover,
    SplashAction,
    SplashState,
    SplashView,
    context_tab_from_metadata,
)
from agent_zero_cli.widgets.chat_log import ChatLog
from agent_zero_cli.widgets.image_entry import ImageEntry
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

_HIDDEN_SLASH_COMMANDS = frozenset({"/computer", "/cu"})
_SPLASH_HIDDEN_COMMANDS = frozenset({"/profile", "/pause", "/resume", "/nudge"})
_NO_AUTO_SLASH_PALETTE_COMMANDS = frozenset({"/attach", "/image", "/img"})
_COMPUTER_USE_MODE_LABELS = {
    "interactive": "Permission Prompt",
    "persistent": "Permission Prompt",
    "allow": "Allow",
}
_COMPUTER_USE_STATUS_LABELS = {
    **_COMPUTER_USE_MODE_LABELS,
    "active": "Active",
    "arming": "Arming",
    "approval required": "Approval Required",
    "disabled": "Disabled",
    "rearm required": "Rearm Required",
}


def _computer_use_label(value: str) -> str:
    normalized = str(value or "").strip().lower()
    return _COMPUTER_USE_STATUS_LABELS.get(normalized, str(value or "").strip())


def _computer_use_mode_label(value: str) -> str:
    normalized = str(value or "").strip().lower()
    return _COMPUTER_USE_MODE_LABELS.get(normalized, str(value or "").strip())


def _computer_use_result_code(result: Mapping[str, Any] | None) -> str:
    if not isinstance(result, Mapping):
        return ""
    return str(result.get("code") or result.get("error") or "").strip()


def _computer_use_result_message(result: Mapping[str, Any] | None) -> str:
    if not isinstance(result, Mapping):
        return ""
    return str(result.get("error") or result.get("code") or "").strip()


class AgentZeroCLI(App):
    """Agent Zero CLI - terminal-native connector shell."""

    CSS_PATH = "styles/app.tcss"
    TITLE = "Agent Zero CLI"
    # Textual reports function keys as lowercase identifiers like `f3`.
    # Keep the canonical key names here and use `key_display` for the footer.
    BINDINGS = [
        Binding("Ctrl+C", "Quit", "Exit", show=False),
        Binding("Ctrl+Q", "Quit", "Exit", show=False),
        Binding(
            "f3",
            "toggle_remote_file_mode",
            "Read&Write",
            show=True,
            priority=True,
            key_display="F3",
        ),
        Binding(
            "f4",
            "toggle_remote_exec",
            "Code-exec on",
            show=True,
            priority=True,
            key_display="F4",
        ),
        Binding("f5", "clear_chat", "Clear", show=False, priority=True, key_display="F5"),
        Binding("f6", "list_chats", "Chats", show=True, priority=True, key_display="F6"),
        Binding("f7", "nudge_agent", "Nudge", show=True, priority=True, key_display="F7"),
        Binding("f8", "pause_agent", "Pause", show=True, priority=True, key_display="F8"),
        Binding("f9", "copy_visible_chat", "Copy", show=False, priority=True, key_display="F9"),
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

    def __init__(
        self,
        config: CLIConfig | None = None,
        *,
        auto_connect_single_instance: bool = True,
        discover_instances: bool = True,
        connect_configured_host: bool = False,
        image_renderer: ImageRenderer | None = None,
    ) -> None:
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
        self.image_renderer = image_renderer or ImageRenderer.disabled()
        base_url = self.config.instance_url or DEFAULT_HOST
        self.client = A0Client(base_url)
        self.image_store = ImageStore(
            self.client,
            max_surface_pixels=self.image_renderer.max_surface_pixels,
        )
        self._image_load_epoch = 0
        self.capabilities: dict[str, Any] = {}
        self.connector_features: set[str] = set()
        self.project_list: list[dict[str, str]] = []
        self.current_project: dict[str, str] | None = None
        self.current_context: str | None = None
        self.current_context_has_messages = False
        self._context_tabs: list[ContextTab] = []
        self.message_queue: list[dict[str, Any]] = []
        self.goal: dict[str, Any] | None = None
        self.show_utility_messages = False
        self._response_delivered = False
        self._context_run_complete = False
        self._run_started_at: float | None = None
        self._last_response_sequence: int | None = None
        self._chat_intro_pending = True
        self._remote_file_write_enabled = True
        self._remote_exec_enabled = self.config.remote_exec_enabled
        self._remote_tool_metadata_error = ""
        self._remote_files = RemoteFileUtility(
            scan_root=os.getcwd(),
            allow_writes=self._remote_file_write_enabled,
        )
        self._python_tty = PythonTTYManager(
            cwd=self._remote_files.scan_root,
            enabled=self._remote_exec_enabled,
            allow_writes=self._remote_file_write_enabled,
        )
        self._computer_use = ComputerUseManager(self.config)
        self._computer_use.set_status_callback(
            lambda label, detail: self._run_on_ui(self._apply_computer_use_status, label, detail)
        )
        self._host_browser = HostBrowserManager(self.config)
        self._local_workspace = self._remote_files.scan_root
        self._remote_workspace = ""
        self._token_refresh_task: asyncio.Task[None] | None = None
        self._state_sync_task: asyncio.Task[None] | None = None
        self._websocket_recovery_task: asyncio.Task[None] | None = None
        self._splash_state = SplashState(
            stage="host",
            host=self.config.instance_url or DEFAULT_HOST,
            remember_host=self.config.remember_host,
            local_workspace=self._local_workspace,
            remote_workspace=self._remote_workspace,
        )
        self._command_registry = self._build_command_registry()
        self._command_lookup = {
            name: spec
            for spec in self._command_registry
            for name in spec.names()
        }
        self._server_commands: tuple[dict[str, Any], ...] = ()
        self._server_commands_context: str | None = None
        self._remote_tree_task: asyncio.Task[None] | None = None
        self._last_remote_tree_hash = ""
        self._last_remote_tree_published_at = 0.0
        self._model_switch_allowed = False
        self._settings_snapshot_signature = ""
        self._model_switcher_signature = ""
        self._model_switcher_signature_pending = ""
        self._model_switcher_signature_pending_retries = 0
        self._pause_latched = False
        self._slash_palette_query: str | None = None
        self._reference_palette_range: tuple[tuple[int, int], tuple[int, int]] | None = None
        self._skill_palette_cache: list[dict[str, Any]] = []
        self._skill_palette_cache_key: tuple[str, str] | None = None
        self._compaction_refresh_context: str | None = None
        self._profile_menu_popover: ProfileMenuPopover | None = None
        self._project_menu_popover: ProjectMenuPopover | None = None
        self._instance_discovery_generation = 0
        self._auto_connect_single_instance = auto_connect_single_instance
        self._discover_instances = discover_instances
        self._connect_configured_host = connect_configured_host
        self._splash_hidden_commands = _SPLASH_HIDDEN_COMMANDS
        self._cli_update_check_started = False

    def compose(self) -> ComposeResult:
        yield ConnectionStatus(id="connection-status")
        yield ContextTabs(id="context-tabs")
        with ContentSwitcher(initial="splash-view", id="body-switcher"):
            yield SplashView()
            yield ChatLog(image_renderer=self.image_renderer, id="chat-log")
        yield ComputerUseBanner(id="computer-use-banner")
        yield GoalBar(id="goal-bar")
        yield ModelSwitcherBar(id="model-switcher-bar")
        yield MessageQueueBar(id="message-queue-bar")
        yield ChatInput(id="message-input")
        yield DynamicFooter()

    def copy_to_clipboard(self, text: str) -> None:
        """Copy text via Textual and mirror it to the Windows clipboard when needed."""
        super().copy_to_clipboard(text)
        if should_use_native_windows_clipboard():
            copy_text_to_windows_clipboard(text)

    async def attach_clipboard_image(self) -> bool:
        try:
            upload = await asyncio.to_thread(create_clipboard_image_upload)
        except AttachmentError:
            return False
        except Exception as exc:
            self._show_notice(f"Error attaching clipboard image: {exc}", error=True)
            return True

        try:
            attachments = await self.client.upload_attachments([upload])
        except Exception as exc:
            self._show_notice(f"Error uploading clipboard image: {exc}", error=True)
            return True

        if not attachments:
            self._show_notice("Error uploading clipboard image: no attachment was returned.", error=True)
            return True

        attachment = attachments[0]
        try:
            self.query_one("#message-input", ChatInput).add_attachment(attachment)
        except Exception:
            return False
        self._show_notice(f"Attached {attachment.name}.")
        return True

    async def on_mount(self) -> None:
        if self.image_renderer.notice:
            self._show_notice(self.image_renderer.notice)
        input_widget = self.query_one("#message-input", ChatInput)
        input_widget.disabled = True
        self.query_one("#goal-bar", GoalBar).clear()
        self.query_one("#model-switcher-bar", ModelSwitcherBar).clear()
        self.query_one("#message-queue-bar", MessageQueueBar).clear()
        self._sync_context_tabs()
        self.query_one("#splash-view", SplashView).set_state(self._splash_state)
        self._sync_workspace_widgets()
        self.query_one("#connection-status", ConnectionStatus).clear_token_usage()
        self._clear_project_state()
        self._sync_computer_use_status()
        self._sync_composer_visibility()

        log = self.query_one("#chat-log", ChatLog)
        self.set_interval(0.1, log.advance_shimmer)
        self._sync_connection_status("disconnected", self.config.instance_url or "")
        self._sync_body_mode()
        self._focus_splash_primary()
        self._start_cli_update_check()
        self.run_worker(self._startup(), exclusive=True, name="startup")

    def get_system_commands(self, screen) -> Iterable[SystemCommand]:
        del screen  # unused; provider iterates App-level ordered commands.
        for spec, _ in self._iter_ui_commands():
            command = spec.canonical_name
            worker_name = f"palette-{command.lstrip('/').replace('/', '-')}"
            yield SystemCommand(
                command,
                spec.description,
                lambda command=command, worker_name=worker_name: self._run_dispatch_command(
                    command,
                    worker_name=worker_name,
                ),
            )
        for command in self._server_commands:
            name = f"/{command['name']}"
            worker_name = f"palette-{name.lstrip('/').replace('/', '-')}"
            description = str(command.get("description") or "Run Agent Zero command.").strip()
            argument_hint = str(command.get("argument_hint") or "").strip()
            source = str(command.get("source_scope_label") or command.get("scope_label") or "").strip()
            if argument_hint:
                description = f"{description} {argument_hint}"
            if source:
                description = f"{description} ({source})"
            yield SystemCommand(
                name,
                description,
                lambda name=name, worker_name=worker_name: self._run_dispatch_command(
                    name,
                    worker_name=worker_name,
                ),
            )
        yield SystemCommand(
            "Browser: Use Host",
            "Run Browser through A0 CLI against your Chromium-family browser.",
            lambda: self.run_worker(
                browser_commands.cmd_browser(self, query="host"),
                exclusive=True,
                name="palette-browser-host",
            ),
        )
        yield SystemCommand(
            "Browser: Docker Container",
            "Run Browser inside the Agent Zero Docker/container browser.",
            lambda: self.run_worker(
                browser_commands.cmd_browser(self, query="container"),
                exclusive=True,
                name="palette-browser-container",
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
                "List previous chats (default sorted by last updated). Use --project to filter by active project.",
                lambda app: availability.require_features(app, "chats_list"),
                lambda app: chat_commands.cmd_chats(app),
            ),
            CommandSpec(
                "/clear",
                (),
                "Clear the visible chat log.",
                lambda app: CommandAvailability(True),
                lambda app: chat_commands.cmd_clear(app),
            ),
            CommandSpec(
                "/project",
                ("/projects",),
                "Open the project menu, or switch directly with /project <name>.",
                lambda app: availability.project_availability(app),
                lambda app: project_commands.cmd_project(app),
            ),
            CommandSpec(
                "/profile",
                (),
                "Manage, select, or quickly create an Agent Zero Core profile.",
                lambda app: availability.profile_availability(app),
                lambda app: profile_commands.cmd_profile(app),
            ),
            CommandSpec(
                "/permissions",
                (),
                "Edit Tools, MCP, and Skill permissions for the current agent.",
                lambda app: availability.permissions_availability(app),
                lambda app: permissions_commands.cmd_permissions(app),
            ),
            CommandSpec(
                "/plugins",
                (),
                "Open the installed-only Agent Zero plugin toggle view.",
                lambda app: availability.installed_plugins_availability(app),
                lambda app: plugin_commands.cmd_plugins(app),
            ),
            CommandSpec(
                "/compact",
                (),
                "Open the connector-backed compaction confirmation flow.",
                lambda app: availability.compact_availability(app),
                lambda app: compaction.cmd_compact(app),
            ),
            CommandSpec(
                "/pause",
                (),
                "Pause the active agent run.",
                lambda app: availability.pause_availability(app),
                lambda app: chat_commands.cmd_pause(app),
            ),
            CommandSpec(
                "/resume",
                (),
                "Resume a paused agent run.",
                lambda app: availability.resume_availability(app),
                lambda app: chat_commands.cmd_resume(app),
            ),
            CommandSpec(
                "/nudge",
                (),
                "Nudge the current agent run.",
                lambda app: availability.nudge_availability(app),
                lambda app: chat_commands.cmd_nudge(app),
            ),
            CommandSpec(
                "/send",
                (),
                "Send all queued messages now.",
                lambda app: availability.message_queue_send_availability(app),
                lambda app: chat_commands.cmd_queue_send(app),
            ),
            CommandSpec(
                "/queue",
                (),
                "Show, send, clear, or remove queued messages.",
                lambda app: availability.message_queue_availability(app),
                lambda app: chat_commands.cmd_queue(app),
            ),
            CommandSpec(
                "/goal",
                (),
                "Create, inspect, update, pause, resume, or delete this chat goal.",
                lambda app: availability.goal_availability(app),
                lambda app: goal_commands.cmd_goal(app),
            ),
            CommandSpec(
                "/presets",
                (),
                "Open preset picker with Main, Utility, and Embedding model details.",
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
                "/browser",
                (),
                "Choose Browser host/container mode and manage host-browser control.",
                lambda app: browser_commands.browser_availability(app),
                lambda app: browser_commands.cmd_browser(app),
            ),
            CommandSpec(
                "/attach",
                ("/image", "/img"),
                "Attach local image file(s) to the next message.",
                lambda app: availability.attachments_availability(app),
                lambda app: chat_commands.cmd_attach(app),
            ),
            CommandSpec(
                "/computer-use",
                ("/computer", "/cu"),
                "Turn local Computer Use on or off.",
                lambda app: CommandAvailability(True),
                lambda app: app._cmd_computer_use(),
            ),
            CommandSpec(
                "/copy",
                (),
                "Copy the currently visible transcript text to the clipboard.",
                lambda app: CommandAvailability(True),
                lambda app: app._cmd_copy_visible_chat(),
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
                "Disconnect and return to the current host connection flow.",
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

    def _open_command_palette(
        self,
        *,
        initial_query: str = "",
        from_slash: bool = False,
        from_skill: bool = False,
    ) -> None:
        if not self.use_command_palette or self._is_command_palette_open():
            return

        self._slash_palette_query = initial_query if from_slash or from_skill else None
        self.push_screen(
            AgentCommandPalette(
                providers=[OrderedSystemCommandsProvider],
                id="--command-palette",
                initial_query=initial_query,
                from_slash=from_slash,
                from_skill=from_skill,
            )
        )

    def _iter_ui_commands(self) -> tuple[tuple[CommandSpec, CommandAvailability], ...]:
        rows: list[tuple[CommandSpec, CommandAvailability]] = []
        for spec in self._command_registry:
            availability = spec.availability(self)
            if spec.canonical_name in _HIDDEN_SLASH_COMMANDS:
                continue
            if (
                spec.canonical_name
                in {"/presets", "/models", "/profile", "/project", "/plugins", "/disconnect"}
                and not availability.available
            ):
                continue
            rows.append((spec, availability))
        return tuple(rows)

    async def _load_server_commands(self, *, force: bool = False) -> tuple[dict[str, Any], ...]:
        context_id = self.current_context if self.connected else None
        if not context_id:
            self._server_commands = ()
            self._server_commands_context = None
            return ()
        if not force and self._server_commands_context == context_id:
            return self._server_commands

        try:
            commands = await self.client.list_commands(context_id)
        except Exception:
            commands = []

        local_names = set(self._command_lookup)
        valid_characters = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_-")
        normalized: list[dict[str, Any]] = []
        for command in commands:
            if not isinstance(command, Mapping):
                continue
            name = str(command.get("name") or "").strip().lower().lstrip("/")
            if (
                not name
                or any(character not in valid_characters for character in name)
                or f"/{name}" in local_names
            ):
                continue
            normalized.append({**command, "name": name})

        self._server_commands = tuple(normalized)
        self._server_commands_context = context_id
        return self._server_commands

    def _sync_connection_status(self, status: str, url: str | None = None) -> None:
        widget = self.query_one("#connection-status", ConnectionStatus)
        widget.status = status
        if url is not None:
            widget.url = url
        widget.set_project_enabled(
            self.connected and bool(self.current_context) and "projects" in self.connector_features
        )
        widget.set_computer_use_state(self._computer_use.status_label, self._computer_use.status_detail)

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
        self._skill_palette_cache_key = None
        self._set_context_tab_project(self.current_context, self.current_project)
        self._sync_project_header()

    def _clear_project_state(self) -> None:
        self.project_list = []
        self.current_project = None
        self._skill_palette_cache_key = None
        self._set_context_tab_project(self.current_context, None)
        self._sync_project_header()

    def _sync_project_header(self) -> None:
        widget = self.query_one("#connection-status", ConnectionStatus)
        widget.set_project_state(
            self.current_project,
            enabled=self.connected and bool(self.current_context) and "projects" in self.connector_features,
        )

    def _is_project_menu_open(self) -> bool:
        return self._project_menu_popover is not None

    def _is_profile_menu_open(self) -> bool:
        return self._profile_menu_popover is not None

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

    async def _refresh_settings_snapshot(
        self,
        payload: Mapping[str, Any] | None = None,
        *,
        silent: bool = True,
    ) -> bool:
        return await state_sync.refresh_settings_snapshot(self, payload, silent=silent)

    async def _refresh_state_snapshot(self, *, silent: bool = True) -> None:
        await state_sync.refresh_state_snapshot(self, silent=silent)

    def _start_state_sync(self) -> None:
        state_sync.start_state_sync(self)

    def _stop_state_sync(self) -> None:
        state_sync.stop_state_sync(self)

    async def _open_project_menu(self) -> None:
        await self._hide_profile_menu()
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
        offset = self._project_menu_popover_offset(popover)
        if offset is not None:
            popover.absolute_offset = offset
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

    async def _open_profile_menu(self) -> None:
        await self._hide_project_menu()
        current_profile, options = await profile_commands.load_profile_menu_state(self, silent=False)
        if not options and "agent_editor" not in self.connector_features:
            self._show_notice("No agent profiles are available from Agent Zero Core.", error=True)
            return
        if self._profile_menu_popover is not None:
            await self._hide_profile_menu()

        popover = ProfileMenuPopover(
            options,
            current_profile=current_profile,
            can_edit="agent_editor" in self.connector_features,
            id="profile-menu-popover",
        )
        self._profile_menu_popover = popover
        offset = self._profile_menu_popover_offset(popover)
        if offset is not None:
            popover.absolute_offset = offset
        await self.mount(popover)
        self.call_after_refresh(self._position_profile_menu_popover)
        self.call_after_refresh(popover.focus_first_item)

    async def _hide_profile_menu(self) -> None:
        popover = self._profile_menu_popover
        self._profile_menu_popover = None
        if popover is None:
            return
        await popover.remove()

    def _project_menu_popover_offset(self, popover: ProjectMenuPopover | None = None) -> Offset | None:
        popover = popover or self._project_menu_popover
        if popover is None:
            return None

        try:
            status = self.query_one("#connection-status", ConnectionStatus)
        except NoMatches:
            return None

        screen_width = self.screen.size.width
        if screen_width <= 0:
            return None

        menu_width = popover.region.width or popover.outer_size.width or 38
        x = max(0, screen_width - menu_width - 2)
        y = max(0, status.region.y + status.region.height)
        return Offset(x, y)

    def _position_project_menu_popover(self) -> None:
        popover = self._project_menu_popover
        if popover is None:
            return

        offset = self._project_menu_popover_offset(popover)
        if offset is None:
            return

        popover.absolute_offset = offset
        popover.refresh(layout=True)

    def _profile_menu_popover_offset(self, popover: ProfileMenuPopover | None = None) -> Offset | None:
        popover = popover or self._profile_menu_popover
        if popover is None:
            return None

        screen_width = self.screen.size.width
        screen_height = self.screen.size.height
        if screen_width <= 0 or screen_height <= 0:
            return None

        try:
            composer = self.query_one("#message-input", ChatInput)
            input_region = composer.region
        except Exception:
            return Offset(2, 2)

        menu_width = popover.region.width or popover.outer_size.width or 44
        menu_height = popover.region.height or popover.outer_size.height or 10
        x = max(1, min(input_region.x, max(1, screen_width - menu_width - 1)))
        y_above = input_region.y - menu_height
        if y_above >= 1:
            y = y_above
        else:
            y = min(
                max(1, input_region.y + input_region.height),
                max(1, screen_height - menu_height - 1),
            )
        return Offset(x, y)

    def _position_profile_menu_popover(self) -> None:
        popover = self._profile_menu_popover
        if popover is None:
            return

        offset = self._profile_menu_popover_offset(popover)
        if offset is None:
            return

        popover.absolute_offset = offset
        popover.refresh(layout=True)

    async def _handle_project_menu_action(self, action: str, project_name_value: str | None = None) -> None:
        await self._hide_project_menu()
        await project_commands.handle_project_menu_action(
            self,
            action,
            project_name_value=project_name_value,
        )

    async def _dismiss_profile_menu(self) -> None:
        await self._hide_profile_menu()
        self._focus_message_input()

    async def _handle_profile_menu_action(self, action: str, profile_key: str = "") -> None:
        options = ()
        popover = self._profile_menu_popover
        if popover is not None:
            options = getattr(popover, "_profiles", ())
        await self._hide_profile_menu()
        if action == "select":
            await profile_commands.apply_profile_selection(self, profile_key, options=options)
        else:
            await profile_commands.handle_profile_menu_action(self, action, profile_key)
        self._focus_message_input()

    def on_key(self, event: events.Key) -> None:
        if event.key != "escape":
            return

        if self._is_profile_menu_open():
            event.prevent_default()
            event.stop()
            self.run_worker(self._dismiss_profile_menu(), exclusive=True, name="dismiss-profile-menu")
            return

        if self._is_project_menu_open():
            event.prevent_default()
            event.stop()
            self.run_worker(self._hide_project_menu(), exclusive=True, name="hide-project-menu")

    def on_resize(self, event: events.Resize) -> None:
        del event
        self._position_project_menu_popover()
        self._position_profile_menu_popover()

    def _splash_host(self) -> str:
        return splash_helpers.splash_host(self)

    def _normalize_host(self, host: str) -> str:
        return splash_helpers.normalize_host(host)

    def _saved_context_for_host(self, host: str) -> str:
        normalized_host = host.strip()
        if not normalized_host:
            return ""

        saved_host = self.config.last_context_host.strip().rstrip("/")
        if self._normalize_host(normalized_host).rstrip("/") != saved_host:
            return ""

        return self.config.last_context_id.strip()

    def _remember_context(self, context_id: str, *, host: str | None = None) -> None:
        normalized_context_id = context_id.strip()
        host_value = (host or self.client.base_url or self.config.instance_url).strip()
        if not normalized_context_id or not host_value:
            return

        normalized_host = self._normalize_host(host_value).rstrip("/")
        self.config.last_context_id = normalized_context_id
        self.config.last_context_host = normalized_host
        save_last_context(normalized_host, normalized_context_id)

    def _sync_context_tabs(self) -> None:
        try:
            self.query_one("#context-tabs", ContextTabs).set_tabs(
                self._context_tabs if self.connected else [],
                self.current_context,
                can_create=self.connected and "chat_create" in self.connector_features,
            )
        except Exception:
            pass

    def _clear_context_tabs(self) -> None:
        self._context_tabs = []
        self._sync_context_tabs()

    def _remember_context_tab(
        self,
        context_id: str,
        metadata: Mapping[str, object] | None = None,
        *,
        has_messages_hint: bool = False,
    ) -> None:
        normalized_context_id = str(context_id or "").strip()
        if not normalized_context_id:
            return

        updated_tabs: list[ContextTab] = []
        replaced = False
        for index, tab in enumerate(self._context_tabs, start=1):
            if tab.context_id != normalized_context_id:
                updated_tabs.append(tab)
                continue

            if metadata is None:
                updated_tabs.append(
                    ContextTab(
                        context_id=tab.context_id,
                        label=tab.label,
                        has_messages=tab.has_messages or has_messages_hint,
                        project_color=tab.project_color,
                    )
                )
            else:
                metadata_tab = context_tab_from_metadata(
                    metadata,
                    context_id=normalized_context_id,
                    index=index,
                    has_messages_hint=tab.has_messages or has_messages_hint,
                )
                updated_tabs.append(
                    ContextTab(
                        context_id=metadata_tab.context_id,
                        label=metadata_tab.label,
                        has_messages=metadata_tab.has_messages,
                        project_color=metadata_tab.project_color or tab.project_color,
                    )
                )
            replaced = True

        if not replaced:
            updated_tabs.append(
                context_tab_from_metadata(
                    metadata,
                    context_id=normalized_context_id,
                    index=len(updated_tabs) + 1,
                    has_messages_hint=has_messages_hint,
                )
            )

        self._context_tabs = updated_tabs
        self._sync_context_tabs()

    async def _refresh_context_tab_metadata(
        self,
        context_id: str,
        *,
        has_messages_hint: bool = False,
    ) -> None:
        if "chat_get" not in self.connector_features:
            return
        normalized_context_id = str(context_id or "").strip()
        if not normalized_context_id:
            return

        try:
            metadata = await self.client.get_chat(normalized_context_id)
        except Exception:
            return
        if isinstance(metadata, Mapping):
            self._remember_context_tab(
                normalized_context_id,
                metadata,
                has_messages_hint=has_messages_hint,
            )

    def _set_context_tab_project(
        self,
        context_id: str | None,
        project: Mapping[str, object] | None,
    ) -> None:
        normalized_context_id = str(context_id or "").strip()
        if not normalized_context_id:
            return

        color = project_color(normalize_project_summary(project) or project)
        changed = False
        updated_tabs: list[ContextTab] = []
        for tab in self._context_tabs:
            if tab.context_id == normalized_context_id and tab.project_color != color:
                updated_tabs.append(
                    ContextTab(
                        context_id=tab.context_id,
                        label=tab.label,
                        has_messages=tab.has_messages,
                        project_color=color,
                    )
                )
                changed = True
            else:
                updated_tabs.append(tab)

        if changed:
            self._context_tabs = updated_tabs
            self._sync_context_tabs()

    def _set_context_tab_has_messages(self, context_id: str | None = None) -> None:
        normalized_context_id = str(context_id or self.current_context or "").strip()
        if not normalized_context_id:
            return

        changed = False
        updated_tabs: list[ContextTab] = []
        for tab in self._context_tabs:
            if tab.context_id == normalized_context_id and not tab.has_messages:
                updated_tabs.append(
                    ContextTab(
                        context_id=tab.context_id,
                        label=tab.label,
                        has_messages=True,
                        project_color=tab.project_color,
                    )
                )
                changed = True
            else:
                updated_tabs.append(tab)

        if changed:
            self._context_tabs = updated_tabs
            self._sync_context_tabs()

    async def _switch_context_from_tab(self, context_id: str) -> None:
        normalized_context_id = str(context_id or "").strip()
        if not normalized_context_id:
            return
        if normalized_context_id == self.current_context:
            self._focus_message_input()
            return

        tab = next(
            (candidate for candidate in self._context_tabs if candidate.context_id == normalized_context_id),
            None,
        )
        await self._switch_context(
            normalized_context_id,
            has_messages_hint=bool(tab.has_messages if tab is not None else False),
        )
        self._focus_message_input()

    async def _close_context_tab(
        self,
        context_id: str,
        *,
        replacement_context_id: str = "",
    ) -> None:
        normalized_context_id = str(context_id or "").strip()
        if not normalized_context_id:
            return

        existing_index = next(
            (
                index
                for index, tab in enumerate(self._context_tabs)
                if tab.context_id == normalized_context_id
            ),
            None,
        )
        if existing_index is None:
            return
        if len(self._context_tabs) < 2:
            return

        was_current = normalized_context_id == self.current_context
        remaining_tabs = [
            tab for tab in self._context_tabs if tab.context_id != normalized_context_id
        ]
        self._context_tabs = remaining_tabs
        self._sync_context_tabs()

        if not was_current:
            self._focus_message_input()
            return

        replacement = str(replacement_context_id or "").strip()
        if not replacement or all(tab.context_id != replacement for tab in remaining_tabs):
            if remaining_tabs:
                replacement = remaining_tabs[min(existing_index, len(remaining_tabs) - 1)].context_id
            else:
                replacement = ""

        if replacement:
            await self._switch_context_from_tab(replacement)
            return

        self._focus_message_input()

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
        remember_host: bool | None = None,
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
            remember_host=remember_host,
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
        self._python_tty.set_write_enabled(value)
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

    def _apply_computer_use_status(self, label: str, detail: str) -> None:
        normalized = str(label or "").strip().lower()
        del detail
        self._sync_computer_use_status()
        if normalized and self.connected and self.client.connected:
            # Remote tool availability depends on this metadata; publish status
            # transitions immediately so users do not need to restart the CLI.
            self.run_worker(
                self._refresh_remote_tool_metadata(),
                exclusive=True,
                name="computer-use-metadata-refresh",
            )

    def _sync_computer_use_status(self) -> None:
        show_composer = self.connected and (
            self.current_context_has_messages or self._splash_state.stage == "ready"
        )
        metadata = self._computer_use.metadata()
        backend_id = str(metadata.get("backend_id") or "")
        backend_family = str(metadata.get("backend_family") or "")
        try:
            self.query_one("#connection-status", ConnectionStatus).set_computer_use_state(
                _computer_use_label(self._computer_use.status_label),
                self._computer_use.status_detail,
            )
        except Exception:
            pass

        try:
            banner = self.query_one("#computer-use-banner", ComputerUseBanner)
            banner.set_state(
                enabled=show_composer and self._computer_use.enabled,
                status=_computer_use_label(self._computer_use.status_label),
                backend_id=backend_id,
                backend_family=backend_family,
            )
        except Exception:
            return
        self._sync_composer_bar_spacing()

    def _set_activity(self, label: str, detail: str = "") -> None:
        self.query_one("#message-input", ChatInput).set_activity(label, detail)

    def _set_idle(self) -> None:
        self.query_one("#message-input", ChatInput).set_idle()
        try:
            self.query_one("#chat-log", ChatLog).dim_active_status()
        except Exception:
            pass

    def _set_message_queue(self, items: Iterable[Mapping[str, Any]] | None) -> None:
        normalized = [dict(item) for item in (items or []) if isinstance(item, Mapping)]
        self.message_queue = normalized
        try:
            self.query_one("#message-queue-bar", MessageQueueBar).set_items(normalized)
        except Exception:
            pass
        try:
            self.query_one("#message-input", ChatInput).set_queue_active(bool(normalized))
        except Exception:
            pass
        self._sync_composer_visibility()

    def _has_message_queue(self) -> bool:
        return bool(self.message_queue)

    def _set_goal(self, goal: Mapping[str, Any] | None) -> None:
        self.goal = dict(goal) if isinstance(goal, Mapping) else None
        try:
            bar = self.query_one("#goal-bar", GoalBar)
            bar.set_goal(self.goal)
        except Exception:
            pass
        self._sync_composer_bar_spacing()

    def _clear_goal_bar(self) -> None:
        self.goal = None
        try:
            self.query_one("#goal-bar", GoalBar).clear()
        except Exception:
            pass
        self._sync_composer_bar_spacing()

    def _sync_composer_bar_spacing(self) -> None:
        try:
            banner = self.query_one("#computer-use-banner", ComputerUseBanner)
            goal_bar = self.query_one("#goal-bar", GoalBar)
            model_bar = self.query_one("#model-switcher-bar", ModelSwitcherBar)
            goal_bar.set_class(bool(banner.display), "bar-following")
            model_bar.set_class(bool(banner.display or goal_bar.display), "bar-following")
        except Exception:
            pass

    async def _refresh_goal_bar(self, *, silent: bool = True) -> bool:
        if not self.connected or not self.current_context:
            self._clear_goal_bar()
            return False
        try:
            response = await self.client.goal_action("get", self.current_context)
        except Exception as exc:
            self._clear_goal_bar()
            if not silent:
                self._show_notice(f"Failed to refresh goal: {exc}", error=True)
            return False
        if not response.get("ok"):
            self._clear_goal_bar()
            if not silent and int(response.get("status_code") or 0) != 404:
                message = str(response.get("message") or "Goal state unavailable.")
                self._show_notice(message, error=True)
            return False

        before = state_sync.snapshot_signature(self.goal)
        goal = response.get("goal") if isinstance(response, Mapping) else None
        self._set_goal(goal if isinstance(goal, Mapping) else None)
        return before != state_sync.snapshot_signature(self.goal)

    def _focus_splash_primary(self) -> None:
        splash_helpers.focus_splash_primary(self)

    def _focus_message_input(self) -> None:
        splash_helpers.focus_message_input(self)

    def _show_notice(self, message: str, *, error: bool = False) -> None:
        splash_helpers.show_notice(self, message, error=error)

    def action_copy_visible_chat(self) -> None:
        try:
            text = self.query_one("#chat-log", ChatLog).copyable_text(visible_only=True)
        except Exception:
            text = ""

        if not text:
            self._show_notice("Nothing visible to copy.", error=True)
            return

        self.copy_to_clipboard(text)
        try:
            self.notify(
                "Copied visible transcript to clipboard.",
                title="Clipboard",
                severity="information",
                timeout=3,
                markup=False,
            )
        except Exception:
            self._show_notice("Copied visible transcript to clipboard.")

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
        self._set_context_tab_has_messages()
        self._sync_body_mode()

    def _show_chat_intro(self, log: ChatLog, category: str) -> None:
        if not self._chat_intro_pending or category not in {"user", "response"}:
            return
        log.ensure_intro_banner()
        self._chat_intro_pending = False

    def _clear_model_switcher(self) -> None:
        clear_model_switcher(self)
        self._model_switcher_signature_pending = ""
        self._model_switcher_signature_pending_retries = 0

    def _apply_model_switcher_state(self, payload: dict[str, Any]) -> None:
        from agent_zero_cli.model_config import apply_model_switcher_state
        allowed, state_kwargs = apply_model_switcher_state(payload)
        self._model_switcher_signature = state_sync.model_switcher_signature(payload)
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

    def _profile_availability(self) -> CommandAvailability:
        return availability.profile_availability(self)

    def _model_presets_availability(self) -> CommandAvailability:
        return availability.model_presets_availability(self)

    def _model_runtime_availability(self) -> CommandAvailability:
        return availability.model_runtime_availability(self)

    def _welcome_actions(self) -> tuple[SplashAction, ...]:
        return splash_helpers.welcome_actions(self)

    def _start_instance_discovery(self, *, auto_connect_single: bool = False) -> None:
        if not self._discover_instances:
            self._instance_discovery_generation += 1
            self._set_splash_state(
                discovered_instances=(),
                discovery_status="unavailable",
                discovery_detail="Docker discovery disabled by --no-docker-discovery.",
                selected_host_url="",
                manual_entry_expanded=True,
            )
            return

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
            resolved_selection = str(instances[0].url)
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

    def _start_cli_update_check(self) -> None:
        if self._cli_update_check_started or not self_update.update_check_enabled():
            return
        self._cli_update_check_started = True
        self.run_worker(
            self._check_for_cli_update(),
            name="cli-update-check",
            group="background",
            exit_on_error=False,
        )

    async def _check_for_cli_update(self) -> None:
        try:
            result = await asyncio.to_thread(self_update.check_for_update, __version__)
        except self_update.LatestReleaseError:
            return
        except Exception:
            return

        if result is None:
            return
        self._show_cli_update_notice(self_update.format_update_available_message(result))

    def _show_cli_update_notice(self, message: str) -> None:
        try:
            self.notify(
                message,
                title="a0 CLI update available",
                severity="information",
                timeout=12,
                markup=False,
            )
        except Exception:
            pass

        try:
            self._show_notice(message)
        except Exception:
            pass

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
        remember_host_flag: bool = False,
    ) -> None:
        await connection.begin_connection(
            self,
            host,
            username=username,
            password=password,
            remember_host_flag=remember_host_flag,
        )

    def _handle_context_snapshot(self, data: dict[str, Any]) -> None:
        event_handlers.handle_context_snapshot(self, data)

    def on_chat_log_history_requested(self, event: ChatLog.HistoryRequested) -> None:
        event.stop()
        context_id = str(self.current_context or "").strip()
        if not context_id:
            self.query_one("#chat-log", ChatLog).history_load_failed(event.before)
            return
        self.run_worker(
            self._load_older_history(context_id, event.before),
            exclusive=True,
            name="load-older-history",
        )

    def on_image_entry_load_requested(self, message: ImageEntry.LoadRequested) -> None:
        message.stop()
        context_id = self.current_context
        base_url = self.client.base_url
        client = self.client
        store = self.image_store
        epoch = self._image_load_epoch
        self.run_worker(
            self._load_image_entry(
                message.entry,
                message.reference,
                message.generation,
                context_id=context_id,
                base_url=base_url,
                client=client,
                store=store,
                epoch=epoch,
            ),
            exclusive=False,
            name="load-image-entry",
        )

    def _invalidate_image_loads(self) -> None:
        """Reject queued image workers from a superseded chat or host lifecycle."""
        self._image_load_epoch += 1

    def _image_load_is_current(
        self,
        entry: ImageEntry,
        reference: ImageReference,
        generation: int,
        *,
        context_id: str | None,
        base_url: str,
        client: A0Client,
        store: ImageStore,
        epoch: int,
    ) -> bool:
        return (
            entry.is_mounted
            and entry.generation == generation
            and context_id is not None
            and reference.context_id == context_id
            and self.current_context == context_id
            and self.client is client
            and self.client.base_url == base_url
            and self.image_store is store
            and self._image_load_epoch == epoch
        )

    async def _load_image_entry(
        self,
        entry: ImageEntry,
        reference: ImageReference,
        generation: int,
        *,
        context_id: str | None,
        base_url: str,
        client: A0Client,
        store: ImageStore,
        epoch: int,
    ) -> None:
        if not self._image_load_is_current(
            entry,
            reference,
            generation,
            context_id=context_id,
            base_url=base_url,
            client=client,
            store=store,
            epoch=epoch,
        ):
            return
        try:
            asset = await store.load(reference)
        except ImageUnavailableError as exc:
            if self._image_load_is_current(
                entry,
                reference,
                generation,
                context_id=context_id,
                base_url=base_url,
                client=client,
                store=store,
                epoch=epoch,
            ):
                entry.set_unavailable(generation, exc.reason)
            return
        except Exception:
            if self._image_load_is_current(
                entry,
                reference,
                generation,
                context_id=context_id,
                base_url=base_url,
                client=client,
                store=store,
                epoch=epoch,
            ):
                entry.set_unavailable(generation, "load failed")
            return

        if not self._image_load_is_current(
            entry,
            reference,
            generation,
            context_id=context_id,
            base_url=base_url,
            client=client,
            store=store,
            epoch=epoch,
        ):
            asset.close()
            return
        entry.set_asset(generation, asset)

    async def _load_older_history(self, context_id: str, before: int) -> None:
        try:
            await self.client.subscribe_context(context_id, history_before=before)
        except Exception as exc:
            if self.current_context == context_id:
                self.query_one("#chat-log", ChatLog).history_load_failed(before)
                self._show_notice(f"Could not load older chat messages: {exc}", error=True)

    def _handle_context_event(self, data: dict[str, Any]) -> None:
        event_handlers.handle_context_event(self, data)

    def _handle_context_complete(self, data: dict[str, Any]) -> None:
        event_handlers.handle_context_complete(self, data)

    def _handle_message_queue_updated(self, data: dict[str, Any]) -> None:
        event_handlers.handle_message_queue_updated(self, data)

    def _handle_connector_error(self, data: dict[str, Any]) -> None:
        event_handlers.handle_connector_error(self, data)

    def _handle_settings_updated(self, data: dict[str, Any]) -> None:
        self._skill_palette_cache_key = None
        self.run_worker(
            self._refresh_settings_snapshot(data),
            exclusive=True,
            name="settings-updated",
        )

    def _handle_file_op(self, data: dict[str, Any]) -> dict[str, Any]:
        return event_handlers.handle_file_op(self, data)

    async def _handle_exec_op(self, data: dict[str, Any]) -> dict[str, Any]:
        return await event_handlers.handle_exec_op(self, data)

    async def _handle_computer_use_op(self, data: dict[str, Any]) -> dict[str, Any]:
        return await event_handlers.handle_computer_use_op(self, data)

    def _computer_use_metadata(self) -> dict[str, Any]:
        return self._computer_use.metadata()

    async def _handle_browser_op(self, data: dict[str, Any]) -> dict[str, Any]:
        return await event_handlers.handle_browser_op(self, data)

    def _host_browser_metadata(self) -> dict[str, Any]:
        return self._host_browser.metadata()

    def _remote_file_metadata(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "write_enabled": self._remote_file_write_enabled,
            "mode": "read_write" if self._remote_file_write_enabled else "read_only",
        }

    def _remote_exec_metadata(self) -> dict[str, Any]:
        return {
            "enabled": self._remote_exec_enabled,
        }

    async def _refresh_remote_tool_metadata(self) -> bool:
        if not self.client.connected:
            self._remote_tool_metadata_error = ""
            return True
        try:
            hello = await self.client.send_hello(
                context_id=self.current_context,
                computer_use=self._computer_use_metadata(),
                host_browser=self._host_browser_metadata(),
                remote_files=self._remote_file_metadata(),
                remote_exec=self._remote_exec_metadata(),
            )
        except Exception as exc:
            self._remote_tool_metadata_error = str(exc).strip() or exc.__class__.__name__
            return False
        self._remote_tool_metadata_error = ""
        exec_config = hello.get("exec_config") if isinstance(hello, dict) else None
        self._python_tty.set_exec_config(exec_config)
        return True

    def _start_remote_tree_publisher(self) -> None:
        event_handlers.start_remote_tree_publisher(self)

    def _stop_remote_tree_publisher(self) -> None:
        event_handlers.stop_remote_tree_publisher(self)

    async def _remote_tree_publish_loop(self) -> None:
        await event_handlers.remote_tree_publish_loop(self)

    async def _publish_remote_tree_snapshot(self, *, force: bool = False) -> None:
        await event_handlers.publish_remote_tree_snapshot(self, force=force)

    def _command_worker_slug(self, value: str) -> str:
        slug = "".join(
            character if character.isalnum() else "-"
            for character in str(value or "").strip().lower()
        ).strip("-")
        while "--" in slug:
            slug = slug.replace("--", "-")
        return slug or "command"

    def _run_dispatch_command(self, text: str, *, worker_name: str):
        return self.run_worker(
            partial(self._dispatch_command, text),
            exclusive=True,
            name=worker_name,
        )

    def _run_skill_command(self, text: str, *, worker_name: str):
        return self.run_worker(
            partial(self._dispatch_skill_command, text),
            exclusive=True,
            name=worker_name,
        )

    def _skills_available(self) -> bool:
        return (
            self.connected
            and bool(self.current_context)
            and "skills_list" in self.connector_features
            and "skills_activate" in self.connector_features
        )

    def _skill_display_name(self, skill: Mapping[str, Any] | None) -> str:
        if not isinstance(skill, Mapping):
            return ""
        name = str(skill.get("name") or "").strip()
        if name:
            return name
        path = str(skill.get("path") or "").strip().replace("\\", "/").rstrip("/")
        return path.rsplit("/", maxsplit=1)[-1] if path else ""

    def _skill_help_text(self, skill: Mapping[str, Any] | None) -> str:
        if not isinstance(skill, Mapping):
            return "Activate this skill for the current chat."

        description = str(skill.get("description") or "").strip()
        origin = str(skill.get("origin") or "").strip()
        path = str(skill.get("path") or "").strip()
        details = " | ".join(part for part in (origin, path) if part)
        if description and details:
            return f"{description} ({details})"
        return description or details or "Activate this skill for the current chat."

    def _skill_search_text(self, skill: Mapping[str, Any] | None) -> str:
        if not isinstance(skill, Mapping):
            return ""
        return " ".join(
            str(skill.get(field) or "").strip()
            for field in ("name", "description", "path", "origin")
        ).casefold()

    async def _load_skill_palette_skills(self, *, force: bool = False) -> list[dict[str, Any]]:
        if not self._skills_available():
            return []

        context_id = self.current_context or ""
        project = project_name(self.current_project)
        cache_key = (context_id, project)
        if not force and self._skill_palette_cache_key == cache_key:
            return list(self._skill_palette_cache)

        raw_skills = await self.client.list_skills(
            context_id=context_id,
            project_name=project,
        )
        skills = [dict(skill) for skill in raw_skills if isinstance(skill, Mapping)]
        skills.sort(
            key=lambda skill: (
                str(skill.get("name") or "").casefold(),
                str(skill.get("path") or "").casefold(),
            )
        )
        self._skill_palette_cache = skills
        self._skill_palette_cache_key = cache_key
        return list(skills)

    def _format_skill_matches(self, matches: list[Mapping[str, Any]]) -> str:
        labels = [
            f"${name}"
            for skill in matches[:6]
            if (name := self._skill_display_name(skill))
        ]
        suffix = ", ..." if len(matches) > 6 else ""
        return ", ".join(labels) + suffix

    def _skill_command_parts(self, text: str) -> tuple[str, str] | None:
        raw = str(text or "").strip()
        if not raw.startswith("$"):
            return None
        if raw == "$":
            return "", ""
        if not is_raw_skill_command(raw):
            return None

        body = raw[1:].strip()
        token, _, remainder = body.partition(" ")
        return token.strip(), remainder.strip()

    def _trailing_palette_token(self, text: str, *, bare_markers: set[str]) -> str | None:
        raw = str(text or "")
        if not raw:
            return None

        stripped = raw.rstrip()
        if not stripped:
            return None

        token_start = len(stripped)
        while token_start > 0 and not stripped[token_start - 1].isspace():
            token_start -= 1

        token = stripped[token_start:]
        if len(stripped) != len(raw) and token not in bare_markers:
            return None
        return token

    def _skill_query(self, text: str) -> str | None:
        token = self._trailing_palette_token(text, bare_markers={"$"})
        if token is None or not token.startswith("$"):
            return None
        if token == "$":
            return "$"
        if not is_raw_skill_command(token):
            return None
        return token.lower()

    async def _resolve_skill_command(self, query: str) -> tuple[dict[str, Any] | None, str]:
        normalized_query = query.strip().lstrip("$").casefold()
        if not normalized_query:
            return None, "Choose a skill first."

        try:
            skills = await self._load_skill_palette_skills()
        except Exception as exc:
            return None, f"Failed to load skills: {exc}"

        if not skills:
            return None, "No skills are available for this chat."

        exact_matches: list[dict[str, Any]] = []
        prefix_matches: list[dict[str, Any]] = []
        contains_matches: list[dict[str, Any]] = []
        for skill in skills:
            name = self._skill_display_name(skill)
            path = str(skill.get("path") or "").strip().replace("\\", "/").rstrip("/")
            path_name = path.rsplit("/", maxsplit=1)[-1] if path else ""
            aliases = {
                alias.casefold()
                for alias in (name, path_name, path)
                if alias
            }
            if normalized_query in aliases:
                exact_matches.append(skill)
                continue
            if any(alias.startswith(normalized_query) for alias in aliases):
                prefix_matches.append(skill)
                continue
            if normalized_query in self._skill_search_text(skill):
                contains_matches.append(skill)

        if len(exact_matches) == 1:
            return exact_matches[0], ""
        if len(exact_matches) > 1:
            return None, f"Skill ${query} is ambiguous. Matches: {self._format_skill_matches(exact_matches)}"

        if len(prefix_matches) == 1:
            return prefix_matches[0], ""
        if len(prefix_matches) > 1:
            return None, f"Skill ${query} is ambiguous. Matches: {self._format_skill_matches(prefix_matches)}"

        if len(contains_matches) == 1:
            return contains_matches[0], ""
        if len(contains_matches) > 1:
            return None, f"Skill ${query} is ambiguous. Matches: {self._format_skill_matches(contains_matches)}"

        return None, f"Unknown skill: ${query}. Type $ to browse available skills."

    async def _activate_skill(self, skill: Mapping[str, Any]) -> bool:
        if not self.current_context:
            self._show_notice("Open or create a chat context before invoking a skill.", error=True)
            return False
        if not self._skills_available():
            self._show_notice("Skill invocation is unavailable on this Agent Zero instance.", error=True)
            return False

        entry = {
            "name": str(skill.get("name") or "").strip(),
            "path": str(skill.get("path") or "").strip(),
        }
        try:
            payload = await self.client.activate_skill(self.current_context, entry)
        except Exception as exc:
            self._show_notice(f"Failed to activate skill: {exc}", error=True)
            return False

        if not payload.get("ok"):
            message = str(payload.get("message") or payload.get("error") or "Failed to activate skill.")
            self._show_notice(message, error=True)
            return False

        name = self._skill_display_name(payload.get("skill") if isinstance(payload, Mapping) else skill)
        if not name:
            name = self._skill_display_name(skill) or "skill"
        self._show_notice(f"Skill activated for this chat: {name}.")
        self._sync_ready_actions()
        return True

    async def _dispatch_skill_command(
        self,
        text: str,
        *,
        attachments: list[Any] | None = None,
        input_widget: Any = None,
    ) -> bool:
        parsed = self._skill_command_parts(text)
        if parsed is None:
            return False

        query, remainder = parsed
        if not query:
            if self._skills_available():
                self._open_command_palette(initial_query="$", from_skill=True)
            else:
                self._show_notice("Open a chat with skill support before typing $.", error=True)
            return True

        skill, error_message = await self._resolve_skill_command(query)
        if skill is None:
            self._show_notice(error_message or f"Unknown skill: ${query}.", error=True)
            return True

        if not await self._activate_skill(skill):
            return True

        if remainder:
            if input_widget is None:
                try:
                    input_widget = self.query_one("#message-input", ChatInput)
                except Exception:
                    input_widget = None
            await self._send_chat_text(
                remainder,
                raw_text=remainder,
                attachments=attachments or [],
                input_widget=input_widget,
            )
        return True

    def _slash_query(self, text: str) -> str | None:
        token = self._trailing_palette_token(text, bare_markers={"/"})
        if token is None or not token.startswith("/"):
            return None
        return token.lower()

    async def _dispatch_command(self, text: str) -> None:
        token = text.split()[0].lower()
        spec = self._command_lookup.get(token)
        if spec is None:
            server_commands = await self._load_server_commands(force=True)
            if token not in {f"/{command['name']}" for command in server_commands}:
                self._show_notice(f"Unknown command: {token}. Type /help for available commands.", error=True)
                return
            await self._send_chat_text(text, raw_text=text, attachments=[])
            return

        availability = spec.availability(self)
        if not availability.available:
            self._show_notice(availability.reason or f"{token} is unavailable right now.", error=True)
            return

        if token == "/profile":
            _, _, query = text.partition(" ")
            await profile_commands.cmd_profile(self, query=query.strip())
            self._sync_ready_actions()
            return

        if token in {"/project", "/projects"}:
            _, _, query = text.partition(" ")
            await project_commands.cmd_project(self, query=query.strip())
            self._sync_ready_actions()
            return

        if token == "/browser":
            _, _, query = text.partition(" ")
            await browser_commands.cmd_browser(self, query=query.strip())
            self._sync_ready_actions()
            return

        if token in {"/attach", "/image", "/img"}:
            _, _, query = text.partition(" ")
            await chat_commands.cmd_attach(self, query=query.strip())
            self._sync_ready_actions()
            return

        if token in {"/computer-use", "/computer", "/cu"}:
            _, _, query = text.partition(" ")
            await self._cmd_computer_use(query=query.strip())
            self._sync_ready_actions()
            return

        if token == "/queue":
            _, _, query = text.partition(" ")
            await chat_commands.cmd_queue(self, query=query.strip())
            self._sync_ready_actions()
            return

        if token == "/goal":
            _, _, query = text.partition(" ")
            await goal_commands.cmd_goal(self, query=query.strip())
            self._sync_ready_actions()
            return

        if token == "/send":
            await chat_commands.cmd_queue_send(self)
            self._sync_ready_actions()
            return

        if token == "/chats":
            parsed = self._parse_chats_command(text)
            if parsed is None:
                self._show_notice(
                    "Usage: /chats [--project|--all-projects] [--sort=updated|created|name]",
                    error=True,
                )
                return

            sort_by, active_project_only = parsed
            await chat_commands.cmd_chats(
                self,
                sort_by=sort_by,
                active_project_only=active_project_only,
            )
            self._sync_ready_actions()
            return

        await spec.handler(self)
        self._sync_ready_actions()

    def _parse_chats_command(self, text: str) -> tuple[str, bool] | None:
        sort_by = "updated"
        active_project_only = False

        tokens = text.split()[1:]
        index = 0
        while index < len(tokens):
            token = tokens[index].lower()

            if token in {"--project", "--active-project", "-p"}:
                active_project_only = True
                index += 1
                continue

            if token in {"--all-projects", "--all", "-a"}:
                active_project_only = False
                index += 1
                continue

            if token.startswith("--sort="):
                value = token.split("=", maxsplit=1)[1]
                if value not in {"updated", "created", "name"}:
                    return None
                sort_by = value
                index += 1
                continue

            if token in {"--sort", "-s"}:
                if index + 1 >= len(tokens):
                    return None
                value = tokens[index + 1].lower()
                if value not in {"updated", "created", "name"}:
                    return None
                sort_by = value
                index += 2
                continue

            if token in {"updated", "created", "name"}:
                sort_by = token
                index += 1
                continue

            return None

        return sort_by, active_project_only

    async def _send_chat_text(
        self,
        text: str,
        *,
        raw_text: str,
        attachments: list[Any],
        input_widget: Any = None,
    ) -> None:
        attachment_paths = [attachment.path for attachment in attachments]
        if not self.current_context:
            self._show_notice("No active chat context.", error=True)
            return

        await self._refresh_remote_tool_metadata()
        await self._publish_remote_tree_snapshot(force=True)

        if "message_queue" in self.connector_features and (self.agent_active or self._has_message_queue()):
            try:
                response = await self.client.add_message_to_queue(
                    text,
                    self.current_context,
                    attachments=attachment_paths,
                )
            except Exception as exc:
                if input_widget is not None:
                    input_widget.value = raw_text
                    input_widget.set_attachments(attachments)
                self._focus_message_input()
                self._show_notice(f"Error queuing message: {exc}", error=True)
                self._sync_ready_actions()
                return

            queue_items = response.get("message_queue") if isinstance(response, Mapping) else None
            if isinstance(queue_items, list):
                self._set_message_queue(queue_items)
            self._show_notice("Message added to queue.")
            self._sync_ready_actions()
            return

        previous_agent_active = self.agent_active
        previous_pause_latched = self._pause_latched
        previous_context_has_messages = self.current_context_has_messages
        previous_response_delivered = self._response_delivered
        previous_context_run_complete = self._context_run_complete

        self._set_pause_latched(False)
        self._mark_context_has_messages()
        self._response_delivered = False
        self._context_run_complete = False
        self.agent_active = True
        self._sync_ready_actions()

        try:
            await self.client.send_message(text, self.current_context, attachments=attachment_paths)
        except Exception as exc:
            self.current_context_has_messages = previous_context_has_messages
            self._response_delivered = previous_response_delivered
            self._context_run_complete = previous_context_run_complete
            self.agent_active = previous_agent_active
            self._set_pause_latched(previous_pause_latched)
            self._sync_body_mode()
            if input_widget is not None:
                input_widget.value = raw_text
                input_widget.set_attachments(attachments)
            self._focus_message_input()
            self._show_notice(f"Error sending message: {exc}", error=True)
            self._sync_ready_actions()

    async def on_chat_input_submitted(self, event: ChatInput.Submitted) -> None:
        raw_text = event.value
        text = raw_text.strip()
        attachments = list(getattr(event, "attachments", []) or [])
        attachment_paths = [attachment.path for attachment in attachments]
        if not text and not attachment_paths:
            if self._has_message_queue():
                await chat_commands.cmd_queue_send(self)
            self._slash_palette_query = None
            return

        if text.startswith("/"):
            token = text.split(maxsplit=1)[0].strip().lower().lstrip("/") or "command"
            worker_name = f"slash-{token.replace('/', '-')}"
            self._run_dispatch_command(text, worker_name=worker_name)
            return

        if text == "$" or is_raw_skill_command(text):
            handled = await self._dispatch_skill_command(
                text,
                attachments=attachments,
                input_widget=event.input,
            )
            if handled:
                return

        await self._send_chat_text(
            text,
            raw_text=raw_text,
            attachments=attachments,
            input_widget=event.input,
        )

    def on_chat_input_value_changed(self, event: ChatInput.ValueChanged) -> None:
        selection = getattr(event.input, "selection", None)
        document = getattr(event.input, "document", None)
        if selection is not None and document is not None and selection.start == selection.end:
            cursor_index = document.get_index_from_location(selection.end)
            reference = reference_query_at_cursor(event.value, cursor_index)
            if reference is not None:
                query, start, end = reference
                self._reference_palette_range = (
                    document.get_location_from_index(start),
                    document.get_location_from_index(end),
                )
                self._open_command_palette(initial_query=query)
                return

        skill_query = self._skill_query(event.value)
        if skill_query is not None:
            if self._skills_available():
                self._open_command_palette(initial_query=skill_query, from_skill=True)
            return

        query = self._slash_query(event.value)
        if query is None:
            return
        if query in _HIDDEN_SLASH_COMMANDS:
            return
        if query != "/" and any(command.startswith(query) for command in _NO_AUTO_SLASH_PALETTE_COMMANDS):
            return

        self._open_command_palette(initial_query=query, from_slash=True)

    def on_command_palette_closed(self, event: CommandPalette.Closed) -> None:
        reference_range = self._reference_palette_range
        self._reference_palette_range = None
        if reference_range is not None:
            if getattr(event, "option_selected", False):
                return
            try:
                input_widget = self.query_one("#message-input", ChatInput)
                input_widget.replace("", *reference_range)
            except Exception:
                pass
            return

        query = self._slash_palette_query
        self._slash_palette_query = None
        if query is None:
            return

        try:
            input_widget = self.query_one("#message-input", ChatInput)
        except Exception:
            return

        value = str(input_widget.value or "")
        if value.strip().lower() == query:
            input_widget.value = ""
            return

        stripped = value.rstrip()
        if not stripped.lower().endswith(query):
            return

        token_start = len(stripped) - len(query)
        if token_start > 0 and not stripped[token_start - 1].isspace():
            return

        input_widget.value = value[:token_start]

    def _insert_reference(
        self,
        reference: str,
        trigger_range: tuple[tuple[int, int], tuple[int, int]] | None,
    ) -> None:
        if not trigger_range:
            return
        input_widget = self.query_one("#message-input", ChatInput)
        end_index = input_widget.document.get_index_from_location(trigger_range[1])
        separator = "" if input_widget.value[end_index : end_index + 1].isspace() else " "
        result = input_widget.replace(f"{reference}{separator}", *trigger_range)
        input_widget.move_cursor(result.end_location)
        input_widget.focus()

    async def on_model_switcher_bar_preset_changed(self, event: ModelSwitcherBar.PresetChanged) -> None:
        await self._set_model_preset(event.value or None, bar=event.bar)

    def on_goal_bar_update_requested(self, event: GoalBar.UpdateRequested) -> None:
        event.stop()
        objective = str((self.goal or {}).get("objective") or "").strip()
        input_widget = self.query_one("#message-input", ChatInput)
        input_widget.value = f"/goal update {objective}".rstrip()
        input_widget.focus()

    def on_goal_bar_pause_resume_requested(self, event: GoalBar.PauseResumeRequested) -> None:
        event.stop()
        action = "pause" if str((self.goal or {}).get("status") or "") == "active" else "resume"
        self.run_worker(
            goal_commands.cmd_goal(self, query=action),
            exclusive=True,
            name=f"goal-{action}",
        )

    def on_goal_bar_delete_requested(self, event: GoalBar.DeleteRequested) -> None:
        event.stop()
        self.run_worker(
            goal_commands.cmd_goal(self, query="delete"),
            exclusive=True,
            name="goal-delete",
        )

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
                remember_host_flag=event.remember_host,
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
            remember_host=event.remember_host,
        )

    def on_splash_view_remember_host_changed(self, event: SplashView.RememberHostChanged) -> None:
        if self._splash_state.remember_host == event.remember_host:
            return
        self._set_splash_state(remember_host=event.remember_host)

    async def on_splash_view_action_requested(self, event: SplashView.ActionRequested) -> None:
        if event.action == "back":
            await connection._cancel_websocket_recovery(self)
            self._set_splash_stage(
                "host",
                message="",
                detail="",
                host=self._splash_host(),
                username=self._splash_state.username,
                password="",
                remember_host=self._splash_state.remember_host,
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
                    remember_host_flag=self._splash_state.remember_host,
                ),
                exclusive=True,
                name="splash-retry",
            )
            return

        if not event.action.startswith("/"):
            return

        worker_name = f"splash-{event.action.lstrip('/').replace('/', '-')}"
        self._run_dispatch_command(event.action, worker_name=worker_name)

    def on_connection_status_project_requested(self, event: ConnectionStatus.ProjectRequested) -> None:
        del event
        self.run_worker(self._toggle_project_menu(), exclusive=True, name="toggle-project-menu")

    def on_context_tabs_context_selected(self, event: ContextTabs.ContextSelected) -> None:
        event.stop()
        context_id = event.context_id.strip()
        if not context_id:
            return
        self.run_worker(
            self._switch_context_from_tab(context_id),
            exclusive=True,
            name=f"context-tab-{context_id}",
        )

    def on_context_tabs_new_requested(self, event: ContextTabs.NewRequested) -> None:
        event.stop()
        self.run_worker(self._cmd_new(), exclusive=True, name="context-tab-new")

    def on_context_tabs_close_requested(self, event: ContextTabs.CloseRequested) -> None:
        event.stop()
        context_id = event.context_id.strip()
        if not context_id:
            return
        self.run_worker(
            self._close_context_tab(
                context_id,
                replacement_context_id=event.replacement_context_id,
            ),
            exclusive=True,
            name=f"context-tab-close-{context_id}",
        )

    def on_project_menu_popover_dismiss_requested(self, event: ProjectMenuPopover.DismissRequested) -> None:
        del event
        self.run_worker(self._hide_project_menu(), exclusive=True, name="hide-project-menu")

    def on_project_menu_item_selected(self, event: ProjectMenuItem.Selected) -> None:
        self.run_worker(
            self._handle_project_menu_action(event.action, event.project_name),
            exclusive=True,
            name=f"project-menu-{event.action}",
        )

    def on_profile_menu_popover_dismiss_requested(self, event: ProfileMenuPopover.DismissRequested) -> None:
        del event
        self.run_worker(self._dismiss_profile_menu(), exclusive=True, name="dismiss-profile-menu")

    def on_profile_menu_item_selected(self, event: ProfileMenuItem.Selected) -> None:
        self.run_worker(
            self._handle_profile_menu_action(event.action, event.profile_key),
            exclusive=True,
            name=f"profile-menu-{event.action}-{event.profile_key}",
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

    async def _cmd_profile(self) -> None:
        await profile_commands.cmd_profile(self)

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

    async def _set_computer_use_enabled(self, enabled: bool) -> None:
        was_enabled = self._computer_use.enabled
        if enabled:
            self._computer_use.set_trust_mode("allow")
        self._computer_use.set_enabled(enabled)
        rearm_result: dict[str, Any] | None = None
        if enabled:
            self._computer_use.mark_approval_pending()
            rearm_result = await self._computer_use.rearm(context_id=self.current_context)
        if not enabled and was_enabled:
            await self._computer_use.disconnect()
        synced = await self._refresh_remote_tool_metadata()
        state = "enabled" if self._computer_use.enabled else "disabled"
        if synced:
            activation_error = None
            if rearm_result is not None and not bool(rearm_result.get("ok")):
                activation_error = rearm_result
            if activation_error is not None:
                message = (
                    _computer_use_result_message(activation_error)
                    or "Approve the platform permission prompt, then run /computer-use on again."
                )
                reason = (
                    "platform permission is not armed"
                    if _computer_use_result_code(activation_error) == "COMPUTER_USE_REARM_REQUIRED"
                    else "activation did not complete"
                )
                self._show_notice(
                    f"Computer use {state} locally, but {reason}: {message}",
                    error=True,
                )
            else:
                status = str(self._computer_use.status_label or "").strip().lower()
                if self._computer_use.enabled and status == "active":
                    self._show_notice("Computer use is active for this CLI session.")
                else:
                    self._show_notice(
                        f"Computer use {state} for this CLI session "
                        f"({_computer_use_mode_label(self._computer_use.trust_mode)})."
                    )
        else:
            self._show_notice(
                "Computer use changed locally, but Agent Zero did not acknowledge "
                f"the update: {self._remote_tool_metadata_error}",
                error=True,
            )
        self._sync_computer_use_status()

    async def _cmd_computer_use(self, *, query: str = "") -> None:
        tokens = query.split()
        action = "-".join(token.strip().lower().replace("_", "-") for token in tokens) if tokens else "status"
        if action in {"status", "state", ""}:
            state = "enabled" if self._computer_use.enabled else "disabled"
            status = _computer_use_label(self._computer_use.status_label) or "Unknown"
            detail = str(self._computer_use.status_detail or "").strip()
            detail_suffix = f": {detail}" if detail else ""
            self._show_notice(
                "Computer use is "
                f"{state} for this CLI session "
                f"({_computer_use_mode_label(self._computer_use.trust_mode)}); "
                f"status: {status}{detail_suffix}. "
                "Use /computer-use on|off."
            )
            return
        if action in {"on", "enable", "enabled", "true", "yes", "1"}:
            await self._set_computer_use_enabled(True)
            return
        if action in {"off", "disable", "disabled", "false", "no", "0"}:
            await self._set_computer_use_enabled(False)
            return
        self._show_notice(
            "Usage: /computer-use on|off|status",
            error=True,
        )

    async def _cmd_copy_visible_chat(self) -> None:
        self.action_copy_visible_chat()

    async def _disconnect_and_exit(self) -> None:
        await connection.disconnect_and_exit(self)

    async def _disconnect_to_login(self) -> None:
        await connection.disconnect_to_login(self)

    async def action_clear_chat(self) -> None:
        await self._cmd_clear()

    async def action_toggle_remote_file_mode(self) -> None:
        self._set_remote_file_write_enabled(not self._remote_file_write_enabled)
        synced = await self._refresh_remote_tool_metadata()
        mode = "Read&Write" if self._remote_file_write_enabled else "Read only"
        if synced:
            self._show_notice(f"Local access: {mode}.")
        else:
            self._show_notice(
                "Local access changed locally, but Agent Zero did not acknowledge "
                f"the update: {self._remote_tool_metadata_error}",
                error=True,
            )

    async def action_toggle_remote_exec(self) -> None:
        next_enabled = not self._remote_exec_enabled
        self._set_remote_exec_enabled(next_enabled)
        if not next_enabled:
            await self._python_tty.close()
        synced = await self._refresh_remote_tool_metadata()
        mode = "enabled" if self._remote_exec_enabled else "disabled"
        if synced:
            self._show_notice(f"Remote execution {mode} for this CLI session.")
        else:
            self._show_notice(
                "Remote execution changed locally, but Agent Zero did not acknowledge "
                f"the update: {self._remote_tool_metadata_error}",
                error=True,
            )

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
