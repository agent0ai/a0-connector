from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Center, Vertical
from textual.screen import Screen
from textual.widgets import Button, Input, Static


class HostInputScreen(Screen[str]):
    """Prompt for the Agent Zero host URL."""

    DEFAULT_HOST = "http://localhost:5080"

    def compose(self) -> ComposeResult:
        with Center():
            with Vertical(id="host-box"):
                yield Static("Agent Zero Host", id="host-title")
                yield Static(
                    "Enter the URL of your Agent Zero instance:",
                    id="host-description",
                )
                yield Input(
                    value=self.DEFAULT_HOST,
                    placeholder=self.DEFAULT_HOST,
                    id="host-url",
                )
                yield Button("Connect", id="host-btn", variant="primary")

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "host-btn":
            return
        self._submit()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "host-url":
            self._submit()

    def _submit(self) -> None:
        url = self.query_one("#host-url", Input).value.strip()
        if not url:
            url = self.DEFAULT_HOST
        self.dismiss(url)
