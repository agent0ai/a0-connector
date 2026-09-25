from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Mapping, Sequence

from rich.cells import cell_len
from rich.text import Text
from textual import events
from textual.binding import Binding
from textual.message import Message
from textual.widgets import Static

from agent_zero_cli.project_utils import normalize_project_summary, project_color
from agent_zero_cli.text_utils import collapse_whitespace

_ISO_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
_ABSOLUTE_MIN_LABEL_CELLS = 3
_PREFERRED_MIN_LABEL_CELLS = 8
_MAX_LABEL_CELLS = 20
_MIN_RULE_CELLS = 4
_TAB_LEFT = "┌"
_TAB_RIGHT = "┐"
_RULE = "_"
_ACTIVE_BORDER = "#00b4ff"
_INACTIVE_BORDER = "#2a3a4a"
_ACTIVE_LABEL = "bold #f5f7fa"
_INACTIVE_LABEL = "#9aa7b4"
_ACTIVE_CLOSE = "bold #f5f7fa on #214459"
_INACTIVE_CLOSE = "#aab4c0 on #172331"
_CREATE_LABEL = "bold #79d18a"
_CLOSE_BUTTON_LABEL = "[×]"


@dataclass(frozen=True)
class ContextTab:
    context_id: str
    label: str
    has_messages: bool = False
    project_color: str = ""


def _trim_to_cells(value: str, max_cells: int) -> str:
    if max_cells <= 0:
        return ""
    if cell_len(value) <= max_cells:
        return value
    suffix = "..."
    if max_cells <= cell_len(suffix):
        return "." * max_cells
    budget = max(0, max_cells - cell_len(suffix))
    trimmed = ""
    used = 0
    for char in value:
        char_width = cell_len(char)
        if used + char_width > budget:
            break
        trimmed += char
        used += char_width
    return trimmed.rstrip() + suffix


def _fallback_label(context_id: str, index: int) -> str:
    del context_id
    return f"Chat {index}"


def _format_webui_label(metadata: Mapping[str, object], context_id: str, index: int) -> str:
    raw_name = collapse_whitespace(metadata.get("name"))
    if raw_name and raw_name != context_id:
        return raw_name

    try:
        chat_no = int(metadata.get("no", 0) or 0)
    except (TypeError, ValueError):
        chat_no = 0
    if chat_no > 0:
        return f"Chat #{chat_no}"

    return _fallback_label(context_id, index)


def _project_color_from_metadata(metadata: Mapping[str, object]) -> str:
    for key in ("project", "current_project"):
        project = metadata.get(key)
        if isinstance(project, Mapping):
            color = project_color(normalize_project_summary(project) or project)
            if color:
                return color

    for key in ("project_color", "project_colour", "color"):
        color = collapse_whitespace(metadata.get(key))
        if color:
            return color
    return ""


def _positive_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _last_message_has_content(metadata: Mapping[str, object]) -> bool:
    last_message = collapse_whitespace(metadata.get("last_message"))
    if not last_message:
        return False
    if last_message == collapse_whitespace(metadata.get("created_at")):
        return False
    return _ISO_TIMESTAMP_RE.match(last_message) is None


def _metadata_has_messages(metadata: Mapping[str, object]) -> bool:
    if metadata.get("has_messages") is True:
        return True

    for key in ("log_entries", "message_count", "log_length"):
        if _positive_int(metadata.get(key)) > 0:
            return True

    return _last_message_has_content(metadata)


def context_tab_from_metadata(
    context: Mapping[str, object] | None,
    *,
    context_id: str = "",
    index: int = 1,
    has_messages_hint: bool = False,
) -> ContextTab:
    metadata = context or {}
    normalized_context_id = collapse_whitespace(
        context_id
        or metadata.get("id")
        or metadata.get("context_id")
        or metadata.get("ctxid")
    )
    label = _format_webui_label(metadata, normalized_context_id, index)

    has_messages = bool(has_messages_hint or _metadata_has_messages(metadata))
    return ContextTab(
        context_id=normalized_context_id,
        label=label,
        has_messages=has_messages,
        project_color=_project_color_from_metadata(metadata),
    )


