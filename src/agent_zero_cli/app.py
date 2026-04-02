from __future__ import annotations

import os
from typing import Any

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.reactive import reactive
from textual.widgets import Footer, RichLog, Static
from rich.markdown import Markdown

from agent_zero_cli.client import A0Client, A0ConnectorPluginMissingError
from agent_zero_cli.config import CLIConfig, load_config, save_env
from agent_zero_cli.screens.chat_list import ChatListScreen
from agent_zero_cli.screens.host_input import HostInputScreen
from agent_zero_cli.screens.login import LoginResult, LoginScreen
from agent_zero_cli.widgets.chat_input import ChatInput


_EVENT_CATEGORY: dict[str, str] = {
    "user_message": "user",
    "assistant_message": "response",
    "assistant_delta": "response",
    "tool_start": "tool",
    "tool_output": "tool",
    "tool_end": "tool",
    "code_start": "code",
    "code_output": "code",
    "warning": "warning",
    "error": "error",
    "status": "info",
    "message_complete": "info",
    "context_updated": "info",
}

# Human-readable status labels shown in the status bar while agent is active.
# Events not listed here produce no status update (bar keeps its last value).
_STATUS_LABEL: dict[str, str] = {
    "user_message": "Sending...",
    "assistant_message": "Responding...",
    "assistant_delta": "Responding...",
    "tool_start": "Using tool...",
    "tool_output": "Using tool...",
    "tool_end": "Using tool...",
    "code_start": "Running code...",
    "code_output": "Running code...",
    "status": "Thinking...",
    "context_updated": "Updating memory...",
}

_PROTOCOL_VERSION = "a0-connector.v1"
_WS_NAMESPACE = "/ws"
_WS_HANDLER = "plugins/a0_connector/ws_connector"


