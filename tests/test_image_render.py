from __future__ import annotations

import builtins
import inspect
import sys
from types import ModuleType, SimpleNamespace

from PIL import Image as PILImage
import pytest

from agent_zero_cli.image_render import (
    CellBox,
    ImageRenderer,
    RendererSelection,
    initialize_image_renderer,
    select_image_mode,
)


def test_auto_prefers_tgp_before_sixel() -> None:
    selected = select_image_mode(
        "auto",
        is_tty=True,
        tgp_supported=True,
        sixel_supported=True,
    )
    assert selected.mode == "tgp"
    assert selected.notice == ""


def test_explicit_unsupported_native_mode_disables_images() -> None:
    selected = select_image_mode(
        "sixel",
        is_tty=True,
        tgp_supported=True,
        sixel_supported=False,
    )
    assert selected.mode == "off"
    assert selected.notice == "Sixel images are unavailable; image rendering disabled."


def test_non_tty_disables_images() -> None:
    assert select_image_mode(
        "auto",
        is_tty=False,
        tgp_supported=True,
        sixel_supported=True,
    ).mode == "off"


def test_pytest_context_forces_library_free_halfcell_before_native_probes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probes: list[str] = []
    package = ModuleType("textual_image")
    package.__path__ = []  # type: ignore[attr-defined]
    renderable = ModuleType("textual_image.renderable")
    renderable.tgp = SimpleNamespace(
        query_terminal_support=lambda: probes.append("tgp") or True,
    )
    renderable.sixel = SimpleNamespace(
        query_terminal_support=lambda: probes.append("sixel") or True,
    )
    monkeypatch.setitem(sys.modules, "textual_image", package)
    monkeypatch.setitem(sys.modules, "textual_image.renderable", renderable)
    monkeypatch.setattr(
        sys,
        "__stdout__",
        SimpleNamespace(isatty=lambda: True),
    )
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/test_image_render.py::automated")

    renderer = initialize_image_renderer()

    assert renderer.mode == "halfcell"
    assert renderer._widget_factory is None
    assert probes == []


def test_explicit_halfcell_skips_native_probes_and_constructs_renderer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from textual_image._terminal import CellSize, get_cell_size
    from textual_image.renderable import sixel, tgp

    probes: list[str] = []
    import_stdout_states: list[bool] = []
    real_import = builtins.__import__

    def track_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "textual_image.widget":
            import_stdout_states.append(bool(sys.__stdout__ and sys.__stdout__.isatty()))
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(tgp, "query_terminal_support", lambda: probes.append("tgp") or True)
    monkeypatch.setattr(sixel, "query_terminal_support", lambda: probes.append("sixel") or True)
    monkeypatch.setattr(get_cell_size, "_result", CellSize(10, 20), raising=False)
    monkeypatch.setattr(builtins, "__import__", track_import)
    tty_stdout = SimpleNamespace(isatty=lambda: True)
    monkeypatch.setattr(sys, "__stdout__", tty_stdout)

    renderer = initialize_image_renderer(environ={"A0_CLI_IMAGE_MODE": "halfcell"})
    widget = renderer.create_widget(
        PILImage.new("RGB", (4, 4), "#123456"),
        CellBox(4, 2),
    )
    from textual_image.widget import HalfcellImage

    assert renderer.mode == "halfcell"
    assert probes == []
    assert import_stdout_states[0] is False
    assert sys.__stdout__ is tty_stdout
    assert isinstance(widget, HalfcellImage)


@pytest.mark.parametrize(
    ("requested", "notice"),
    [
        ("auto", ""),
        ("tgp", "TGP images are unavailable; image rendering disabled."),
        ("sixel", "Sixel images are unavailable; image rendering disabled."),
    ],
)
def test_warp_skips_native_probes_and_disables_images(
    monkeypatch: pytest.MonkeyPatch,
    requested: str,
    notice: str,
) -> None:
    from textual_image.renderable import sixel, tgp

    probes: list[str] = []
    monkeypatch.setattr(tgp, "query_terminal_support", lambda: probes.append("tgp") or True)
    monkeypatch.setattr(sixel, "query_terminal_support", lambda: probes.append("sixel") or True)
    monkeypatch.setattr(sys, "__stdout__", SimpleNamespace(isatty=lambda: True))

    renderer = initialize_image_renderer(
        environ={
            "A0_CLI_IMAGE_MODE": requested,
            "TERM_PROGRAM": "wArPtErMiNaL",
        },
    )
    assert renderer.mode == "off"
    assert renderer.notice == notice
    assert probes == []


