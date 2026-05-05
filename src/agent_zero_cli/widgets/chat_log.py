from __future__ import annotations

from typing import Any

from rich.console import Console, Group, RenderableType
from rich.padding import Padding
from rich.segment import Segment
from rich.text import Text
from textual import events
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.content import Content
from textual.style import Style
from textual.widgets import Static

from agent_zero_cli.widgets.shimmer import build_dim_status, build_shimmer_text


_STATUS_HISTORY_PADDING = (0, 0, 0, 2)
_STATUS_BODY_PADDING = (1, 0, 0, 4)
_STATUS_THOUGHT_LIMIT = 6
_REDACTED_ARG_KEYS = {
    "code",
    "content",
    "html",
    "markdown",
    "prompt",
    "stderr",
    "stdout",
    "text",
}
_SKIP_TOOL_ARG_KEYS = {"text"}
_SKIP_META_KEYS = {
    "headline",
    "step",
    "thoughts",
    "tool_args",
    "tool_name",
    "reasoning",
}

_AGENT_ZERO_BANNER = """ █████╗   ██████╗ ███████╗███╗   ██╗████████╗   ███████╗███████╗██████╗  ██████╗
██╔══██╗ ██╔════╝ ██╔════╝████╗  ██║╚══██╔══╝   ╚══███╔╝██╔════╝██╔══██╗██╔═══██╗
███████║ ██║  ███╗█████╗  ██╔██╗ ██║   ██║        ███╔╝ █████╗  ██████╔╝██║   ██║
██╔══██║ ██║   ██║██╔══╝  ██║╚██╗██║   ██║       ███╔╝  ██╔══╝  ██╔══██╗██║   ██║
██║  ██║ ╚██████╔╝███████╗██║ ╚████║   ██║      ███████╗███████╗██║  ██║╚██████╔╝
╚═╝  ╚═╝  ╚═════╝ ╚══════╝╚═╝  ╚═══╝   ╚═╝      ╚══════╝╚══════╝╚═╝  ╚═╝ ╚═════╝"""
_AGENT_ZERO_BANNER_COMPACT = "Agent Zero"
_AGENT_ZERO_BANNER_TINY = "A0"


def _banner_width(banner: str) -> int:
    return max(len(line) for line in banner.splitlines() if line)


_BANNER_VARIANTS: tuple[str, ...] = (
    _AGENT_ZERO_BANNER,
    _AGENT_ZERO_BANNER_COMPACT,
    _AGENT_ZERO_BANNER_TINY,
)


def _select_agent_zero_banner(available_width: int) -> str:
    width = max(available_width, 0)
    for banner in _BANNER_VARIANTS:
        if width >= _banner_width(banner):
            return banner
    return _AGENT_ZERO_BANNER_TINY


def _build_banner_text(banner: str) -> Text:
    text = Text(banner, style="#00b4ff")
    text.no_wrap = True
    text.overflow = "ignore"
    return text


def _normalize_status_text(value: str) -> str:
    return " ".join(value.split())


def _truncate_status_text(value: str, limit: int = 120) -> str:
    normalized = _normalize_status_text(value)
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[: max(limit - 1, 0)].rstrip()}…"


def _count_label(count: int, singular: str, plural: str | None = None) -> str:
    noun = singular if count == 1 else (plural or f"{singular}s")
    return f"{count} {noun}"


def _hidden_payload_summary(value: Any) -> str:
    if isinstance(value, str):
        return f"{len(_normalize_status_text(value))} chars hidden"
    if isinstance(value, dict):
        return f"{_count_label(len(value), 'key')} hidden"
    if isinstance(value, list):
        return f"{_count_label(len(value), 'item')} hidden"
    return "hidden"


def _summarize_status_value(
    key: str,
    value: Any,
    *,
    redact_strings: bool = False,
) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        normalized = _normalize_status_text(value)
        if not normalized:
            return None
        if redact_strings:
            return _hidden_payload_summary(normalized)
        return _truncate_status_text(normalized)
    if isinstance(value, dict):
        return _hidden_payload_summary(value) if key in _REDACTED_ARG_KEYS else _count_label(len(value), "key")
    if isinstance(value, list):
        return _hidden_payload_summary(value) if key in _REDACTED_ARG_KEYS else _count_label(len(value), "item")
    return _truncate_status_text(str(value))