class ContextTabs(Static):
    """A compact, single-line tab strip for chat contexts."""

    can_focus = True
    BINDINGS = [
        Binding("left", "previous_tab", "Previous tab", show=False),
        Binding("right", "next_tab", "Next tab", show=False),
        Binding("enter", "activate", "Activate tab", show=False),
        Binding("x", "close_tab", "Close tab", show=False),
        Binding("n", "new_chat", "New chat", show=False),
    ]

    class ContextSelected(Message):
        def __init__(self, context_id: str, tabs: "ContextTabs") -> None:
            super().__init__()
            self.context_id = context_id
            self.tabs = tabs

    class NewRequested(Message):
        def __init__(self, tabs: "ContextTabs") -> None:
            super().__init__()
            self.tabs = tabs

    class CloseRequested(Message):
        def __init__(
            self,
            context_id: str,
            replacement_context_id: str,
            tabs: "ContextTabs",
        ) -> None:
            super().__init__()
            self.context_id = context_id
            self.replacement_context_id = replacement_context_id
            self.tabs = tabs

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__("", id=id, markup=False)
        self._tabs: list[ContextTab] = []
        self._active_context_id = ""
        self._can_create = False
        self._spans: list[tuple[int, int, str]] = []
        self._close_spans: list[tuple[int, int, str]] = []
        self._new_span: tuple[int, int] | None = None
        self.display = False

    @property
    def tabs(self) -> tuple[ContextTab, ...]:
        return tuple(self._tabs)

    @property
    def active_context_id(self) -> str:
        return self._active_context_id

    def set_tabs(
        self,
        tabs: Sequence[ContextTab],
        active_context_id: str | None,
        *,
        can_create: bool = False,
    ) -> None:
        self._tabs = [tab for tab in tabs if tab.context_id]
        self._active_context_id = str(active_context_id or "").strip()
        self._can_create = can_create
        self.display = bool(self._tabs)
        self._render_tabs()
        self.refresh(layout=True)

    def on_resize(self, event: events.Resize) -> None:
        del event
        self._render_tabs()

    def _label_cell_limits(self) -> list[int]:
        if not self._tabs:
            return []

        width = max(self.size.width, self.content_size.width, 0)
        if width <= 0:
            return [_PREFERRED_MIN_LABEL_CELLS for _ in self._tabs]

        fixed_width = 0
        for index, tab in enumerate(self._tabs, start=1):
            fixed_width += self._fixed_tab_cells(index, tab)

        if self._can_create:
            fixed_width += cell_len(f"{_TAB_LEFT} + {_TAB_RIGHT}")

        available = max(0, width - fixed_width - _MIN_RULE_CELLS)
        desired = [
            min(
                cell_len(tab.label or _fallback_label(tab.context_id, index)),
                _MAX_LABEL_CELLS,
            )
            for index, tab in enumerate(self._tabs, start=1)
        ]
        return self._allocate_label_limits(desired, available)

    def _fixed_tab_cells(self, index: int, tab: ContextTab) -> int:
        project_width = cell_len(" ●") if tab.project_color.strip() else 0
        close_width = cell_len(f" {_CLOSE_BUTTON_LABEL}") if self._show_close_buttons else 0
        return (
            cell_len(_TAB_LEFT)
            + cell_len(f" {index}")
            + project_width
            + cell_len(" ")
            + close_width
            + cell_len(" ")
            + cell_len(_TAB_RIGHT)
        )

    @property
    def _show_close_buttons(self) -> bool:
        return len(self._tabs) > 1

    def _allocate_label_limits(self, desired: list[int], available: int) -> list[int]:
        if not desired:
            return []

        if available <= 0:
            return [0 for _ in desired]

        total_desired = sum(desired)
        if available >= total_desired:
            return desired

        preferred = [min(value, _PREFERRED_MIN_LABEL_CELLS) for value in desired]
        if available >= sum(preferred):
            limits = preferred[:]
            extra = available - sum(limits)
            return self._spread_extra_label_cells(limits, desired, extra)

        minimum = [min(value, _ABSOLUTE_MIN_LABEL_CELLS) for value in desired]
        if available >= sum(minimum):
            limits = minimum[:]
            extra = available - sum(limits)
            return self._spread_extra_label_cells(limits, preferred, extra)

        limits = [0 for _ in desired]
        remaining = available
        while remaining > 0:
            changed = False
            for index, desired_cells in enumerate(desired):
                if remaining <= 0:
                    break
                if limits[index] < desired_cells:
                    limits[index] += 1
                    remaining -= 1
                    changed = True
            if not changed:
                break
        return limits

    def _spread_extra_label_cells(
        self,
        limits: list[int],
        targets: list[int],
        extra: int,
    ) -> list[int]:
        while extra > 0:
            growable = [
                index
                for index, limit in enumerate(limits)
                if limit < targets[index]
            ]
            if not growable:
                break

            share = max(1, extra // len(growable))
            for index in growable:
                if extra <= 0:
                    break
                amount = min(targets[index] - limits[index], share, extra)
                limits[index] += amount
                extra -= amount
        return limits

    def _render_tabs(self) -> None:
        line = Text()
        self._spans = []
        self._close_spans = []
        self._new_span = None
        cell_offset = 0
        label_limits = self._label_cell_limits()
        show_close_buttons = self._show_close_buttons

        for index, tab in enumerate(self._tabs, start=1):
            label = _trim_to_cells(
                tab.label or _fallback_label(tab.context_id, index),
                label_limits[index - 1] if index - 1 < len(label_limits) else _PREFERRED_MIN_LABEL_CELLS,
            )
            active = tab.context_id == self._active_context_id
            border_style = _ACTIVE_BORDER if active else _INACTIVE_BORDER
            label_style = _ACTIVE_LABEL if active else _INACTIVE_LABEL
            close_style = _ACTIVE_CLOSE if active else _INACTIVE_CLOSE
            start = cell_offset
            project_dot = tab.project_color.strip()

            segments: list[tuple[str, str]] = [
                (_TAB_LEFT, border_style),
                (f" {index}", label_style),
            ]
            if project_dot:
                segments.append((" ●", project_dot))
            for segment, style in segments:
                line.append(segment, style=style)
                cell_offset += cell_len(segment)

            line.append(f" {label}", style=label_style)
            cell_offset += cell_len(f" {label}")

            if show_close_buttons:
                line.append(" ", style=label_style)
                cell_offset += cell_len(" ")
                close_start = cell_offset
                line.append(_CLOSE_BUTTON_LABEL, style=close_style)
                cell_offset += cell_len(_CLOSE_BUTTON_LABEL)
                self._close_spans.append((close_start, cell_offset, tab.context_id))

            for segment, style in (
                (" ", label_style),
                (_TAB_RIGHT, border_style),
            ):
                line.append(segment, style=style)
                cell_offset += cell_len(segment)
            self._spans.append((start, cell_offset, tab.context_id))

        if self._can_create:
            start = cell_offset
            for segment, style in (
                (_TAB_LEFT, "#31503a"),
                (" + ", _CREATE_LABEL),
                (_TAB_RIGHT, "#31503a"),
            ):
                line.append(segment, style=style)
                cell_offset += cell_len(segment)
            self._new_span = (start, cell_offset)

        width = max(self.size.width, self.content_size.width, 0)
        remaining = max(0, width - cell_offset)
        if remaining:
            line.append(_RULE * remaining, style=_INACTIVE_BORDER)

        self.update(line)

    def on_click(self, event: events.Click) -> None:
        offset = event.get_content_offset(self)
        if offset is None:
            return
        if offset.y != 0:
            return

        x = int(offset.x)
        if self._new_span is not None:
            start, end = self._new_span
            if start <= x < end:
                event.stop()
                self.post_message(self.NewRequested(self))
                return

        for start, end, context_id in self._close_spans:
            if start <= x < end:
                event.stop()
                self._request_close_context(context_id)
                return

        for start, end, context_id in self._spans:
            if start <= x < end:
                event.stop()
                self.post_message(self.ContextSelected(context_id, self))
                return

    def _active_index(self) -> int:
        for index, tab in enumerate(self._tabs):
            if tab.context_id == self._active_context_id:
                return index
        return 0

    def _active_tab_index(self) -> int | None:
        for index, tab in enumerate(self._tabs):
            if tab.context_id == self._active_context_id:
                return index
        return None

    def _replacement_after_close(self, closed_index: int) -> str:
        remaining = [tab for index, tab in enumerate(self._tabs) if index != closed_index]
        if not remaining:
            return ""
        replacement_index = min(closed_index, len(remaining) - 1)
        return remaining[replacement_index].context_id

    def _request_close_context(self, context_id: str) -> None:
        if len(self._tabs) < 2:
            return

        for index, tab in enumerate(self._tabs):
            if tab.context_id != context_id:
                continue
            self.post_message(
                self.CloseRequested(
                    tab.context_id,
                    self._replacement_after_close(index),
                    self,
                )
            )
            return

    def _select_relative(self, delta: int) -> None:
        if len(self._tabs) < 2:
            return
        index = (self._active_index() + delta) % len(self._tabs)
        self.post_message(self.ContextSelected(self._tabs[index].context_id, self))

    def action_previous_tab(self) -> None:
        self._select_relative(-1)

    def action_next_tab(self) -> None:
        self._select_relative(1)

    def action_activate(self) -> None:
        if self._active_context_id:
            self.post_message(self.ContextSelected(self._active_context_id, self))

    def action_close_tab(self) -> None:
        if len(self._tabs) < 2:
            return

        index = self._active_tab_index()
        if index is None:
            return

        tab = self._tabs[index]
        self._request_close_context(tab.context_id)

    def action_new_chat(self) -> None:
        if self._can_create:
            self.post_message(self.NewRequested(self))
