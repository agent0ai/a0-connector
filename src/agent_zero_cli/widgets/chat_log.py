from __future__ import annotations

from typing import Any

from rich.console import RenderableType
from textual.containers import VerticalScroll
from textual.widgets import Static

from agent_zero_cli.widgets.shimmer import build_dim_status, build_shimmer_text


class ChatLog(VerticalScroll):
    """A log widget that updates its children based on sequence tracking."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._seq_to_widget: dict[int, Static] = {}
        self._sys_seq: int = -100

        # Shimmer state
        self._active_seq: int | None = None
        self._active_label: str = ""
        self._active_detail: str = ""
        self._shimmer_phase: float = 0.0
        self._shimmer_frame: int = 0

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
        self._active_seq = None
        for child in self.children:
            child.remove()

