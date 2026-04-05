from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from rich.markdown import Markdown
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Center, Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, ListItem, ListView, Select, Static


@dataclass(frozen=True)
class FilterOption:
    value: str
    label: str


@dataclass(frozen=True)
class SkillsFilters:
    project_name: str | None = None
    agent_profile: str | None = None


@dataclass(frozen=True)
class SkillEntry:
    name: str
    description: str = ""
    path: str = ""
    project_name: str = ""
    agent_profile: str = ""


@dataclass
class SkillsFiltersChanged(Message):
    bubble = True

    filters: SkillsFilters


@dataclass
class SkillsRefreshRequested(Message):
    bubble = True

    filters: SkillsFilters


@dataclass
class SkillsSelectionChanged(Message):
    bubble = True

    skill: SkillEntry | None
    filters: SkillsFilters


@dataclass
class SkillsDeleteRequested(Message):
    bubble = True

    skill: SkillEntry
    filters: SkillsFilters


def _coerce_filter_option(value: object) -> FilterOption:
    if isinstance(value, FilterOption):
        return value
    if isinstance(value, str):
        clean = value.strip()
        return FilterOption(value=clean, label=clean or "Unnamed")
    if isinstance(value, Mapping):
        raw_value = str(
            value.get("value")
            or value.get("name")
            or value.get("id")
            or value.get("path")
            or ""
        ).strip()
        raw_label = str(value.get("label") or value.get("title") or raw_value).strip()
        return FilterOption(value=raw_value, label=raw_label or raw_value or "Unnamed")
    clean = str(value).strip()
    return FilterOption(value=clean, label=clean or "Unnamed")


def _coerce_skill_entry(value: object) -> SkillEntry:
    if isinstance(value, SkillEntry):
        return value
    if isinstance(value, Mapping):
        return SkillEntry(
            name=str(value.get("name") or value.get("title") or value.get("path") or "Skill"),
            description=str(value.get("description") or value.get("summary") or ""),
            path=str(value.get("path") or value.get("skill_path") or ""),
            project_name=str(value.get("project_name") or value.get("project") or ""),
            agent_profile=str(value.get("agent_profile") or value.get("profile") or ""),
        )
    clean = str(value).strip()
    return SkillEntry(name=clean or "Skill")


def _normalize_filter_value(value: object) -> str | None:
    if value in {"", None}:
        return None
    text = str(value).strip()
    return text or None


def _build_filter_options(
    values: Sequence[FilterOption | Mapping[str, Any] | str] | None,
    *,
    all_label: str,
) -> list[tuple[str, str]]:
    options: list[tuple[str, str]] = [(all_label, "")]
    for raw in values or []:
        option = _coerce_filter_option(raw)
        if not option.value:
            continue
        options.append((option.label, option.value))
    return options


def _filter_option_values(values: Sequence[FilterOption | Mapping[str, Any] | str] | None) -> set[str]:
    return {option.value for option in (_coerce_filter_option(raw) for raw in values or []) if option.value}


def _format_skill_details(skill: SkillEntry | None, filters: SkillsFilters) -> Markdown:
    if skill is None:
        lines = [
            "# Select a skill",
            "",
            "Choose a skill on the left to inspect its details.",
        ]
        if filters.project_name or filters.agent_profile:
            lines.extend(
                [
                    "",
                    "Current filters:",
                    f"- Project: {filters.project_name or 'all'}",
                    f"- Agent profile: {filters.agent_profile or 'all'}",
                ]
            )
        return Markdown("\n".join(lines))

    lines = [
        f"# {skill.name}",
        "",
        f"- Path: `{skill.path or 'Unavailable'}`",
        f"- Project: {skill.project_name or 'All projects'}",
        f"- Agent profile: {skill.agent_profile or 'All profiles'}",
    ]
    if skill.description.strip():
        lines.extend(["", skill.description.strip()])
    return Markdown("\n".join(lines))


