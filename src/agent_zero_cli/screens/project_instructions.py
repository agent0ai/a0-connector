from __future__ import annotations

from dataclasses import dataclass

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Button, Static, TextArea


@dataclass(frozen=True)
class ProjectInstructionsResult:
    instructions: str


class ProjectInstructionsScreen(Screen[ProjectInstructionsResult | None]):
    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+s", "save", "Save", show=True, priority=True),
    ]

    def __init__(
        self,
        *,
        title: str,
        name: str,
        instructions: str,
    ) -> None:
        super().__init__()
        self._title = title
        self._name = name
        self._instructions = instructions

    def compose(self) -> ComposeResult:
        with Vertical(id="project-instructions-box"):
            yield Static("Project Instructions", id="project-instructions-title")
            yield Static(f"{self._title}  /{self._name}", id="project-instructions-subtitle")
            yield Static(
                "Editing the main instructions only. Change description, color, knowledge, and other metadata in the WebUI.",
                id="project-instructions-description",
            )
            yield TextArea(
                self._instructions,
                language=None,
                show_line_numbers=False,
                soft_wrap=True,
                id="project-instructions-input",
            )
            with Horizontal(id="project-instructions-actions"):
                yield Button("Cancel", id="project-instructions-cancel")
                yield Button("Save", id="project-instructions-save", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#project-instructions-input", TextArea).focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_save(self) -> None:
        instructions = self.query_one("#project-instructions-input", TextArea).text
        self.dismiss(ProjectInstructionsResult(instructions=instructions))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id == "project-instructions-save":
            self.action_save()
        elif button_id == "project-instructions-cancel":
            self.dismiss(None)
