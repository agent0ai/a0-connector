from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.geometry import Offset
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static


@dataclass(frozen=True)
class ComposerChoice:
    value: str
    custom: bool = False


class ComposerChoiceScreen(ModalScreen[ComposerChoice | None]):
    """Render a server-defined choice control without interpreting its values."""

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

    def __init__(
        self, control: Mapping[str, Any], *, anchor_id: str,
        custom_value: str = "", error: str = "",
    ) -> None:
        super().__init__()
        self._control = control
        self._anchor_id = anchor_id
        options = control.get("options")
        self._choices = [
            option for option in options
            if isinstance(option, Mapping) and isinstance(option.get("value"), str)
        ] if isinstance(options, list) else []
        self._custom_value = custom_value or (
            str(control.get("value") or "")
            if control.get("value") not in {option["value"] for option in self._choices}
            else ""
        )
        self._error = error

    def compose(self) -> ComposeResult:
        selected = self._control.get("value")
        with Vertical(id="composer-choice-popover"):
            yield Static(Text(str(self._control.get("title") or "Choose value")), id="composer-choice-title")
            with VerticalScroll(id="composer-choice-options"):
                for index, option in enumerate(self._choices):
                    marker = "✓" if selected == option["value"] else " "
                    yield Button(
                        Text(f"{marker} {option.get('label') or option['value']}"),
                        id=f"composer-choice-option-{index}", classes="composer-choice-option",
                    )
            if self._control.get("description"):
                yield Static(Text(str(self._control["description"])), classes="composer-choice-note")
            if self._control.get("custom") is True:
                with Horizontal(id="composer-choice-custom-row"):
                    yield Input(self._custom_value, placeholder="Custom value", max_length=64, id="composer-choice-custom")
                    yield Button("Apply", id="composer-choice-apply", disabled=not self._custom_value.strip())
            if self._error:
                yield Static(Text(self._error), id="composer-choice-error")

    def on_mount(self) -> None:
        self.call_after_refresh(self._position)
        if self._choices:
            self.query_one("#composer-choice-option-0", Button).focus()
        elif self._control.get("custom") is True:
            self.query_one(Input).focus()

    def on_resize(self) -> None:
        self.call_after_refresh(self._position)

    def _position(self) -> None:
        box = self.query_one("#composer-choice-popover", Vertical)
        box.styles.width = min(44, max(1, self.size.width - 2))
        box.styles.max_height = max(1, self.size.height - 2)
        try:
            anchor = self.app.query_one(f"#{self._anchor_id}", Button).region
        except Exception:
            return
        width = box.region.width or 44
        height = box.region.height or 14
        box.absolute_offset = Offset(
            max(0, min(anchor.x, self.size.width - width)),
            max(0, min(anchor.y - height, self.size.height - height)),
        )

    def on_click(self, event: events.Click) -> None:
        if not self.query_one("#composer-choice-popover").region.contains(event.screen_x, event.screen_y):
            self.action_cancel()

    def on_input_changed(self, event: Input.Changed) -> None:
        self.query_one("#composer-choice-apply", Button).disabled = not event.value.strip()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._apply_custom(event.value)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        button_id = event.button.id or ""
        if button_id == "composer-choice-apply":
            self._apply_custom(self.query_one("#composer-choice-custom", Input).value)
        elif button_id.startswith("composer-choice-option-"):
            index = int(button_id.rsplit("-", 1)[1])
            self.dismiss(ComposerChoice(self._choices[index]["value"]))

    def _apply_custom(self, value: str) -> None:
        if value.strip():
            self.dismiss(ComposerChoice(value, custom=True))

    def action_cancel(self) -> None:
        self.dismiss(None)
