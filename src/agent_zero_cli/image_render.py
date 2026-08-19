"""Interactive terminal-image renderer selection and widget lifecycle helpers."""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Literal

if TYPE_CHECKING:
    from PIL import Image as PILImage
    from textual.widget import Widget


ImageMode = Literal["tgp", "sixel", "halfcell", "off"]
VALID_REQUESTED_MODES = frozenset({"auto", "tgp", "sixel", "halfcell", "off"})
_TERM_FEATURE_PREFIX = re.compile(r"[A-Za-z0-9]*")
_TERM_FEATURE_TOKEN = re.compile(r"[A-Z][a-z]*(?:[0-9]+)?")


@dataclass(frozen=True)
class CellBox:
    """A terminal-image size expressed in character cells."""

    columns: int
    rows: int


THUMBNAIL_MAX = CellBox(36, 12)
EXPANDED_MAX = CellBox(96, 32)


@dataclass(frozen=True)
class RendererSelection:
    """The selected terminal image protocol and an optional user-facing notice."""

    mode: ImageMode
    notice: str = ""


def select_image_mode(
    requested: str,
    *,
    is_tty: bool,
    tgp_supported: bool,
    sixel_supported: bool,
    force_halfcell: bool = False,
) -> RendererSelection:
    """Choose a renderer without importing terminal-image widgets."""
    normalized = str(requested or "auto").strip().lower()
    invalid = normalized not in VALID_REQUESTED_MODES
    if invalid:
        normalized = "auto"
    if normalized == "off":
        return RendererSelection("off")
    if force_halfcell or normalized == "halfcell":
        notice = "Invalid A0_CLI_IMAGE_MODE; using half-cell images." if invalid else ""
        return RendererSelection("halfcell", notice)
    if not is_tty:
        tgp_supported = sixel_supported = False
    if normalized == "tgp":
        return RendererSelection("tgp") if tgp_supported else RendererSelection(
            "off", "TGP images are unavailable; image rendering disabled."
        )
    if normalized == "sixel":
        return RendererSelection("sixel") if sixel_supported else RendererSelection(
            "off", "Sixel images are unavailable; image rendering disabled."
        )
    mode: ImageMode = "tgp" if tgp_supported else "sixel" if sixel_supported else "off"
    notice = "Invalid A0_CLI_IMAGE_MODE; using automatic image detection." if invalid else ""
    return RendererSelection(mode, notice)


WidgetFactory = Callable[["PILImage.Image"], "Widget"]


class ImageRenderer:
    """Create appropriately-sized image widgets without leaking backend details."""

    def __init__(
        self,
        selection: RendererSelection,
        *,
        cell_pixels: tuple[int, int],
        widget_factory: WidgetFactory | None = None,
    ) -> None:
        self.selection = selection
        self.cell_pixels = (max(1, cell_pixels[0]), max(1, cell_pixels[1]))
        self._widget_factory = widget_factory

    @property
    def mode(self) -> ImageMode:
        return self.selection.mode

    @property
    def notice(self) -> str:
        return self.selection.notice

    @property
    def max_surface_pixels(self) -> tuple[int, int]:
        """Return the largest surface each rendering backend may allocate."""
        cell_width, cell_height = self.cell_pixels
        if self.mode in {"tgp", "sixel"}:
            return EXPANDED_MAX.columns * cell_width, EXPANDED_MAX.rows * cell_height
        if self.mode == "halfcell":
            return 96, 64
        return 1, 1

    @classmethod
    def disabled(cls) -> "ImageRenderer":
        """Create an off renderer without loading ``textual-image``."""
        return cls(RendererSelection("off"), cell_pixels=(1, 1))

    @classmethod
    def for_test(
        cls,
        *,
        mode: ImageMode = "halfcell",
        cell_pixels: tuple[int, int] = (10, 20),
    ) -> "ImageRenderer":
        """Create a library-free renderer for deterministic sizing tests."""
        return cls(RendererSelection(mode), cell_pixels=cell_pixels)

    def fit_box(
        self,
        image_size: tuple[int, int],
        *,
        available_columns: int,
        expanded: bool,
    ) -> CellBox:
        """Fit an image completely inside the requested terminal-cell bounds."""
        image_width, image_height = image_size
        cell_width, cell_height = self.cell_pixels
        max_box = EXPANDED_MAX if expanded else THUMBNAIL_MAX
        max_columns = max(1, min(max_box.columns, available_columns))
        scale = min(
            max_columns * cell_width / image_width,
            max_box.rows * cell_height / image_height,
        )
        columns = max(1, min(max_columns, round(image_width * scale / cell_width)))
        rows = max(1, min(max_box.rows, round(image_height * scale / cell_height)))
        return CellBox(columns, rows)

    def create_widget(
        self,
        image: "PILImage.Image",
        box: CellBox,
    ) -> "Widget":
        """Create a native widget sized to a previously fitted cell box."""
        if self._widget_factory is None:
            raise RuntimeError("Image rendering is unavailable.")
        return self._create_sized_widget(self._widget_factory, image, box)

    @staticmethod
    def _create_sized_widget(
        factory: WidgetFactory,
        image: "PILImage.Image",
        box: CellBox,
    ) -> "Widget":
        widget = factory(image)
        widget.styles.width = box.columns
        widget.styles.height = box.rows
        return widget

    def cleanup_widget(self, widget: "Widget | None") -> None:
        """Release terminal graphics before asking Textual to remove the widget."""
        if widget is None:
            return
        try:
            setattr(widget, "image", None)
        except Exception:
            pass
        try:
            widget.remove()
        except Exception:
            pass

    def redraw_widget(self, widget: "Widget | None") -> None:
        """Redraw a mounted Sixel tree after terminal cells were repainted."""
        if self.mode != "sixel" or widget is None or not widget.is_mounted:
            return
        from textual.widget import Widget

        for owned_widget in widget.walk_children(Widget, with_self=True):
            owned_widget.refresh()


