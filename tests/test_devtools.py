from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

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


def test_preview_launcher_forces_colour_and_halfcell(monkeypatch: pytest.MonkeyPatch) -> None:
    preview = _load_module("preview_launcher_images", "devtools/preview_launcher.py")
    called: list[tuple[str, list[str], dict[str, str]]] = []
    monkeypatch.setenv("A0_CLI_IMAGE_MODE", "auto")
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(
        preview.os,
        "execve",
        lambda executable, argv, env: called.append((executable, argv, env)),
    )

    preview.main()

    assert called[0][2]["A0_CLI_IMAGE_MODE"] == "halfcell"
    assert "NO_COLOR" not in called[0][2]
    assert preview.os.environ["NO_COLOR"] == "1"
    assert preview.os.environ["A0_CLI_IMAGE_MODE"] == "auto"
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


@pytest.mark.anyio
async def test_snapshot_keeps_theme_colour_from_no_colour_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_zero_cli.app import AgentZeroCLI

    snapshot = _load_module("snapshot_colour", "devtools/snapshot.py")
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("TERM", "dumb")
    monkeypatch.setattr(AgentZeroCLI, "_start_instance_discovery", lambda *a, **kw: None)
    monkeypatch.setattr(AgentZeroCLI, "_start_cli_update_check", lambda *a, **kw: None)
    output = tmp_path / "colour.svg"

    await snapshot._capture(output, width=100, height=32, wait=0)

    assert "fill: #00b4ff" in output.read_text(encoding="utf-8")
    assert snapshot.os.environ["NO_COLOR"] == "1"
    assert AgentZeroCLI(config=snapshot._snapshot_config()).no_color


@pytest.mark.parametrize(
    "check,upgrade,changed",
    [(False, False, False), (False, True, True), (True, False, False), (True, False, True)],
)
def test_dependency_lock_preserves_pins_and_checks_without_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, check: bool, upgrade: bool, changed: bool
) -> None:
    lock = _load_module("lock_dependencies", "devtools/lock_dependencies.py")
    monkeypatch.setattr(lock, "ROOT", tmp_path)
    originals = {}
    for name in ("RUNTIME_IN", "BUILD_IN", "RUNTIME_LOCK", "BUILD_LOCK", "PYPROJECT"):
        path = tmp_path / getattr(lock, name).relative_to(ROOT)
        path.parent.mkdir(parents=True, exist_ok=True)
        text = "example==1.0\n"
        if name == "PYPROJECT":
            text = lock._sync_pyproject(
                "[build-system]\nrequires = []\n[project]\ndependencies = []\n",
                runtime_requirements=["example==1.0"],
                build_requirements=["example==1.0"],
            )
        path.write_text(text, encoding="utf-8")
        originals[path] = text
        monkeypatch.setattr(lock, name, path)

    calls = []

    def compile_locked(command, **kwargs):
        assert ("--upgrade" in command) == upgrade
        output = Path(command[command.index("-o") + 1])
        if not output.is_absolute():
            output = kwargs["cwd"] / output
        assert output.read_text(encoding="utf-8") == "example==1.0\n"
        if changed:
            output.write_text("example==2.0\n", encoding="utf-8")
        calls.append(output)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(lock.subprocess, "run", compile_locked)
    monkeypatch.setattr(
        lock.sys,
        "argv",
        ["lock_dependencies.py", *(["--check"] if check else []), *(["--upgrade"] if upgrade else [])],
    )

    assert lock.main() == int(check and changed)
    assert len(calls) == 2
    if check:
        assert all(path.read_text(encoding="utf-8") == text for path, text in originals.items())
    else:
        expected = "example==2.0" if changed else "example==1.0"
        assert lock.PYPROJECT.read_text(encoding="utf-8").count(expected) == 2
