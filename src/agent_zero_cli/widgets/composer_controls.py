from __future__ import annotations

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.message import Message
from textual.reactive import reactive
from textual.widgets import Button

from agent_zero_cli.text_utils import clip_text


class ComposerControls(Horizontal):
    controls: reactive[list[dict]] = reactive(list, recompose=True)

    class Requested(Message):
        def __init__(self, control_id: str, anchor_id: str) -> None:
            super().__init__()
            self.control_id = control_id
            self.anchor_id = anchor_id

    class Resized(Message):
        pass

    def compose(self) -> ComposeResult:
        for index, control in enumerate(self.controls):
            label = str(control.get("label") or control["id"])
            yield Button(
                Text(f"{clip_text(label, 24)} ▴"), name=control["id"],
                id=f"composer-control-{index}", classes="model-switcher-chip composer-control",
                tooltip=str(control.get("title") or label),
            )

    def set_controls(self, controls: list[dict]) -> None:
        self.controls = controls
        self.display = bool(controls)

    def on_resize(self, event: events.Resize) -> None:
        self.post_message(self.Resized())

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.post_message(self.Requested(event.button.name or "", event.button.id or ""))