class AgentZeroCLI(App):
    """Agent Zero CLI - Terminal Chat Interface."""

    CSS_PATH = "styles/app.tcss"
    TITLE = "Agent Zero CLI"
    BINDINGS = [
        Binding("ctrl+c", "quit", "Exit", show=True),
        Binding("f5", "clear_chat", "Clear", show=True, priority=True),
        Binding("f6", "list_chats", "Chats", show=True, priority=True),
        Binding("f7", "nudge_agent", "Nudge", show=True, priority=True),
        Binding("f8", "pause_agent", "Pause", show=True, priority=True),
    ]

    connected = reactive(False)
    agent_active = reactive(False)

    def __init__(self, config: CLIConfig | None = None) -> None:
        super().__init__()
        self.config = config or load_config()
        self.client = A0Client(
            self.config.instance_url or HostInputScreen.DEFAULT_HOST,
            api_key=self.config.api_key,
        )
        self.current_context: str | None = None

    def compose(self) -> ComposeResult:
        yield RichLog(id="chat-log", wrap=True, highlight=True, markup=True)
        yield Static("Waiting for input", id="status-bar")
        yield ChatInput(id="message-input")
        yield Footer()

    async def on_mount(self) -> None:
        input_widget = self.query_one("#message-input", ChatInput)
        input_widget.disabled = True
        input_widget.focus()
        self.run_worker(self._startup(), exclusive=True, name="startup")

    def _set_status(self, text: str) -> None:
        """Update the status bar with a (possibly truncated) message."""
        bar = self.query_one("#status-bar", Static)
        # Truncate to terminal width minus a small margin so it never wraps
        width = self.size.width or 80
        max_len = max(10, width - 4)
        display = text if len(text) <= max_len else text[: max_len - 1] + "…"
        bar.update(display)

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    async def _startup(self) -> None:
        log = self.query_one("#chat-log", RichLog)
        input_widget = self.query_one("#message-input", ChatInput)

        # --- Step 1: Resolve host URL ---
        if not self.config.instance_url:
            host_url = await self.push_screen_wait(HostInputScreen())
            if not host_url:
                host_url = HostInputScreen.DEFAULT_HOST
            self.config.instance_url = host_url
            self.client.base_url = host_url.rstrip("/")

        log.write("[dim]Connecting to Agent Zero...[/dim]")

        # --- Step 2: Probe capabilities ---
        capabilities, plugin_missing = await self._fetch_capabilities(log)
        if capabilities is None:
            if not plugin_missing:
                log.write(f"[red]No Agent Zero instance found at {self.config.instance_url}[/red]")
                log.write(
                    "[dim]Check the URL, firewall, and TLS. If the server is up, install the "
                    "a0_connector plugin (see README).[/dim]"
                )
            input_widget.disabled = True
            return

        try:
            self._validate_capabilities(capabilities)
        except ValueError as exc:
            log.write(f"[red]{exc}[/red]")
            input_widget.disabled = True
            return

        # --- Step 3: Resolve API key ---
        auth_modes = capabilities.get("auth") or []

        if not self.config.api_key and "login" in auth_modes:
            login_result = await self.push_screen_wait(LoginScreen(self.client))
            if login_result:
                if not isinstance(login_result, LoginResult):
                    raise TypeError(f"Unexpected login result: {login_result!r}")
                self.config.api_key = login_result.api_key
                self.client.api_key = login_result.api_key
                if login_result.save_credentials:
                    save_env("AGENT_ZERO_HOST", self.config.instance_url)
                    save_env("AGENT_ZERO_API_KEY", login_result.api_key)
            else:
                log.write("[red]Login cancelled.[/red]")
                input_widget.disabled = True
                return

        if not self.config.api_key and "api_key" in auth_modes:
            log.write("[red]No API key available. Set AGENT_ZERO_API_KEY or log in.[/red]")
            input_widget.disabled = True
            return

        # --- Step 4: Verify API key ---
        if self.config.api_key:
            try:
                api_key_ok = await self.client.verify_api_key()
            except Exception as exc:
                log.write(f"[red]API check failed: {exc}[/red]")
                input_widget.disabled = True
                return
            if not api_key_ok:
                log.write("[red]API key rejected by the connector endpoint.[/red]")
                input_widget.disabled = True
                return

        # --- Step 5: Wire callbacks and connect ---
        self.client.on_connect = lambda: self._run_on_ui(self._set_connected, True)
        self.client.on_disconnect = lambda: self._run_on_ui(self._set_connected, False)
        self.client.on_context_snapshot = lambda data: self._run_on_ui(
            self._handle_context_snapshot, data
        )
        self.client.on_context_event = lambda data: self._run_on_ui(
            self._handle_context_event, data
        )
        self.client.on_context_complete = lambda data: self._run_on_ui(
            self._handle_context_complete, data
        )
        self.client.on_error = lambda data: self._run_on_ui(
            self._handle_connector_error, data
        )
        self.client.on_file_op = self._handle_file_op

        try:
            await self.client.connect_websocket()
            await self.client.send_hello()
        except Exception as exc:
            log.write(f"[red]WebSocket connection failed: {exc}[/red]")
            input_widget.disabled = True
            return

        try:
            self.current_context = await self.client.create_chat()
            await self.client.subscribe_context(self.current_context)
        except Exception as exc:
            log.write(f"[red]Failed to create the initial chat: {exc}[/red]")
            input_widget.disabled = True
            return

        log.write("[green]Connected to Agent Zero.[/green]")
        input_widget.disabled = False

    async def _fetch_capabilities(
        self, log: RichLog
    ) -> tuple[dict[str, Any] | None, bool]:
        """Return (capabilities, plugin_missing). plugin_missing is True on HTTP 404 for /capabilities."""
        try:
            return await self.client.fetch_capabilities(), False
        except A0ConnectorPluginMissingError as exc:
            for line in str(exc).splitlines():
                if line.strip():
                    log.write(f"[yellow]{line}[/yellow]")
            return None, True
        except Exception as exc:
            log.write(f"[dim]Capabilities probe failed: {exc}[/dim]")
            return None, False

    def _validate_capabilities(self, capabilities: dict[str, Any]) -> None:
        protocol = capabilities.get("protocol")
        namespace = capabilities.get("websocket_namespace")
        handlers = capabilities.get("websocket_handlers") or []
        auth_modes = capabilities.get("auth") or []

        if protocol != _PROTOCOL_VERSION:
            raise ValueError(
                f"Unsupported connector protocol: expected {_PROTOCOL_VERSION}, got {protocol!r}"
            )
        if namespace != _WS_NAMESPACE:
            raise ValueError(
                f"Unsupported WebSocket namespace: expected {_WS_NAMESPACE}, got {namespace!r}"
            )
        if not isinstance(handlers, list) or _WS_HANDLER not in handlers:
            raise ValueError(
                f"Connector handler activation is missing {_WS_HANDLER!r} in capabilities"
            )
        if "api_key" not in auth_modes:
            raise ValueError("Connector capabilities do not advertise API-key auth")

    # ------------------------------------------------------------------
    # UI helpers
    # ------------------------------------------------------------------

    def _set_connected(self, value: bool) -> None:
        self.connected = value

    def _run_on_ui(self, func: Any, *args: Any) -> None:
        app_loop = getattr(self, "loop", None)
        if app_loop is None:
            func(*args)
        else:
            app_loop.call_soon_threadsafe(func, *args)

    # ------------------------------------------------------------------
    # Connector event handlers
    # ------------------------------------------------------------------

    def _handle_context_snapshot(self, data: dict[str, Any]) -> None:
        """Handle a batch of historical events from subscribe_context."""
        context_id = data.get("context_id", "")
        if context_id != self.current_context:
            return

        log = self.query_one("#chat-log", RichLog)
        events = data.get("events", [])
        for event in events:
            self._render_connector_event(log, event)

    def _handle_context_event(self, data: dict[str, Any]) -> None:
        """Handle a single streaming event from the agent."""
        context_id = data.get("context_id", "")
        if context_id != self.current_context:
            return

        self.agent_active = True
        input_widget = self.query_one("#message-input", ChatInput)
        input_widget.disabled = True

        # Update status bar with a clean label for this event type
        event_type = data.get("event", "")
        label = _STATUS_LABEL.get(event_type)
        if label:
            self._set_status(label)

        log = self.query_one("#chat-log", RichLog)
        self._render_connector_event(log, data)

    def _handle_context_complete(self, data: dict[str, Any]) -> None:
        """Handle agent completion -- re-enable input."""
        context_id = data.get("context_id", "")
        if context_id != self.current_context:
            return

        self.agent_active = False
        input_widget = self.query_one("#message-input", ChatInput)
        input_widget.disabled = False
        input_widget.focus()
        self._set_status("Waiting for input")

    def _handle_connector_error(self, data: dict[str, Any]) -> None:
        """Handle error events from the connector."""
        log = self.query_one("#chat-log", RichLog)
        code = data.get("code", "ERROR")
        message = data.get("message", "Unknown error")
        log.write(f"[red]{code}: {message}[/red]")

    def _handle_file_op(self, data: dict[str, Any]) -> dict[str, Any]:
        """Handle file operation requests from text_editor_remote."""
        op_id = data.get("op_id", "")
        op = data.get("op", "")
        path = data.get("path", "")

        try:
            if op == "read":
                return self._file_op_read(op_id, path, data)
            elif op == "write":
                return self._file_op_write(op_id, path, data)
            elif op == "patch":
                return self._file_op_patch(op_id, path, data)
            else:
                return {"op_id": op_id, "ok": False, "error": f"Unknown op: {op}"}
        except Exception as e:
            return {"op_id": op_id, "ok": False, "error": str(e)}

    def _file_op_read(self, op_id: str, path: str, data: dict) -> dict:
        """Read a file from the local filesystem."""
        line_from = data.get("line_from")
        line_to = data.get("line_to")

        if not os.path.isfile(path):
            return {"op_id": op_id, "ok": False, "error": f"File not found: {path}"}

        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()

        total = len(lines)
        start = (line_from - 1) if line_from and line_from > 0 else 0
        end = line_to if line_to and line_to <= total else total
        selected = lines[start:end]

        content = ""
        for i, line in enumerate(selected, start=start + 1):
            content += f"{i:>4} | {line}"

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

    def _file_op_write(self, op_id: str, path: str, data: dict) -> dict:
        """Write content to a file on the local filesystem."""
        content = data.get("content", "")
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return {"op_id": op_id, "ok": True, "result": {"path": path}}

    def _file_op_patch(self, op_id: str, path: str, data: dict) -> dict:
        """Apply line-based edits to a file on the local filesystem."""
        edits = data.get("edits", [])
        if not os.path.isfile(path):
            return {"op_id": op_id, "ok": False, "error": f"File not found: {path}"}

        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()

        sorted_edits = sorted(edits, key=lambda e: e.get("from", 0), reverse=True)
        for edit in sorted_edits:
            fr = edit.get("from", 1)
            to = edit.get("to")
            content = edit.get("content")
            idx = fr - 1

            if to is None and content is not None:
                new_lines = content.splitlines(True)
                lines[idx:idx] = new_lines
            elif content is None:
                to_idx = to if to else fr
                del lines[idx:to_idx]
            else:
                to_idx = to if to else fr
                new_lines = content.splitlines(True)
                lines[idx:to_idx] = new_lines

        with open(path, "w", encoding="utf-8") as f:
            f.writelines(lines)

        return {"op_id": op_id, "ok": True, "result": {"path": path}}

    # ------------------------------------------------------------------
    # Event rendering
    # ------------------------------------------------------------------

    def _render_connector_event(self, log: RichLog, event: dict[str, Any]) -> None:
        """Render a connector event to the chat log."""
        event_type = event.get("event", "")
        data = event.get("data", {})
        text = data.get("text", "")
        heading = data.get("heading", "")

        category = _EVENT_CATEGORY.get(event_type, "info")

        if category == "user":
            if text:
                log.write(f"[bold cyan]You:[/bold cyan] {text}")
            return

        if category == "response":
            log.write("[bold green]Agent Zero:[/bold green]")
            if text:
                log.write(Markdown(text))
            return

        if category == "warning":
            msg = f"{heading}: {text}" if heading else text
            log.write(f"[yellow]{msg}[/yellow]")
            return

        if category == "error":
            msg = f"{heading}: {text}" if heading else text
            log.write(f"[red]{msg}[/red]")
            return

    # ------------------------------------------------------------------
    # Message submission
    # ------------------------------------------------------------------

    async def on_chat_input_submitted(self, event: ChatInput.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        if not text:
            return

        if text.startswith("/"):
            await self._handle_command(text)
            return

        log = self.query_one("#chat-log", RichLog)
        if not self.current_context:
            log.write("[red]No active chat context.[/red]")
            return

        event.input.disabled = True
        self.agent_active = True
        try:
            await self.client.send_message(text, self.current_context)
        except Exception as exc:
            log.write(f"[red]Error sending message: {exc}[/red]")
            event.input.disabled = False
            self.agent_active = False

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    async def _handle_command(self, text: str) -> None:
        command = text.split()[0].lower()
        handlers = {
            "/chats": self._cmd_chats,
            "/new": self._cmd_new,
            "/exit": self._cmd_exit,
            "/help": self._cmd_help,
        }
        handler = handlers.get(command)
        if handler is None:
            log = self.query_one("#chat-log", RichLog)
            log.write(
                f"[yellow]Unknown command: {command}. Type /help for available commands.[/yellow]"
            )
            return
        if command == "/chats":
            self.run_worker(handler(), exclusive=True, name="cmd-chats")
            return
        await handler()

    async def _cmd_chats(self) -> None:
        log = self.query_one("#chat-log", RichLog)
        try:
            contexts = await self.client.list_chats()
        except Exception as exc:
            log.write(f"[red]Error listing chats: {exc}[/red]")
            return

        if not contexts:
            log.write("[dim]No previous chats found.[/dim]")
            return

        result = await self.push_screen_wait(ChatListScreen(contexts))
        if not result:
            return

        if self.current_context:
            await self.client.unsubscribe_context(self.current_context)
        self.current_context = result
        log.clear()
        self._set_status("Waiting for input")
        await self.client.subscribe_context(result, from_seq=0)

    async def _cmd_new(self) -> None:
        log = self.query_one("#chat-log", RichLog)
        if self.current_context:
            await self.client.unsubscribe_context(self.current_context)
        self.current_context = await self.client.create_chat()
        log.clear()
        self._set_status("Waiting for input")
        await self.client.subscribe_context(self.current_context)

    async def _cmd_exit(self) -> None:
        await self.client.disconnect()
        self.exit()

    async def _cmd_help(self) -> None:
        log = self.query_one("#chat-log", RichLog)
        log.write("[bold]Available commands:[/bold]")
        log.write("/chats - List previous chats")
        log.write("/new - Start a new chat")
        log.write("/exit - Exit the CLI")
        log.write("/help - Show this help")

    # ------------------------------------------------------------------
    # Key-binding actions
    # ------------------------------------------------------------------

    async def action_clear_chat(self) -> None:
        """F5 - Clear the chat log display."""
        log = self.query_one("#chat-log", RichLog)
        log.clear()

    async def action_list_chats(self) -> None:
        """F6 - Open the chat list screen."""
        self.run_worker(self._cmd_chats(), exclusive=True, name="cmd-chats")

    async def action_nudge_agent(self) -> None:
        """F7 - Send a nudge to the agent to continue."""
        if not self.current_context or not self.connected or self.agent_active:
            return
        log = self.query_one("#chat-log", RichLog)
        input_widget = self.query_one("#message-input", ChatInput)
        input_widget.disabled = True
        self.agent_active = True
        try:
            await self.client.send_message(".", self.current_context)
        except Exception as exc:
            log.write(f"[red]Nudge failed: {exc}[/red]")
            input_widget.disabled = False
            self.agent_active = False

    async def action_pause_agent(self) -> None:
        """F8 - Toggle pause on the agent (interrupt current run)."""
        if not self.connected:
            return
        log = self.query_one("#chat-log", RichLog)
        if not hasattr(self.client, "pause_agent"):
            log.write("[yellow]Pause not supported by this connector version.[/yellow]")
            return
        try:
            await self.client.pause_agent(self.current_context)
            log.write("[dim]Agent paused.[/dim]")
        except Exception as exc:
            log.write(f"[red]Pause failed: {exc}[/red]")
