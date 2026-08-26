from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from agent_zero_cli.config import CLIConfig
from agent_zero_cli.image_render import CellBox


ROOT = Path(__file__).resolve().parents[1]


def _load_module(name: str, relative_path: str):
    path = ROOT / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_snapshot_uses_current_cli_config_shape() -> None:
    snapshot = _load_module("snapshot_module", "devtools/snapshot.py")

    config = snapshot._snapshot_config()

    assert isinstance(config, CLIConfig)
    assert config.instance_url == "http://127.0.0.1:19999"


def test_preview_launcher_forces_halfcell(monkeypatch: pytest.MonkeyPatch) -> None:
    preview = _load_module("preview_launcher_images", "devtools/preview_launcher.py")
    called: list[tuple[str, list[str]]] = []
    monkeypatch.setenv("A0_CLI_IMAGE_MODE", "auto")
    monkeypatch.setattr(
        preview.os,
        "execv",
        lambda executable, argv: called.append((executable, argv)),
    )

    preview.main()

    assert preview.os.environ["A0_CLI_IMAGE_MODE"] == "halfcell"
    assert called[0][1][-2:] == ["-m", "agent_zero_cli"]


def test_snapshot_uses_forced_halfcell_renderer(monkeypatch: pytest.MonkeyPatch) -> None:
    from PIL import Image as PILImage
    from textual_image.widget import HalfcellImage

    snapshot = _load_module("snapshot_images", "devtools/snapshot.py")
    monkeypatch.setenv("A0_CLI_IMAGE_MODE", "off")

    renderer = snapshot._snapshot_renderer()
    widget = renderer.create_widget(
        PILImage.new("RGB", (4, 4), "#123456"),
        CellBox(4, 2),
    )

    assert renderer.mode == "halfcell"
    assert isinstance(widget, HalfcellImage)