class SkillDeleteConfirmScreen(ModalScreen[bool]):
    """Tiny confirmation dialog for destructive skill deletion."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("enter", "confirm", "Delete"),
    ]

    def __init__(self, skill: SkillEntry) -> None:
        super().__init__()
        self.skill = skill

    def compose(self) -> ComposeResult:
        with Center():
            with Vertical(id="skills-delete-confirm-box"):
                yield Static("Delete skill?", id="skills-delete-confirm-title")
                yield Static(
                    f"Delete `{self.skill.name}` at `{self.skill.path or 'Unknown path'}`?",
                    id="skills-delete-confirm-message",
                )
                with Horizontal(id="skills-delete-confirm-actions"):
                    yield Button("Cancel", id="skills-delete-confirm-cancel")
                    yield Button("Delete", id="skills-delete-confirm-delete", variant="error")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id == "skills-delete-confirm-delete":
            self.dismiss(True)
        elif button_id == "skills-delete-confirm-cancel":
            self.dismiss(False)


class SkillsScreen(ModalScreen[None]):
    """Skills browser with filters, details and app-driven actions."""

    BINDINGS = [
        Binding("escape", "cancel", "Close"),
        Binding("r", "refresh", "Refresh"),
        Binding("delete", "delete_selected", "Delete"),
    ]

    def __init__(
        self,
        skills: Sequence[SkillEntry | Mapping[str, Any]] | None = None,
        *,
        filters: SkillsFilters | Mapping[str, Any] | None = None,
        project_options: Sequence[FilterOption | Mapping[str, Any] | str] | None = None,
        agent_profile_options: Sequence[FilterOption | Mapping[str, Any] | str] | None = None,
    ) -> None:
        super().__init__()
        self._skills: list[SkillEntry] = [_coerce_skill_entry(skill) for skill in skills or []]
        self._skill_by_item_id: dict[str, SkillEntry] = {}
        self._selected_skill_path: str | None = None
        self._project_options = list(project_options or [])
        self._agent_profile_options = list(agent_profile_options or [])
        self._filters = self._coerce_filters(filters)
        self._busy = False
        self._status = ""
        self._suppress_events = False

    def compose(self) -> ComposeResult:
        with Center():
            with Vertical(id="skills-box"):
                yield Static("Skills", id="skills-title")
                yield Static(
                    "Filter loaded skills, inspect details, refresh the list, or request deletion.",
                    id="skills-description",
                )
                with Horizontal(id="skills-filters"):
                    with Vertical(id="skills-filter-project"):
                        yield Static("Project", id="skills-filter-project-label")
                        yield Select(
                            _build_filter_options(self._project_options, all_label="All projects"),
                            prompt="Project filter",
                            allow_blank=False,
                            value=self._filters.project_name or "",
                            id="skills-project-filter",
                        )
                    with Vertical(id="skills-filter-agent"):
                        yield Static("Agent profile", id="skills-filter-agent-label")
                        yield Select(
                            _build_filter_options(
                                self._agent_profile_options,
                                all_label="All agent profiles",
                            ),
                            prompt="Agent-profile filter",
                            allow_blank=False,
                            value=self._filters.agent_profile or "",
                            id="skills-agent-profile-filter",
                        )
                    with Vertical(id="skills-filter-actions"):
                        yield Static(" ", id="skills-filter-actions-spacer")
                        yield Button("Refresh", id="skills-refresh")
                yield Static("", id="skills-summary")
                with Horizontal(id="skills-body"):
                    with Vertical(id="skills-list-pane"):
                        yield Static("Skills", id="skills-list-title")
                        yield ListView(id="skills-list")
                    with VerticalScroll(id="skills-details-pane"):
                        yield Static(_format_skill_details(None, self._filters), id="skills-details")
                yield Static("", id="skills-status")
                with Horizontal(id="skills-actions"):
                    yield Button("Refresh", id="skills-refresh-bottom")
                    yield Button("Delete selected", id="skills-delete")
                    yield Button("Close", id="skills-close")

    def on_mount(self) -> None:
        self._sync_controls()
        self._rebuild_list()
        self._focus_list()

    def _coerce_filters(
        self, filters: SkillsFilters | Mapping[str, Any] | None
    ) -> SkillsFilters:
        if isinstance(filters, SkillsFilters):
            return filters
        if isinstance(filters, Mapping):
            return SkillsFilters(
                project_name=_normalize_filter_value(filters.get("project_name")),
                agent_profile=_normalize_filter_value(filters.get("agent_profile")),
            )
        return SkillsFilters()

    def _current_skill(self) -> SkillEntry | None:
        if not self._selected_skill_path:
            return None
        for skill in self._skill_by_item_id.values():
            if skill.path == self._selected_skill_path:
                return skill
        return None

    def _focus_list(self) -> None:
        list_view = self.query_one("#skills-list", ListView)
        list_view.focus()

    def _sync_controls(self) -> None:
        project_values = _filter_option_values(self._project_options)
        agent_values = _filter_option_values(self._agent_profile_options)
        self._suppress_events = True
        try:
            project_filter = self.query_one("#skills-project-filter", Select)
            agent_filter = self.query_one("#skills-agent-profile-filter", Select)
            project_filter.value = (
                self._filters.project_name if self._filters.project_name in project_values else ""
            )
            agent_filter.value = (
                self._filters.agent_profile if self._filters.agent_profile in agent_values else ""
            )
        finally:
            self._suppress_events = False
        self._update_status()

    def _update_status(self, message: str = "") -> None:
        self._status = message
        summary = self.query_one("#skills-summary", Static)
        filters_text = (
            f"Project: {self._filters.project_name or 'all'} | "
            f"Agent profile: {self._filters.agent_profile or 'all'} | "
            f"Skills: {len(self._skills)}"
        )
        summary.update(filters_text)
        self.query_one("#skills-status", Static).update(Text.from_markup(message) if message else "")

    def _rebuild_list(self) -> None:
        list_view = self.query_one("#skills-list", ListView)
        list_view.clear()
        self._skill_by_item_id.clear()
        matched_selection = False
        selected_index = 0

        for index, skill in enumerate(self._skills, start=1):
            item_id = f"skill-{index}"
            label_parts = [f"{index}. {skill.name}"]
            if skill.project_name:
                label_parts.append(skill.project_name)
            if skill.agent_profile:
                label_parts.append(skill.agent_profile)
            if skill.description.strip():
                label_parts.append(skill.description.strip())
            item = ListItem(Static(" | ".join(label_parts)), id=item_id)
            list_view.append(item)
            self._skill_by_item_id[item_id] = skill
            if skill.path and skill.path == self._selected_skill_path:
                matched_selection = True
                selected_index = index - 1

        if self._skills and not matched_selection:
            self._selected_skill_path = self._skills[0].path or None
            selected_index = 0

        if self._skills:
            list_view.index = selected_index

        self._render_details()

    def _render_details(self) -> None:
        details = self.query_one("#skills-details", Static)
        details.update(_format_skill_details(self._current_skill(), self._filters))

        delete_button = self.query_one("#skills-delete", Button)
        delete_button.disabled = self._busy or self._current_skill() is None

    def _set_busy(self, busy: bool, message: str = "") -> None:
        self._busy = busy
        for button_id in ("skills-refresh", "skills-refresh-bottom", "skills-delete"):
            self.query_one(f"#{button_id}", Button).disabled = busy
        self._update_status(message)
        self._render_details()

    def set_skills(
        self,
        skills: Sequence[SkillEntry | Mapping[str, Any]],
    ) -> None:
        """Replace the visible skill list without re-creating the modal."""

        selected_path = self._selected_skill_path
        self._skills = [_coerce_skill_entry(skill) for skill in skills]
        self._selected_skill_path = selected_path
        self._rebuild_list()
        self._update_status()

    def set_filter_options(
        self,
        *,
        project_options: Sequence[FilterOption | Mapping[str, Any] | str] | None = None,
        agent_profile_options: Sequence[FilterOption | Mapping[str, Any] | str] | None = None,
    ) -> None:
        self._project_options = list(project_options or [])
        self._agent_profile_options = list(agent_profile_options or [])
        self._suppress_events = True
        try:
            self.query_one("#skills-project-filter", Select).set_options(
                _build_filter_options(self._project_options, all_label="All projects")
            )
            self.query_one("#skills-agent-profile-filter", Select).set_options(
                _build_filter_options(
                    self._agent_profile_options,
                    all_label="All agent profiles",
                )
            )
        finally:
            self._suppress_events = False
        self._sync_controls()

    def set_filters(self, filters: SkillsFilters | Mapping[str, Any]) -> None:
        self._filters = self._coerce_filters(filters)
        self._sync_controls()
        self._render_details()

    def set_busy(self, busy: bool, message: str = "") -> None:
        self._set_busy(busy, message)

    def set_error(self, message: str) -> None:
        self._update_status(f"[red]{message}[/red]")

    @property
    def current_filters(self) -> SkillsFilters:
        return self._filters

    @property
    def selected_skill(self) -> SkillEntry | None:
        return self._current_skill()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_refresh(self) -> None:
        self.post_message(SkillsRefreshRequested(filters=self._filters))

    async def action_delete_selected(self) -> None:
        skill = self._current_skill()
        if skill is None:
            self._update_status("[yellow]Select a skill before deleting.[/yellow]")
            return

        confirmed = await self.app.push_screen_wait(SkillDeleteConfirmScreen(skill))
        if confirmed:
            self.post_message(SkillsDeleteRequested(skill=skill, filters=self._filters))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id in {"skills-refresh", "skills-refresh-bottom"}:
            self.action_refresh()
        elif button_id == "skills-delete":
            self.run_worker(self.action_delete_selected(), exclusive=True, name="skills-delete")
        elif button_id == "skills-close":
            self.dismiss(None)

    def on_select_changed(self, event: Select.Changed) -> None:
        if self._suppress_events:
            return
        widget_id = event.select.id or ""
        if widget_id == "skills-project-filter":
            self._filters = SkillsFilters(
                project_name=_normalize_filter_value(event.value),
                agent_profile=self._filters.agent_profile,
            )
        elif widget_id == "skills-agent-profile-filter":
            self._filters = SkillsFilters(
                project_name=self._filters.project_name,
                agent_profile=_normalize_filter_value(event.value),
            )
        else:
            return

        self._update_status()
        self.post_message(SkillsFiltersChanged(filters=self._filters))

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        item_id = event.item.id or ""
        skill = self._skill_by_item_id.get(item_id)
        self._selected_skill_path = skill.path if skill is not None else None
        self._render_details()
        self.post_message(SkillsSelectionChanged(skill=skill, filters=self._filters))


__all__ = [
    "FilterOption",
    "SkillDeleteConfirmScreen",
    "SkillEntry",
    "SkillsDeleteRequested",
    "SkillsFilters",
    "SkillsFiltersChanged",
    "SkillsRefreshRequested",
    "SkillsScreen",
    "SkillsSelectionChanged",
]