def _create_halfcell_renderer(selection: RendererSelection) -> ImageRenderer:
    """Build the real Unicode fallback without probing native protocols."""
    try:
        from textual_image._terminal import get_cell_size

        cell_size = get_cell_size()
    except ImportError:
        return ImageRenderer(selection, cell_pixels=(1, 2))

    original_stdout = sys.__stdout__

    class _NonTtyStdout:
        def isatty(self) -> bool:
            return False

        def __getattr__(self, name: str) -> object:
            return getattr(original_stdout, name)

    try:
        if original_stdout is not None:
            sys.__stdout__ = _NonTtyStdout()  # type: ignore[assignment]
        from textual_image.widget import HalfcellImage
    except ImportError:
        return ImageRenderer(selection, cell_pixels=(1, 2))
    finally:
        sys.__stdout__ = original_stdout
    return ImageRenderer(
        selection,
        cell_pixels=(cell_size.width, cell_size.height),
        widget_factory=HalfcellImage,
    )


def _term_features_include(term_features: str, feature: str) -> bool:
    """Return whether an encoded ``TERM_FEATURES`` value has an exact token."""
    prefix = _TERM_FEATURE_PREFIX.match(str(term_features or ""))
    if prefix is None:
        return False
    return feature in _TERM_FEATURE_TOKEN.findall(prefix.group(0))


def initialize_image_renderer(
    *,
    environ: Mapping[str, str] | None = None,
    force_halfcell: bool = False,
) -> ImageRenderer:
    """Probe the terminal and capture its cell size before Textual starts."""
    environment = os.environ if environ is None else environ
    requested = environment.get("A0_CLI_IMAGE_MODE", "auto")
    normalized = str(requested or "auto").strip().lower()
    if normalized == "off":
        return ImageRenderer.disabled()
    if environment.get("PYTEST_CURRENT_TEST") and not force_halfcell:
        return ImageRenderer.for_test(mode="halfcell")

    stdout = sys.__stdout__
    is_tty = bool(stdout and stdout.isatty())
    term_program = str(environment.get("TERM_PROGRAM", "")).strip().casefold()
    if force_halfcell or normalized == "halfcell":
        selection = select_image_mode(
            requested,
            is_tty=is_tty,
            tgp_supported=False,
            sixel_supported=False,
            force_halfcell=force_halfcell,
        )
        return _create_halfcell_renderer(selection)
    if not is_tty or term_program == "warpterminal":
        selection = select_image_mode(
            requested,
            is_tty=False,
            tgp_supported=False,
            sixel_supported=False,
        )
        return ImageRenderer(selection, cell_pixels=(1, 1))

    try:
        from textual_image.renderable import tgp, sixel

        tgp_supported = bool(tgp.query_terminal_support()) if is_tty else False
        direct_iterm_reports_sixel = (
            normalized == "auto"
            and term_program == "iterm.app"
            and not environment.get("TMUX")
            and _term_features_include(environment.get("TERM_FEATURES", ""), "Sx")
        )
        if direct_iterm_reports_sixel:
            sixel_supported = True
        else:
            sixel_supported = bool(sixel.query_terminal_support()) if is_tty else False
        selection = select_image_mode(
            requested,
            is_tty=is_tty,
            tgp_supported=tgp_supported,
            sixel_supported=sixel_supported,
        )
        if selection.mode == "off":
            return ImageRenderer(selection, cell_pixels=(1, 1))

        from textual_image._terminal import get_cell_size
        from textual_image.widget import SixelImage, TGPImage

        cell_size = get_cell_size()
    except ImportError:
        selection = select_image_mode(
            requested,
            is_tty=is_tty,
            tgp_supported=False,
            sixel_supported=False,
        )
        return ImageRenderer(selection, cell_pixels=(1, 1))

    factories: dict[ImageMode, WidgetFactory] = {
        "tgp": TGPImage,
        "sixel": SixelImage,
    }
    return ImageRenderer(
        selection,
        cell_pixels=(cell_size.width, cell_size.height),
        widget_factory=factories[selection.mode],
    )
