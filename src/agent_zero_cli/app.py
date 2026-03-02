from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.reactive import reactive
from textual.widgets import Footer, Header, Input, RichLog
from rich.markdown import Markdown
from rich.syntax import Syntax

from agent_zero_cli.client import A0Client
from agent_zero_cli.config import CLIConfig, load_config
from agent_zero_cli.screens.chat_list import ChatListScreen
from agent_zero_cli.screens.login import LoginScreen


class AgentZeroCLI(App):
    """Agent Zero CLI - Terminal Chat Interface."""

    CSS_PATH = "styles/app.tcss"
    TITLE = "Agent Zero CLI"
    BINDINGS = [
        Binding("ctrl+c", "quit", "Exit", show=True),
    ]

    connected = reactive(False)
    agent_active = reactive(False)

    def __init__(self, config: CLIConfig | None = None) -> None:
        super().__init__()
        self.config = config or load_config()
        self.client = A0Client(self.config.instance_url)
        self.current_context: str | None = None
        self.log_cursor = 0
        self.log_guid: str | None = None
        self.contexts: list[dict[str, Any]] = []
        self._install_prompt_future: asyncio.Future[bool] | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield RichLog(id="chat-log", wrap=True, highlight=True, markup=True)
        yield Input(placeholder="Type a message... (/help for commands)", id="message-input")
        yield Footer()

    async def on_mount(self) -> None:
        input_widget = self.query_one("#message-input", Input)
        input_widget.disabled = True
        input_widget.focus()
        self._refresh_subtitle()
        self.run_worker(self._startup(), exclusive=True, name="startup")

    def watch_connected(self, connected: bool) -> None:
        self._refresh_subtitle()

    def watch_agent_active(self, agent_active: bool) -> None:
        self._refresh_subtitle()

    def _refresh_subtitle(self) -> None:
        if self.agent_active:
            self.sub_title = "Agent thinking..."
        elif self.connected:
            self.sub_title = "Connected"
        else:
            self.sub_title = "Disconnected"

    async def _startup(self) -> None:
        log = self.query_one("#chat-log", RichLog)
        input_widget = self.query_one("#message-input", Input)

        log.write("[dim]Connecting to Agent Zero...[/dim]")
        if not await self.client.check_health():
            log.write(f"[red]No Agent Zero instance found at {self.config.instance_url}[/red]")
            should_install = await self._prompt_install(log, input_widget)
            if should_install:
                await self._run_install(log)
                if not await self.client.check_health():
                    log.write("[red]Agent Zero is still unreachable after install.[/red]")
                    input_widget.disabled = True
                    return
            else:
                log.write("[dim]Update .cli-config.json or start Agent Zero manually.[/dim]")
                input_widget.disabled = True
                return

        if await self.client.needs_auth():
            login_ok = await self.push_screen_wait(LoginScreen(self.client))
            if not login_ok:
                log.write("[red]Authentication failed. Exiting.[/red]")
                self.exit(return_code=1)
                return

        self.client.on_state_push = self._handle_state_push
        self.client.on_connect = lambda: self._run_on_ui(self._set_connected, True)
        self.client.on_disconnect = lambda: self._run_on_ui(self._set_connected, False)

        await self.client.connect_websocket()
        self.current_context = await self.client.create_chat()
        await self.client.request_state(self.current_context)
        log.write("[green]Connected to Agent Zero.[/green]")
        input_widget.disabled = False

    async def _prompt_install(self, log: RichLog, input_widget: Input) -> bool:
        log.write("[dim]Would you like to install Agent Zero? (y/n)[/dim]")
        loop = asyncio.get_running_loop()
        self._install_prompt_future = loop.create_future()
        input_widget.disabled = False
        input_widget.focus()
        result = await self._install_prompt_future
        self._install_prompt_future = None
        return result

    async def _run_install(self, log: RichLog) -> None:
        log.write("[dim]Launching install.sh...[/dim]")
        install_path = self._install_script_path()
        with self.suspend():
            subprocess.run(["/bin/bash", str(install_path)], check=False)
        log.write("[dim]Install finished. Retrying connection...[/dim]")

    def _install_script_path(self) -> Path:
        return Path(__file__).resolve().parents[3] / "install.sh"

    def _set_connected(self, value: bool) -> None:
        self.connected = value

    def _handle_state_push(self, data: dict[str, Any]) -> None:
        self._run_on_ui(self._handle_state_push_ui, data)

    def _run_on_ui(self, func: Any, *args: Any) -> None:
        app_loop = getattr(self, "loop", None)  # type: ignore[attr-defined]
        if app_loop is None:
            func(*args)
        else:
            app_loop.call_soon_threadsafe(func, *args)

    def _handle_state_push_ui(self, data: dict[str, Any]) -> None:
        payload = data.get("data", data)
        snapshot = payload.get("snapshot", payload)
        log_widget = self.query_one("#chat-log", RichLog)
        log_guid = snapshot.get("log_guid")
        if log_guid and log_guid != self.log_guid:
            self.log_guid = log_guid
            self.log_cursor = 0
            log_widget.clear()
        logs = snapshot.get("logs", [])
        if isinstance(logs, list):
            for entry in logs:
                if isinstance(entry, dict):
                    self._render_log_entry(log_widget, entry)
        log_version = snapshot.get("log_version")
        if isinstance(log_version, int):
            self.log_cursor = max(self.log_cursor, log_version)
        self.agent_active = bool(snapshot.get("log_progress_active", False))
        input_widget = self.query_one("#message-input", Input)
        input_widget.disabled = self.agent_active
        contexts = snapshot.get("contexts")
        if isinstance(contexts, list):
            self.contexts = contexts

    def _render_log_entry(self, log: RichLog, entry: dict[str, Any]) -> None:
        entry_type = entry.get("type", "")
        heading = entry.get("heading", "")
        content = entry.get("content", "")
        if heading is None:
            heading = ""
        if content is None:
            content = ""
        if not isinstance(heading, str):
            heading = str(heading)
        if not isinstance(content, str):
            content = str(content)

        if entry_type == "response":
            log.write("[bold green]Agent Zero:[/bold green]")
            if content:
                log.write(Markdown(content))
            return

        if entry_type == "tool":
            title = heading or "Tool"
            log.write(f"[dim]Tool: {title}[/dim]")
            if content:
                log.write(f"[dim]{content}[/dim]")
            return

        if entry_type == "code_exe":
            title = heading or "Code"
            log.write(f"[dim]Code: {title}[/dim]")
            if content:
                log.write(Syntax(content, "text", word_wrap=True))
            return

        if entry_type == "warning":
            text = f"{heading}: {content}" if heading else content
            log.write(f"[yellow]{text}[/yellow]")
            return

        if entry_type == "error":
            text = f"{heading}: {content}" if heading else content
            log.write(f"[red]{text}[/red]")
            return

        if entry_type == "info":
            text = f"{heading}: {content}" if heading else content
            log.write(f"[dim]{text}[/dim]")
            return

        if heading or content:
            text = f"{heading}: {content}" if heading else content
            log.write(text)

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        if not text:
            return
        if self._install_prompt_future and not self._install_prompt_future.done():
            response = text.lower()
            if response in {"y", "yes"}:
                self._install_prompt_future.set_result(True)
            elif response in {"n", "no"}:
                self._install_prompt_future.set_result(False)
            else:
                log = self.query_one("#chat-log", RichLog)
                log.write("[yellow]Please answer y or n.[/yellow]")
            return

        if text.startswith("/"):
            await self._handle_command(text)
            return

        log = self.query_one("#chat-log", RichLog)
        if not self.current_context:
            log.write("[red]No active chat context.[/red]")
            return

        log.write(f"[bold cyan]You:[/bold cyan] {text}")
        event.input.disabled = True
        try:
            await self.client.send_message(text, self.current_context)
        except Exception as exc:  # pragma: no cover - network errors
            log.write(f"[red]Error sending message: {exc}[/red]")
            event.input.disabled = False

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
        if not self.contexts:
            log.write("[dim]No previous chats found.[/dim]")
            return
        result = await self.push_screen_wait(ChatListScreen(self.contexts))
        if not result:
            return
        self.current_context = result
        self.log_cursor = 0
        self.log_guid = None
        log.clear()
        await self.client.request_state(result, log_from=0)

    async def _cmd_new(self) -> None:
        log = self.query_one("#chat-log", RichLog)
        self.current_context = await self.client.create_chat()
        self.log_cursor = 0
        self.log_guid = None
        log.clear()
        await self.client.request_state(self.current_context, log_from=0)

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
