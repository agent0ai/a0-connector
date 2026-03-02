from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Center, Vertical
from textual.screen import Screen
from textual.widgets import Button, Input, Static

from agent_zero_cli.client import A0Client


class LoginScreen(Screen[bool]):
    """Login screen for authenticated instances."""

    MAX_ATTEMPTS = 3

    def __init__(self, client: A0Client) -> None:
        super().__init__()
        self.client = client
        self.attempts = 0

    def compose(self) -> ComposeResult:
        with Center():
            with Vertical(id="login-box"):
                yield Static("Agent Zero - Login", id="login-title")
                yield Input(placeholder="Username", id="username")
                yield Input(placeholder="Password", password=True, id="password")
                yield Button("Login", id="login-btn", variant="primary")
                yield Static("", id="login-error")

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "login-btn":
            return
        await self._attempt_login()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "password":
            await self._attempt_login()

    async def _attempt_login(self) -> None:
        username = self.query_one("#username", Input).value
        password = self.query_one("#password", Input).value
        error = self.query_one("#login-error", Static)

        if not username or not password:
            error.update("Username and password are required.")
            return

        ok = await self.client.login(username, password)
        if ok:
            self.dismiss(True)
            return

        self.attempts += 1
        remaining = self.MAX_ATTEMPTS - self.attempts
        if remaining <= 0:
            error.update("Login failed. Too many attempts.")
            self.app.exit(return_code=1)
            return

        error.update(f"Invalid credentials. {remaining} attempts left.")
