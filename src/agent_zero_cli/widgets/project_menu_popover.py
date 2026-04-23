from __future__ import annotations

from typing import Mapping, Sequence

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.message import Message
from textual.reactive import reactive
from textual.widgets import Static

from agent_zero_cli.project_utils import (
    display_project_title,
    normalize_project_summary,
    project_color,
    project_name,
    project_title,
)


def _project_item_label(project: Mapping[str, object], *, active: bool) -> Text:
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


class ProjectMenuItem(Static):
    can_focus = True
    disabled = reactive(False)

    class Selected(Message):
        def __init__(self, action: str, project_name: str | None = None) -> None:
            super().__init__()
            self.action = action
            self.project_name = project_name

    def __init__(
        self,
        label: str | Text,
        *,
        action: str,
        project_name: str | None = None,
        disabled: bool = False,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(label, id=id, classes=classes)
        self.action_name = action
        self.project_name = project_name
        self.disabled = disabled
        self.can_focus = not disabled
        if disabled:
            self.add_class("-disabled")

    def watch_disabled(self, disabled: bool) -> None:
        self.can_focus = not disabled
        if disabled:
            self.add_class("-disabled")
        else:
            self.remove_class("-disabled")

    def on_click(self, event: events.Click) -> None:
        event.stop()
        self._select()

    def on_key(self, event) -> None:
        if event.key == "escape":
            event.prevent_default()
            event.stop()
            self.post_message(ProjectMenuPopover.DismissRequested())
            return
        if event.key in {"enter", "space"}:
            event.prevent_default()
            event.stop()
            self._select()

    def _select(self) -> None:
        if self.disabled:
            return
        self.post_message(self.Selected(self.action_name, self.project_name))


class ProjectMenuPopover(Vertical):
    BINDINGS = [Binding("escape", "dismiss", "Cancel", show=False)]

    class DismissRequested(Message):
        pass

    def __init__(
        self,
        projects: Sequence[Mapping[str, object]] | None = None,
        *,
        current_project: Mapping[str, object] | None = None,
        id: str | None = None,
    ) -> None:
        super().__init__(id=id)
        self._projects = [
            normalized
            for project in projects or ()
            if (normalized := normalize_project_summary(project)) is not None
        ]
        self._current_project = normalize_project_summary(current_project)
        self._current_project_name = project_name(self._current_project)

    def compose(self) -> ComposeResult:
        if self._current_project_name:
            current_title = display_project_title(self._current_project, default=self._current_project_name)
            yield ProjectMenuItem(
                f"Edit {current_title}",
                action="edit",
                project_name=self._current_project_name,
                id="project-menu-edit",
                classes="project-menu-item",
            )
            yield ProjectMenuItem(
                "Deactivate",
                action="deactivate",
                project_name=self._current_project_name,
                id="project-menu-deactivate",
                classes="project-menu-item",
            )
            yield Static("Switch Project", classes="project-menu-section")
        with VerticalScroll(id="project-menu-items"):
            if self._projects:
                for project in self._projects:
                    name = project_name(project)
                    if not name:
                        continue
                    yield ProjectMenuItem(
                        _project_item_label(project, active=name == self._current_project_name),
                        action="activate",
                        project_name=name,
                        disabled=name == self._current_project_name,
                        id=f"project-menu-switch-{name}",
                        classes="project-menu-item project-menu-project",
                    )
            else:
                yield Static("No projects available.", id="project-menu-empty")

    def action_dismiss(self) -> None:
        self.post_message(self.DismissRequested())

    def focus_first_item(self) -> None:
        for item in self.query(ProjectMenuItem):
            if not item.disabled:
                item.focus()
                break
