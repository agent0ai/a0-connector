from __future__ import annotations

from typing import Any

from rich.console import RenderableType
from rich.text import Text
from textual import events
from textual.containers import VerticalScroll
from textual.widgets import Static

from agent_zero_cli.widgets.shimmer import build_dim_status, build_shimmer_text

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
        self._seq_to_widget: dict[int, Static] = {}
        self._sys_seq: int = -100
        self._intro_widget: Static | None = None

        # Shimmer state
        self._active_seq: int | None = None
        self._active_label: str = ""
        self._active_detail: str = ""
        self._shimmer_phase: float = 0.0
        self._shimmer_frame: int = 0

    def ensure_intro_banner(self) -> None:
        """Mount the Agent Zero intro banner above the first rendered message."""
        if self._intro_widget is not None:
            return

        self._intro_widget = build_agent_zero_banner_widget(classes="agent-zero-banner")
        before = self.children[0] if self.children else None
        self.mount(self._intro_widget, before=before)

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
        at_bottom = self.is_at_bottom()

        if sequence in self._seq_to_widget:
            widget = self._seq_to_widget[sequence]
            widget.update(renderable)
            # Only scroll updates if we were already at the bottom (Sticky Scrolling)
            if scroll and at_bottom:
                widget.scroll_visible(animate=False)
        else:
            widget = Static(renderable)
            self._seq_to_widget[sequence] = widget
            self.mount(widget)
            if scroll:
                widget.scroll_visible(animate=False)

    def set_active_status(self, seq: int, label: str, detail: str) -> None:
        """Set a new active status line, dimming the previous one if necessary."""
        if self._active_seq is not None and self._active_seq != seq:
            self.dim_active_status()

        self._active_seq = seq
        self._active_label = label
        self._active_detail = detail
        self.refresh_active_status()

    def dim_active_status(self) -> None:
        """Freeze and dim the current active status line."""
        if self._active_seq is not None:
            content = build_dim_status(self._active_label, self._active_detail)
            self.append_or_update(self._active_seq, content)

        self.stop_active_status()

    def stop_active_status(self) -> None:
        """Clear the active status tracking without overwriting the current content."""
        self._active_seq = None
        self._active_label = ""
        self._active_detail = ""

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
        content = build_shimmer_text(
            self._active_label,
            self._active_detail,
            self._shimmer_phase,
            self._shimmer_frame,
        )
        self.append_or_update(self._active_seq, content, scroll=False)

    def clear(self) -> None:
        """Clear the timeline and reset the tracking map."""
        self._seq_to_widget.clear()
        self._intro_widget = None
        self._active_seq = None
        for child in self.children:
            child.remove()


def build_agent_zero_banner_widget(*, id: str | None = None, classes: str = "agent-zero-banner") -> Static:
    """Build the shared Agent Zero banner used by splash and chat views."""
    return AgentZeroBanner(id=id, classes=classes)