def sanitize_status_meta(
    meta: dict[str, Any] | None,
) -> tuple[list[tuple[str, str]], list[str], int]:
    """Extract concise KVP rows and thought bullets from streamed status metadata."""
    if not isinstance(meta, dict):
        return [], [], 0

    rows: list[tuple[str, str]] = []
    consumed: set[str] = set()

    headline = _summarize_status_value("headline", meta.get("headline"))
    if headline:
        rows.append(("headline", headline))
        consumed.add("headline")

    consumed.add("step")

    tool_name = _summarize_status_value("tool_name", meta.get("tool_name"))
    if tool_name:
        rows.append(("tool", tool_name))
        consumed.add("tool_name")

    tool_args = meta.get("tool_args")
    if isinstance(tool_args, dict):
        consumed.add("tool_args")
        for arg_name, arg_value in tool_args.items():
            if str(arg_name) in _SKIP_TOOL_ARG_KEYS:
                continue
            summary = _summarize_status_value(
                str(arg_name),
                arg_value,
                redact_strings=str(arg_name) in _REDACTED_ARG_KEYS,
            )
            if summary:
                rows.append((f"arg.{arg_name}", summary))

    for key in sorted(meta):
        if key in consumed or key in _SKIP_META_KEYS:
            continue
        summary = _summarize_status_value(str(key), meta.get(key))
        if summary:
            rows.append((str(key), summary))

    thought_items = meta.get("thoughts")
    thoughts: list[str] = []
    hidden_thoughts = 0
    if isinstance(thought_items, list):
        for item in thought_items:
            if not isinstance(item, str):
                continue
            normalized = _normalize_status_text(item)
            if not normalized:
                continue
            if len(thoughts) < _STATUS_THOUGHT_LIMIT:
                thoughts.append(_truncate_status_text(normalized, limit=180))
            else:
                hidden_thoughts += 1

    return rows, thoughts, hidden_thoughts


def _build_status_body(
    meta: dict[str, Any] | None,
) -> Padding | None:
    rows, thoughts, hidden_thoughts = sanitize_status_meta(meta)
    if not rows and not thoughts and not hidden_thoughts:
        return None

    renderables: list[RenderableType] = []
    for key, value in rows:
        line = Text()
        line.append(f"{key}:", style="#7f8c98")
        line.append(f" {value}", style="#d8e1ea")
        renderables.append(line)

    if thoughts:
        thoughts_label = Text()
        thoughts_label.append("thoughts:", style="#7f8c98")
        renderables.append(thoughts_label)
        for thought in thoughts:
            bullet = Text()
            bullet.append("  • ", style="#7f8c98")
            bullet.append(thought, style="#d8e1ea")
            renderables.append(bullet)

    if hidden_thoughts:
        more = Text()
        more.append("  … ", style="#7f8c98")
        more.append(f"{hidden_thoughts} more hidden", style="#7f8c98")
        renderables.append(more)

    return Padding(Group(*renderables), _STATUS_BODY_PADDING)


def _segments_to_content(
    segments: list[Segment],
    *,
    ansi_theme: object | None = None,
) -> Content:
    parts: list[Content | str | tuple[str, Style]] = []
    for text, rich_style, control in segments:
        if control or not text:
            continue
        style = Style.from_rich_style(rich_style, ansi_theme) if rich_style else ""
        parts.append((text, style) if style else text)
    if not parts:
        return Content()
    return Content.assemble(*parts, strip_control_codes=False).simplify()