def test_iterm_exact_sixel_capability_is_authoritative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from textual_image._terminal import CellSize, get_cell_size
    from textual_image.renderable import sixel, tgp

    probes: list[str] = []
    monkeypatch.setattr(tgp, "query_terminal_support", lambda: probes.append("tgp") or False)
    monkeypatch.setattr(sixel, "query_terminal_support", lambda: probes.append("sixel") or False)
    monkeypatch.setattr(get_cell_size, "_result", CellSize(10, 20), raising=False)
    monkeypatch.setattr(sys, "__stdout__", SimpleNamespace(isatty=lambda: True))

    renderer = initialize_image_renderer(
        environ={
            "A0_CLI_IMAGE_MODE": "auto",
            "TERM_PROGRAM": "ITERM.APP",
            "TERM_FEATURES": "T3Uw2Sx;future-data-is-ignored",
        },
    )

    assert renderer.mode == "sixel"
    assert probes == ["tgp"]


@pytest.mark.parametrize("term_features", ["Sxtra", "Asx", "Sx0"])
def test_iterm_sixel_capability_requires_an_exact_token(
    monkeypatch: pytest.MonkeyPatch,
    term_features: str,
) -> None:
    from textual_image._terminal import CellSize, get_cell_size
    from textual_image.renderable import sixel, tgp

    probes: list[str] = []
    monkeypatch.setattr(tgp, "query_terminal_support", lambda: probes.append("tgp") or False)
    monkeypatch.setattr(sixel, "query_terminal_support", lambda: probes.append("sixel") or False)
    monkeypatch.setattr(get_cell_size, "_result", CellSize(10, 20), raising=False)
    monkeypatch.setattr(sys, "__stdout__", SimpleNamespace(isatty=lambda: True))

    renderer = initialize_image_renderer(
        environ={
            "A0_CLI_IMAGE_MODE": "auto",
            "TERM_PROGRAM": "iTerm.app",
            "TERM_FEATURES": term_features,
        },
    )

    assert renderer.mode == "off"
    assert probes == ["tgp", "sixel"]


@pytest.mark.parametrize(
    "environment_override",
    [
        {"TMUX": "/private/tmp/tmux/default,1,0"},
        {"A0_CLI_IMAGE_MODE": "sixel"},
    ],
)
def test_iterm_sixel_capability_only_overrides_auto_outside_tmux(
    monkeypatch: pytest.MonkeyPatch,
    environment_override: dict[str, str],
) -> None:
    from textual_image._terminal import CellSize, get_cell_size
    from textual_image.renderable import sixel, tgp

    probes: list[str] = []
    monkeypatch.setattr(tgp, "query_terminal_support", lambda: probes.append("tgp") or False)
    monkeypatch.setattr(sixel, "query_terminal_support", lambda: probes.append("sixel") or False)
    monkeypatch.setattr(get_cell_size, "_result", CellSize(10, 20), raising=False)
    monkeypatch.setattr(sys, "__stdout__", SimpleNamespace(isatty=lambda: True))
    environment = {
        "A0_CLI_IMAGE_MODE": "auto",
        "TERM_PROGRAM": "iTerm.app",
        "TERM_FEATURES": "T3Sx",
        **environment_override,
    }

    renderer = initialize_image_renderer(environ=environment)

    assert renderer.mode == "off"
    assert probes == ["tgp", "sixel"]


