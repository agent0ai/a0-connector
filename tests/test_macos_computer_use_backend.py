from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MACOS_PACKAGE_SRC = PROJECT_ROOT / "packages" / "a0-computer-use-macos" / "src"
if str(MACOS_PACKAGE_SRC) not in sys.path:
    sys.path.insert(0, str(MACOS_PACKAGE_SRC))

from a0_computer_use_macos.backend import MACOS_BACKEND_SPEC, MacOSComputerUseBackend
import a0_computer_use_macos.runtime as macos_runtime_mod
from a0_computer_use_macos.runtime import (
    MacOSComputerUseError,
    MacOSComputerUseRuntime,
    MacOSSessionStore,
)
from a0_computer_use_macos.shared import normalize_action_payload


class _FakeDriver:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []
        self._width = 1280
        self._height = 720

    def screen_size(self) -> tuple[int, int]:
        self.calls.append(("screen_size", tuple(), {}))
        return self._width, self._height

    def capture_png(self) -> tuple[bytes, int, int]:
        self.calls.append(("capture_png", tuple(), {}))
        png_bytes = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/5wAAAABJRU5ErkJggg=="
        )
        return png_bytes, 1, 1

    def move(self, x: float, y: float) -> None:
        self.calls.append(("move", (x, y), {}))

    def click(self, x: float, y: float, *, button: str, count: int) -> None:
        self.calls.append(("click", (x, y), {"button": button, "count": count}))

    def scroll(self, dx: int, dy: int) -> None:
        self.calls.append(("scroll", (dx, dy), {}))

    def key(self, keys: list[str]) -> None:
        self.calls.append(("key", (tuple(keys),), {}))

    def type_text(self, text: str, *, submit: bool) -> None:
        self.calls.append(("type_text", (text,), {"submit": submit}))


def _runtime(tmp_path: Path) -> MacOSComputerUseRuntime:
    runtime = MacOSComputerUseRuntime(driver=_FakeDriver(), state_dir=tmp_path / "state")
    runtime._ensure_accessibility_permission = lambda **kwargs: None  # type: ignore[method-assign]
    runtime._probe_capture_dimensions = lambda **kwargs: (1280, 720)  # type: ignore[method-assign]
    return runtime


def test_macos_backend_spec_exports_expected_metadata() -> None:
    spec = MACOS_BACKEND_SPEC

    assert spec.backend_id == "macos"
    assert spec.backend_family == "macos"
    assert spec.interpreter_strategy == "current_python"
    assert Path(spec.helper_target).name == "runtime.py"
    assert spec.supports_trust_mode("interactive") is True
    assert spec.supports_trust_mode("persistent") is True
    assert spec.supports_trust_mode("free_run") is True
    assert "inline-png-capture" in spec.features
    assert "accessibility-trust" in spec.features


def test_macos_backend_wrapper_uses_current_python() -> None:
    backend = MacOSComputerUseBackend()

    assert backend.spec is MACOS_BACKEND_SPEC
    assert backend.helper_command()[0] == sys.executable
    assert backend.helper_command()[-1] == "--stdio"


def test_macos_action_normalization_matches_shared_surface() -> None:
    move = normalize_action_payload("move", {"x": 0.25, "y": 0.75}, context_id="ctx-1")
    click = normalize_action_payload(
        "click",
        {"x": 0.4, "y": 0.6, "button": "right", "count": 2},
        context_id="ctx-1",
    )
    scroll = normalize_action_payload("scroll", {"dx": 1, "dy": -2}, context_id="ctx-1")
    keys = normalize_action_payload("key", {"key": "cmd+shift+t"}, context_id="ctx-1")
    typed = normalize_action_payload("type", {"text": "hello", "submit": True}, context_id="ctx-1")

    assert move["x"] == 0.25 and move["y"] == 0.75
    assert click["button"] == "right" and click["count"] == 2
    assert scroll["dx"] == 1 and scroll["dy"] == -2
    assert keys["keys"] == ["cmd", "shift", "t"]
    assert typed["text"] == "hello" and typed["submit"] is True