def _renderable_to_content(widget: Static, renderable: RenderableType) -> Content:
    if isinstance(renderable, Content):
        return renderable
    if isinstance(renderable, Text):
        return Content.from_rich_text(renderable)
    if isinstance(renderable, str):
        return Content.from_text(renderable)

    ansi_theme = None
    # Child width already accounts for scrollbars; app width can clip panel borders.
    width = max(widget.content_region.width, widget.size.width, 0)
    try:
        app = widget.app
        ansi_theme = app.ansi_theme
    except Exception:
        app = None

    if width <= 1:
        try:
            parent = widget.parent
            gutter = parent.scrollbar_gutter if parent is not None else None
            gutter_right = gutter.right if gutter is not None else 0
            parent_width = (
                max(parent.content_region.width, parent.size.width)
                if parent is not None
                else 0
            )
            width = max(width, parent_width - gutter_right)
        except Exception:
            pass

    if width <= 1 and app is not None:
        width = app.size.width

    width = max(width, 1)

    console = Console(
        width=width,
        record=True,
        force_terminal=True,
        legacy_windows=False,
        color_system="truecolor",
        markup=False,
    )
    lines = [
        _segments_to_content(line, ansi_theme=ansi_theme).rstrip_end(width)
        for line in console.render_lines(
            renderable,
            console.options.update(width=width, height=None, highlight=False),
            pad=False,
            new_lines=False,
        )
    ]
    if not lines:
        return Content()
    return Content("\n").join(lines).simplify()


class SelectableStatic(Static):
    """Static widget that keeps the current rich transcript renderable selectable."""

    can_focus = True

    def __init__(
        self,
        content: RenderableType = "",
        *,
        expand: bool = False,
        shrink: bool = False,
        markup: bool = True,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
        disabled: bool = False,
    ) -> None:
        super().__init__(
            "",
            expand=expand,
            shrink=shrink,
            markup=markup,
            name=name,
            id=id,
            classes=classes,
            disabled=disabled,
        )
        self._renderable = content

    def render(self) -> Content:
        return _renderable_to_content(self, self._renderable)

    def update(self, content: RenderableType = "", *, layout: bool = True) -> None:
        self._renderable = content
        self.refresh(layout=layout)

    def on_mouse_down(self, event: events.MouseDown) -> None:
        del event
        self.focus()


class StatusEntry(SelectableStatic):
    """Interactive status row with a shimmer header and expandable KVP body."""

    can_focus = True
    BINDINGS = [
        Binding("enter", "toggle", "Toggle status details", show=False),
        Binding("space", "toggle", "Toggle status details", show=False),
    ]

    def __init__(self, *, id: str | None = None, classes: str = "status-entry") -> None:
        super().__init__("", id=id, classes=classes)
        self._label = ""
        self._detail = ""
        self._meta: dict[str, Any] = {}
        self._active = False
        self._expanded = False
        self._phase = 0.0
        self._frame = 0

    def set_status(
        self,
        label: str,
        detail: str,
        meta: dict[str, Any] | None,
        *,
        active: bool,
        shimmer_phase: float = 0.0,
        shimmer_frame: int = 0,
    ) -> None:
        self._label = label
        self._detail = detail
        self._meta = meta if isinstance(meta, dict) else {}
        self._active = active
        self._phase = shimmer_phase
        self._frame = shimmer_frame
        if not self.has_details:
            self._expanded = False
        self._refresh_content(layout=True)

    @property
    def has_details(self) -> bool:
        body = _build_status_body(self._meta)
        return body is not None

    def action_toggle(self) -> None:
        if not self.has_details:
            return
        self._expanded = not self._expanded
        self._refresh_content(layout=True)
        self.scroll_visible(animate=False)

    def on_click(self, event: events.Click) -> None:
        if self.text_selection is not None:
            return
        if not self.has_details:
            return
        self.action_toggle()
        event.stop()

    def _refresh_content(self, *, layout: bool) -> None:
        arrow = "▼" if self._expanded and self.has_details else "▶" if self.has_details else "•"
        header = Text()
        header.append(f"{arrow} ", style="#7f8c98")
        status_text = (
            build_shimmer_text(self._label, self._detail, self._phase, self._frame)
            if self._active
            else build_dim_status(self._label, self._detail)
        )
        header.append_text(status_text)

        renderables: list[RenderableType] = [header]
        if self._expanded:
            body = _build_status_body(self._meta)
            if body is not None:
                renderables.append(body)

        self.update(Padding(Group(*renderables), _STATUS_HISTORY_PADDING), layout=layout)


