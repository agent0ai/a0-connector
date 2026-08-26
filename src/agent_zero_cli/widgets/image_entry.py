"""Focusable transcript image surface with explicit renderer ownership."""

from __future__ import annotations

from typing import Literal

from textual import events, messages
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.timer import Timer
from textual.widget import Widget
from textual.widgets import Static

from agent_zero_cli.image_render import CellBox, ImageRenderer
from agent_zero_cli.image_store import ImageAsset
from agent_zero_cli.media_refs import ImageReference


_ImageEntryState = Literal[
    "pending",
    "loading",
    "rendered",
    "unavailable",
    "disabled",
]


class ImageEntry(Vertical):
    """Own one lazily-loaded image and its currently mounted terminal surface."""

    can_focus = True
    BINDINGS = [
        Binding("enter", "toggle", "Toggle image size", show=False),
        Binding("space", "toggle", "Toggle image size", show=False),
    ]

    class LoadRequested(Message):
        """Request that application orchestration load one image generation."""

        def __init__(
            self,
            entry: "ImageEntry",
            reference: ImageReference,
            generation: int,
        ) -> None:
            super().__init__()
            self.entry = entry
            self.reference = reference
            self.generation = generation

    class SurfaceChanged(Message):
        """Notify the owning transcript that an image surface changed layout."""

    def __init__(
        self,
        reference: ImageReference,
        renderer: ImageRenderer,
        *,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(id=id, classes=classes)
        self._reference = reference
        self._renderer = renderer
        self._state: _ImageEntryState
        self._reason = ""
        if renderer.mode == "off":
            self._state = "disabled"
            self._reason = "images disabled"
        elif reference.source == "unavailable":
            self._state = "unavailable"
            self._reason = reference.value or "image unavailable"
        else:
            self._state = "pending"
        self._generation = 0
        self._expanded = True
        self._asset: ImageAsset | None = None
        self._surface: Widget | None = None
        self._surface_box: CellBox | None = None
        self._resize_timer: Timer | None = None
        self._caption = Static(markup=False, classes="image-caption")
        self._placeholder = Static(markup=False, classes="image-placeholder")
        self._surface_host = Vertical(classes="image-surface-host")
        self._refresh_labels()

    @property
    def reference(self) -> ImageReference:
        return self._reference

    @property
    def state(self) -> _ImageEntryState:
        return self._state

    @property
    def expanded(self) -> bool:
        return self._expanded

    @property
    def generation(self) -> int:
        return self._generation

    def compose(self) -> ComposeResult:
        yield self._caption
        yield self._placeholder
        yield self._surface_host

    def request_load(self) -> None:
        """Post one load request when this entry is eligible to fetch."""
        if self._state != "pending":
            return
        self._generation += 1
        self._state = "loading"
        self._refresh_labels()
        self.post_message(self.LoadRequested(self, self.reference, self._generation))

    def set_asset(self, generation: int, asset: ImageAsset) -> None:
        """Take ownership of a current loaded asset and mount its surface."""
        if generation != self._generation or self._state != "loading":
            asset.close()
            return

        self._asset = asset
        self._state = "rendered"
        self._reason = ""
        self._refresh_labels()
        self._remount_surface()

    def set_unavailable(self, generation: int, reason: str) -> None:
        """Replace a current load or render failure with a stable placeholder."""
        if generation != self._generation:
            return
        self._cleanup_surface()
        if self._asset is not None:
            self._asset.close()
            self._asset = None
        self._state = "unavailable"
        self._reason = str(reason or "image unavailable")
        self._refresh_labels()
        self.refresh(layout=True)

    def release_surface(self) -> None:
        """Release renderer and Pillow resources for an off-screen image."""
        active_generation = self._state in {"loading", "rendered"}
        self._cleanup_surface()
        if self._asset is not None:
            self._asset.close()
            self._asset = None
        if active_generation:
            self._generation += 1
            self._state = "pending"
            self._refresh_labels()
            self.refresh(layout=True)

    def redraw_surface(self) -> None:
        """Ask the renderer to restore a mounted non-retained native surface."""
        if self._state == "rendered":
            self._renderer.redraw_widget(self._surface)

    def copy_text(self) -> str:
        """Return only the semantic transcript representation of the image."""
        return self.reference.copy_text

    def action_toggle(self) -> None:
        if self._state != "rendered" or self._asset is None:
            return
        self._expanded = not self._expanded
        self._refresh_labels()
        self._remount_surface()
        self.refresh(layout=True)
        self.scroll_visible(animate=False)

    def on_click(self, event: events.Click) -> None:
        if self.text_selection is not None:
            return
        if self._state != "rendered":
            return
        self.action_toggle()
        event.stop()

    def on_mouse_down(self, event: events.MouseDown) -> None:
        del event
        self.focus()

    def on_resize(self, event: events.Resize) -> None:
        del event
        if self._resize_timer is not None:
            self._resize_timer.stop()
        self._resize_timer = self.set_timer(0.1, self._on_resize_timer)

    async def on_prune(self, event: messages.Prune) -> None:
        """Release native graphics before Textual prunes descendant widgets."""
        del event
        self._cancel_resize_timer()
        self.release_surface()

    def on_unmount(self) -> None:
        self._cancel_resize_timer()

    def _cancel_resize_timer(self) -> None:
        if self._resize_timer is not None:
            self._resize_timer.stop()
            self._resize_timer = None

    def _remount_surface(self) -> None:
        asset = self._asset
        if self._state != "rendered" or asset is None or not self.is_mounted:
            return

        box = self._renderer.fit_box(
            (asset.width, asset.height),
            available_columns=self._available_columns(),
            expanded=self._expanded,
        )
        if self._surface is not None and self._surface_box == box:
            return

        try:
            self._cleanup_surface()
            surface = self._renderer.create_widget(asset.image, box)
        except Exception:
            self.set_unavailable(self._generation, "renderer failed")
            return

        self._surface = surface
        self._surface_box = box
        self._surface_host.mount(surface)
        self.refresh(layout=True)
        self.post_message(self.SurfaceChanged())

    def _on_resize_timer(self) -> None:
        self._resize_timer = None
        self._remount_surface()

    def _cleanup_surface(self) -> None:
        surface = self._surface
        self._surface = None
        self._surface_box = None
        if surface is not None:
            self._renderer.cleanup_widget(surface)

    def _available_columns(self) -> int:
        widths = (
            self.content_region.width,
            self.size.width,
            self._surface_host.content_region.width,
        )
        return next((width for width in widths if width > 0), 1)

    def _refresh_labels(self) -> None:
        toggle = "collapse" if self._expanded else "expand"
        self._caption.update(
            f"{self.reference.caption} — Enter/Space to {toggle}",
            layout=True,
        )
        if self._state == "rendered":
            placeholder = ""
            visible = False
        elif self._state == "loading":
            placeholder = "Loading image…"
            visible = True
        elif self._state in {"unavailable", "disabled"}:
            placeholder = f"{self.reference.caption}: {self._reason}"
            visible = True
        else:
            placeholder = "Image ready to load"
            visible = True
        self._placeholder.update(placeholder, layout=True)
        self._placeholder.display = visible


__all__ = ["ImageEntry"]
