from __future__ import annotations

from rich.text import Text
from textual.app import RenderResult
from textual.reactive import reactive
from textual.widgets import Static


class ConnectionStatus(Static):
    """A subtle connection status indicator at the top right."""

    status = reactive("connecting")
    url = reactive("")
    _tick_count = reactive(0)

    def on_mount(self) -> None:
        self.set_interval(0.1, self._tick)

    def _tick(self) -> None:
        if self.status == "connecting":
            self._tick_count += 1

    def render(self) -> RenderResult:
        label = f"{self.url} " if self.url else ""
        if self.status == "connected":
            return Text.assemble(
                (label, "dim"),
                ("•", "green")
            )
        elif self.status == "connecting":
            return Text.assemble(
                (label, "dim"),
                ("•", "yellow")
            )
        else:
            label = f"Disconnected ({self.url}) " if self.url else "Disconnected "
            return Text.assemble(
                (label, "dim"),
                ("•", "red")
            )