class CodeEntry(SelectableStatic):
    """Interactive code row with a status header and expandable rich body."""

    can_focus = True
    BINDINGS = [
        Binding("enter", "toggle", "Toggle code details", show=False),
        Binding("space", "toggle", "Toggle code details", show=False),
    ]

    def __init__(self, *, id: str | None = None, classes: str = "status-entry code-entry") -> None:
        super().__init__("", id=id, classes=classes)
        self._label = ""
        self._detail = ""
        self._body: RenderableType | None = None
        self._active = False
        self._expanded = False
        self._phase = 0.0
        self._frame = 0

    @property
    def has_details(self) -> bool:
        return self._body is not None

    def set_code(
        self,
        label: str,
        detail: str,
        body: RenderableType | None,
        *,
        active: bool,
        shimmer_phase: float = 0.0,
        shimmer_frame: int = 0,
        default_expanded: bool = True,
    ) -> None:
        had_details = self.has_details
        self._label = label
        self._detail = detail
        self._body = body
        self._active = active
        self._phase = shimmer_phase
        self._frame = shimmer_frame
        if not self.has_details:
            self._expanded = False
        elif default_expanded and not had_details:
            self._expanded = True
        self._refresh_content(layout=True)

    def action_toggle(self) -> None:
        if not self.has_details:
            return
        self._expanded = not self._expanded
        self._refresh_content(layout=True)
        self.scroll_visible(animate=False)

    def on_click(self, event: events.Click) -> None:
        if self.text_selection is not None:
            return
        if not self.has_details:
            return
        self.action_toggle()
        event.stop()

    def _refresh_content(self, *, layout: bool) -> None:
        arrow = "▼" if self._expanded and self.has_details else "▶" if self.has_details else "•"
        header = Text()
        header.append(f"{arrow} ", style="#7f8c98")
        status_text = (
            build_shimmer_text(self._label, self._detail, self._phase, self._frame)
            if self._active
            else build_dim_status(self._label, self._detail)
        )
        header.append_text(status_text)

        renderables: list[RenderableType] = [header]
        if self._expanded and self._body is not None:
            renderables.append(Padding(self._body, _STATUS_BODY_PADDING))

        self.update(Padding(Group(*renderables), _STATUS_HISTORY_PADDING), layout=layout)


class AgentZeroBanner(Static):
    """Responsive Agent Zero banner that keeps a readable shape while resizing."""

    def __init__(self, *, id: str | None = None, classes: str = "agent-zero-banner") -> None:
        super().__init__("", id=id, classes=classes)
        self._current_banner = ""

    def on_mount(self) -> None:
        self.call_after_refresh(self._sync_banner)

    def on_resize(self, event: events.Resize) -> None:
        self._sync_banner()

    def _sync_banner(self) -> None:
        selected = _select_agent_zero_banner(self.size.width)
        if selected == self._current_banner:
            return
        self._current_banner = selected
        self.update(_build_banner_text(selected))