def test_macos_runtime_rejects_free_run_without_restore_token(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)

    with pytest.raises(MacOSComputerUseError) as exc_info:
        runtime.start_session({"context_id": "ctx-1", "trust_mode": "free_run"})

    assert exc_info.value.code == "COMPUTER_USE_REARM_REQUIRED"


def test_macos_runtime_session_policies_are_persisted_when_valid(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    restore_token = "123e4567-e89b-12d3-a456-426614174000"

    first = runtime.start_session(
        {
            "context_id": "ctx-1",
            "trust_mode": "persistent",
            "restore_token": restore_token,
        }
    )
    runtime.stop_session({"context_id": "ctx-1"})
    second = runtime.start_session(
        {
            "context_id": "ctx-1",
            "trust_mode": "persistent",
            "restore_token": restore_token,
        }
    )

    assert first["session_id"] == second["session_id"]
    assert second["reused"] is True
    stored = MacOSSessionStore(state_dir=tmp_path / "state").get("ctx-1")
    assert stored is not None
    assert stored.restore_token == restore_token


def test_macos_runtime_capture_returns_inline_png_payload(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    runtime.start_session(
        {
            "context_id": "ctx-1",
            "trust_mode": "persistent",
            "restore_token": "123e4567-e89b-12d3-a456-426614174000",
        }
    )

    capture = runtime.capture({"context_id": "ctx-1"})

    assert capture["width"] == 1
    assert capture["height"] == 1
    assert capture["png_base64"]
    assert base64.b64decode(capture["png_base64"])


def test_macos_runtime_capture_writes_requested_path_without_inline_payload(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    runtime.start_session(
        {
            "context_id": "ctx-1",
            "trust_mode": "persistent",
            "restore_token": "123e4567-e89b-12d3-a456-426614174000",
        }
    )
    capture_path = tmp_path / "captures" / "capture.png"

    capture = runtime.capture({"context_id": "ctx-1", "capture_path": str(capture_path)})

    assert capture["width"] == 1
    assert capture["height"] == 1
    assert capture["capture_path"] == str(capture_path)
    assert "png_base64" not in capture
    assert capture_path.exists()


def test_macos_runtime_debug_logs_start_session_progress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("A0_COMPUTER_USE_DEBUG", "1")
    runtime = _runtime(tmp_path)

    runtime.start_session(
        {
            "context_id": "ctx-1",
            "trust_mode": "persistent",
            "restore_token": "123e4567-e89b-12d3-a456-426614174000",
        }
    )

    stderr = capsys.readouterr().err
    assert "start_session.begin" in stderr
    assert "start_session.accessibility.begin" in stderr
    assert "start_session.capture_probe.begin" in stderr
    assert "start_session.created_session" in stderr


def test_macos_runtime_normalizes_actions_and_routes_input(tmp_path: Path) -> None:
    driver = _FakeDriver()
    runtime = MacOSComputerUseRuntime(driver=driver, state_dir=tmp_path / "state")
    runtime._ensure_accessibility_permission = lambda **kwargs: None  # type: ignore[method-assign]
    runtime._probe_capture_dimensions = lambda **kwargs: (1280, 720)  # type: ignore[method-assign]
    runtime.start_session(
        {
            "context_id": "ctx-1",
            "trust_mode": "persistent",
            "restore_token": "123e4567-e89b-12d3-a456-426614174000",
        }
    )

    runtime.move({"context_id": "ctx-1", "x": 0.25, "y": 0.75})
    runtime.click({"context_id": "ctx-1", "x": 0.5, "y": 0.5, "button": "left", "count": 2})
    runtime.scroll({"context_id": "ctx-1", "dx": 1, "dy": -2})
    runtime.key({"context_id": "ctx-1", "keys": ["cmd", "shift", "t"]})
    runtime.type_text({"context_id": "ctx-1", "text": "hello", "submit": True})

    assert [call[0] for call in driver.calls if call[0] != "capture_png"] == [
        "move",
        "click",
        "scroll",
        "key",
        "type_text",
    ]