def test_non_iterm_terminal_still_uses_native_probes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from textual_image._terminal import CellSize, get_cell_size
    from textual_image.renderable import sixel, tgp

    probes: list[str] = []
    monkeypatch.setattr(tgp, "query_terminal_support", lambda: probes.append("tgp") or False)
    monkeypatch.setattr(sixel, "query_terminal_support", lambda: probes.append("sixel") or True)
    monkeypatch.setattr(get_cell_size, "_result", CellSize(10, 20), raising=False)
    monkeypatch.setattr(sys, "__stdout__", SimpleNamespace(isatty=lambda: True))

    renderer = initialize_image_renderer(
        environ={
            "A0_CLI_IMAGE_MODE": "auto",
            "TERM_PROGRAM": "ExampleTerminal",
            "TERM_FEATURES": "Sx",
        },
    )

    assert renderer.mode == "sixel"
    assert probes == ["tgp", "sixel"]


@pytest.mark.parametrize(
    "shell_environment",
    [
        {"SHELL": "/bin/bash"},
        {"SHELL": "/bin/zsh"},
        {"PSModulePath": r"C:\\Program Files\\PowerShell\\Modules"},
    ],
    ids=("bash", "zsh", "powershell"),
)
def test_standard_shells_disable_images_without_native_protocol(
    monkeypatch: pytest.MonkeyPatch,
    shell_environment: dict[str, str],
) -> None:
    from textual_image.renderable import sixel, tgp

    probes: list[str] = []
    monkeypatch.setattr(tgp, "query_terminal_support", lambda: probes.append("tgp") or False)
    monkeypatch.setattr(sixel, "query_terminal_support", lambda: probes.append("sixel") or False)
    monkeypatch.setattr(sys, "__stdout__", SimpleNamespace(isatty=lambda: True))

    renderer = initialize_image_renderer(
        environ={"A0_CLI_IMAGE_MODE": "auto", **shell_environment},
    )

    assert renderer.mode == "off"
    assert probes == ["tgp", "sixel"]


def test_renderer_fits_complete_thumbnail_and_expanded_boxes() -> None:
    renderer = ImageRenderer.for_test(mode="halfcell", cell_pixels=(1, 2))
    assert renderer.fit_box((1600, 900), available_columns=120, expanded=False) == CellBox(36, 10)
    assert renderer.fit_box((1600, 900), available_columns=80, expanded=True) == CellBox(80, 22)


def test_renderer_exposes_box_driven_widget_factory() -> None:
    from textual_image.widget import TGPImage

    assert tuple(inspect.signature(ImageRenderer.create_widget).parameters) == (
        "self",
        "image",
        "box",
    )
    assert tuple(inspect.signature(ImageRenderer.cleanup_widget).parameters) == (
        "self",
        "widget",
    )

    renderer = ImageRenderer(
        RendererSelection("tgp"),
        cell_pixels=(10, 20),
        widget_factory=TGPImage,
    )
    image = PILImage.new("RGB", (4, 4), "#123456")
    box = CellBox(8, 3)

    native_widget = renderer.create_widget(image, box)

    assert isinstance(native_widget, TGPImage)
    assert native_widget.styles.width.cells == box.columns
    assert native_widget.styles.height.cells == box.rows


def test_cleanup_is_none_safe_and_releases_image_before_removal() -> None:
    events: list[tuple[str, object]] = []

    class FakeWidget:
        image: object = object()

        def remove(self) -> None:
            events.append(("remove", self.image))

    renderer = ImageRenderer.for_test()
    widget = FakeWidget()

    renderer.cleanup_widget(None)
    renderer.cleanup_widget(widget)  # type: ignore[arg-type]

    assert events == [("remove", None)]


def test_cleanup_suppresses_image_release_and_removal_failures() -> None:
    events: list[str] = []

    class FailingWidget:
        @property
        def image(self) -> object:
            return object()

        @image.setter
        def image(self, value: object) -> None:
            assert value is None
            events.append("release")
            raise RuntimeError("protocol cleanup failed")

        def remove(self) -> None:
            events.append("remove")
            raise RuntimeError("widget removal failed")

    renderer = ImageRenderer.for_test()

    renderer.cleanup_widget(FailingWidget())  # type: ignore[arg-type]

    assert events == ["release", "remove"]


def test_installed_textual_image_branch_constructs_each_widget() -> None:
    from textual_image.widget import HalfcellImage, SixelImage, TGPImage

    image = PILImage.new("RGB", (4, 4), "#123456")
    for widget_type in (TGPImage, SixelImage, HalfcellImage):
        widget = widget_type(image)
        assert widget.image is image
        widget.image = None