class ChatLog(VerticalScroll):
    """A log widget that updates its children based on sequence tracking."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._seq_to_widget: dict[int, SelectableStatic] = {}
        self._sys_seq: int = -100
        self._intro_widget: Static | None = None
        self._workspace_widget: Static | None = None
        self._local_workspace = ""
        self._remote_workspace = ""
        self._auto_follow = True

        # Shimmer state
        self._active_seq: int | None = None
        self._active_label: str = ""
        self._active_detail: str = ""
        self._active_meta: dict[str, Any] = {}
        self._shimmer_phase: float = 0.0
        self._shimmer_frame: int = 0

    def _pause_auto_follow_if_scrolled_up(self, previous_scroll_y: float) -> None:
        if self.scroll_y < previous_scroll_y:
            self._auto_follow = False

    def _resume_auto_follow_if_at_bottom(self) -> None:
        if self.is_at_bottom():
            self._auto_follow = True

    def _should_auto_scroll(self, scroll: bool) -> bool:
        if not scroll:
            return False
        if not self._auto_follow:
            self._resume_auto_follow_if_at_bottom()
        return self._auto_follow

    def _schedule_scroll_end(self) -> None:
        # Defer until after refresh so long/tall renderables are measured first.
        self.call_after_refresh(self._scroll_end_if_auto_follow)

    def _scroll_end_if_auto_follow(self) -> None:
        if self._auto_follow:
            self.scroll_end(animate=False)

    def _on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        previous_scroll_y = self.scroll_y
        super()._on_mouse_scroll_up(event)
        self._pause_auto_follow_if_scrolled_up(previous_scroll_y)

    def _on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        super()._on_mouse_scroll_down(event)
        self._resume_auto_follow_if_at_bottom()

    def action_scroll_up(self) -> None:
        previous_scroll_y = self.scroll_y
        super().action_scroll_up()
        self._pause_auto_follow_if_scrolled_up(previous_scroll_y)

    def action_page_up(self) -> None:
        previous_scroll_y = self.scroll_y
        super().action_page_up()
        self._pause_auto_follow_if_scrolled_up(previous_scroll_y)

    def action_scroll_home(self) -> None:
        previous_scroll_y = self.scroll_y
        super().action_scroll_home()
        self._pause_auto_follow_if_scrolled_up(previous_scroll_y)

    def action_scroll_down(self) -> None:
        super().action_scroll_down()
        self._resume_auto_follow_if_at_bottom()

    def action_page_down(self) -> None:
        super().action_page_down()
        self._resume_auto_follow_if_at_bottom()

    def action_scroll_end(self) -> None:
        super().action_scroll_end()
        self._resume_auto_follow_if_at_bottom()

    def ensure_intro_banner(self) -> None:
        """Mount the Agent Zero intro banner above the first rendered message."""
        if self._intro_widget is not None:
            self._sync_workspace_widget()
            return

        self._intro_widget = build_agent_zero_banner_widget(classes="agent-zero-banner")
        before = self.children[0] if self.children else None
        self.mount(self._intro_widget, before=before)
        self._sync_workspace_widget()

    def set_workspace(self, *, local_workspace: str = "", remote_workspace: str = "") -> None:
        self._local_workspace = local_workspace.strip()
        self._remote_workspace = remote_workspace.strip()
        self._sync_workspace_widget()

    def _workspace_line(self) -> str:
        parts: list[str] = []
        if self._local_workspace:
            parts.append(f"Local {self._local_workspace}")
        if self._remote_workspace:
            parts.append(f"Remote {self._remote_workspace}")
        return "  |  ".join(parts)

    def _sync_workspace_widget(self) -> None:
        line = self._workspace_line()
        if not line:
            if self._workspace_widget is not None:
                self._workspace_widget.remove()
                self._workspace_widget = None
            return

        if self._workspace_widget is None:
            self._workspace_widget = Static(
                Text(line, style="#7f8c98"),
                classes="workspace-context",
            )
            if self._intro_widget is not None:
                before = self.children[1] if len(self.children) > 1 else None
            else:
                before = self.children[0] if self.children else None
            self.mount(self._workspace_widget, before=before)
            return

        self._workspace_widget.update(Text(line, style="#7f8c98"))

    def write(self, renderable: RenderableType) -> None:
        """Write a new un-updatable message using an internal sequence ID."""
        self.append_or_update(self._sys_seq, renderable, scroll=True)
        self._sys_seq -= 1

    def is_at_bottom(self) -> bool:
        """Check if the view is currently at the bottom (or content too small to scroll)."""
        if self.virtual_size.height <= self.size.height:
            return True
        return self.scroll_y >= self.max_scroll_y - 1

    def append_or_update(
        self, sequence: int, renderable: RenderableType, scroll: bool = True
    ) -> None:
        """Add a new renderable or update an existing one bounded to `sequence`.

        Args:
            sequence: The unique ID identifying this block.
            renderable: The rich renderable to display.
            scroll: Whether to automatically scroll to the element.
        """
        should_scroll = self._should_auto_scroll(scroll)
        widget = self._seq_to_widget.get(sequence)
        if widget is not None and widget.__class__ is not SelectableStatic:
            widget.remove()
            widget = None

        if isinstance(widget, SelectableStatic) and widget.__class__ is SelectableStatic:
            widget.update(renderable)
            if should_scroll:
                self._schedule_scroll_end()
        else:
            widget = SelectableStatic(renderable)
            self._seq_to_widget[sequence] = widget
            self.mount(widget)
            if should_scroll:
                self._schedule_scroll_end()

    def append_or_update_status(
        self,
        sequence: int,
        label: str,
        detail: str,
        meta: dict[str, Any] | None = None,
        *,
        active: bool = False,
        scroll: bool = True,
    ) -> None:
        """Add or update a structured status widget bounded to `sequence`."""
        should_scroll = self._should_auto_scroll(scroll)
        widget = self._seq_to_widget.get(sequence)
        if widget is not None and not isinstance(widget, StatusEntry):
            widget.remove()
            widget = None

        if not isinstance(widget, StatusEntry):
            widget = StatusEntry()
            self._seq_to_widget[sequence] = widget
            self.mount(widget)

        widget.set_status(
            label,
            detail,
            meta,
            active=active,
            shimmer_phase=self._shimmer_phase,
            shimmer_frame=self._shimmer_frame,
        )
        if should_scroll:
            self._schedule_scroll_end()

    def append_or_update_code(
        self,
        sequence: int,
        label: str,
        detail: str,
        body: RenderableType | None,
        *,
        active: bool = False,
        scroll: bool = True,
    ) -> None:
        """Add or update an expandable code widget bounded to `sequence`."""
        should_scroll = self._should_auto_scroll(scroll)
        widget = self._seq_to_widget.get(sequence)
        if widget is not None and not isinstance(widget, CodeEntry):
            widget.remove()
            widget = None

        if not isinstance(widget, CodeEntry):
            widget = CodeEntry()
            self._seq_to_widget[sequence] = widget
            self.mount(widget)

        widget.set_code(
            label,
            detail,
            body,
            active=active,
            shimmer_phase=self._shimmer_phase,
            shimmer_frame=self._shimmer_frame,
        )
        if should_scroll:
            self._schedule_scroll_end()

    def set_active_status(
        self,
        seq: int,
        label: str,
        detail: str,
        meta: dict[str, Any] | None = None,
    ) -> None:
        """Set a new active status line, dimming the previous one if necessary."""
        if self._active_seq is not None and self._active_seq != seq:
            self.dim_active_status()

        self._active_seq = seq
        self._active_label = label
        self._active_detail = detail
        self._active_meta = meta if isinstance(meta, dict) else {}
        self.refresh_active_status()

    def dim_active_status(self) -> None:
        """Freeze and dim the current active status line."""
        if self._active_seq is not None:
            self.append_or_update_status(
                self._active_seq,
                self._active_label,
                self._active_detail,
                self._active_meta,
                active=False,
            )

        self.stop_active_status()

    def stop_active_status(self) -> None:
        """Clear the active status tracking without overwriting the current content."""
        self._active_seq = None
        self._active_label = ""
        self._active_detail = ""
        self._active_meta = {}

    def advance_shimmer(self) -> None:
        """Advance the shimmer animation state and refresh the active line."""
        if self._active_seq is None:
            return
        self._shimmer_phase = (self._shimmer_phase + 0.1) % 1.0
        self._shimmer_frame = (self._shimmer_frame + 1) % 10
        self.refresh_active_status()

    def refresh_active_status(self) -> None:
        """Re-render the active status line with current animation state."""
        if self._active_seq is None:
            return
        self.append_or_update_status(
            self._active_seq,
            self._active_label,
            self._active_detail,
            self._active_meta,
            active=True,
            scroll=False,
        )

    def clear(self) -> None:
        """Clear the timeline and reset the tracking map."""
        self._seq_to_widget.clear()
        self._intro_widget = None
        self._workspace_widget = None
        self._active_seq = None
        self._active_meta = {}
        self._auto_follow = True
        for child in self.children:
            child.remove()


def build_agent_zero_banner_widget(*, id: str | None = None, classes: str = "agent-zero-banner") -> Static:
    """Build the shared Agent Zero banner used by splash and chat views."""
    return AgentZeroBanner(id=id, classes=classes)
