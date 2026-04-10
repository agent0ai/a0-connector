from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from agent_zero_cli.project_utils import (
    display_project_title,
    normalize_project_summary,
    project_color,
    project_name,
    project_title,
)


@dataclass(frozen=True)
class ProjectMenuResult:
    action: str
    project_name: str | None = None


def _project_button_label(project: Mapping[str, object], *, active: bool) -> Text:
    color = project_color(project)
    dot = "●" if color else "○"
    title = display_project_title(project, default="Unnamed project")
    name = project_name(project)

    label = Text()
    label.append(dot, style=color or "#7f8c98")
    label.append(f" {title}", style="#d9e2ec" if not active else "#7f8c98")
    if name and name != project_title(project):
        label.append(f"  /{name}", style="#7f8c98")
    return label


class ProjectMenuScreen(ModalScreen[ProjectMenuResult | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(
        self,
        projects: Sequence[Mapping[str, object]] | None = None,
        *,
        current_project: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__()
        self._projects = [
            normalized
            for project in projects or ()
            if (normalized := normalize_project_summary(project)) is not None
        ]
        self._current_project = normalize_project_summary(current_project)
        self._current_project_name = project_name(self._current_project)

    def compose(self) -> ComposeResult:
        with Vertical(id="project-menu-box"):
            yield Static("Projects", id="project-menu-title")
            if self._current_project_name:
                current_title = display_project_title(self._current_project, default=self._current_project_name)
                yield Button(f"Edit {current_title}", id="project-menu-edit", classes="project-menu-action")
                yield Button("Deactivate", id="project-menu-deactivate", classes="project-menu-action")
                yield Static("Switch Project", classes="project-menu-section")
            with VerticalScroll(id="project-menu-items"):
                if self._projects:
                    for project in self._projects:
                        name = project_name(project)
                        if not name:
                            continue
                        button = Button(
                            _project_button_label(project, active=name == self._current_project_name),
                            id=f"project-menu-switch-{name}",
                            classes="project-menu-project",
                        )
                        button.disabled = name == self._current_project_name
                        yield button
                else:
                    yield Static("No projects available.", id="project-menu-empty")

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id == "project-menu-edit":
            self.dismiss(ProjectMenuResult(action="edit", project_name=self._current_project_name or None))
            return
        if button_id == "project-menu-deactivate":
            self.dismiss(ProjectMenuResult(action="deactivate", project_name=self._current_project_name or None))
            return
        if button_id.startswith("project-menu-switch-"):
            self.dismiss(
                ProjectMenuResult(
                    action="activate",
                    project_name=button_id.removeprefix("project-menu-switch-") or None,
                )
            )
