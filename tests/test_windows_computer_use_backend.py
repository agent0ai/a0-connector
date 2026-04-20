from __future__ import annotations

import base64
import os
import sys
import types
from pathlib import Path

import numpy as np
import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WINDOWS_PACKAGE_SRC = PROJECT_ROOT / "packages" / "a0-computer-use-windows" / "src"
if str(WINDOWS_PACKAGE_SRC) not in sys.path:
    sys.path.insert(0, str(WINDOWS_PACKAGE_SRC))

from a0_computer_use_windows.backend import WINDOWS_BACKEND_SPEC, WindowsComputerUseBackend
import a0_computer_use_windows.runtime as windows_runtime_mod
from a0_computer_use_windows.runtime import (
    WindowsComputerUseError,
    WindowsComputerUseRuntime,
    WindowsSessionStore,
)
from a0_computer_use_windows.shared import normalize_action_payload


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


def test_windows_backend_spec_exports_expected_metadata() -> None:
    spec = WINDOWS_BACKEND_SPEC

    assert spec.backend_id == "windows"
    assert spec.backend_family == "windows"
    assert spec.interpreter_strategy == "current_python"
    assert Path(spec.helper_target).name == "runtime.py"
    assert spec.supports_trust_mode("interactive") is True
    assert spec.supports_trust_mode("persistent") is True
    assert spec.supports_trust_mode("free_run") is True
    assert "inline-png-capture" in spec.features
    assert "uia-automation" in spec.features


def test_windows_backend_wrapper_uses_current_python() -> None:
    backend = WindowsComputerUseBackend()

    assert backend.spec is WINDOWS_BACKEND_SPEC
    assert backend.helper_command()[0] == sys.executable
    assert backend.helper_command()[-1] == "--stdio"


def test_windows_action_normalization_matches_shared_surface() -> None:
    move = normalize_action_payload("move", {"x": 0.25, "y": 0.75}, context_id="ctx-1")
    click = normalize_action_payload(
        "click",
        {"x": 0.4, "y": 0.6, "button": "right", "count": 2},
        context_id="ctx-1",
    )
    scroll = normalize_action_payload("scroll", {"dx": 1, "dy": -2}, context_id="ctx-1")
    keys = normalize_action_payload("key", {"key": "ctrl+alt+t"}, context_id="ctx-1")
    typed = normalize_action_payload("type", {"text": "hello", "submit": True}, context_id="ctx-1")

    assert move["x"] == 0.25 and move["y"] == 0.75
    assert click["button"] == "right" and click["count"] == 2
    assert scroll["dx"] == 1 and scroll["dy"] == -2
    assert keys["keys"] == ["ctrl", "alt", "t"]
    assert typed["text"] == "hello" and typed["submit"] is True


def test_windows_runtime_rejects_free_run_without_restore_token(tmp_path: Path) -> None:
    runtime = WindowsComputerUseRuntime(driver=_FakeDriver(), state_dir=tmp_path / "state")

    with pytest.raises(WindowsComputerUseError) as exc_info:
        runtime.start_session({"context_id": "ctx-1", "trust_mode": "free_run"})

    assert exc_info.value.code == "COMPUTER_USE_REARM_REQUIRED"


def test_windows_runtime_session_policies_are_persisted_when_valid(tmp_path: Path) -> None:
    runtime = WindowsComputerUseRuntime(driver=_FakeDriver(), state_dir=tmp_path / "state")
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
    stored = WindowsSessionStore(state_dir=tmp_path / "state").get("ctx-1")
    assert stored is not None
    assert stored.restore_token == restore_token


def test_windows_runtime_interactive_sessions_are_fresh_each_time(tmp_path: Path) -> None:
    runtime = WindowsComputerUseRuntime(driver=_FakeDriver(), state_dir=tmp_path / "state")

    first = runtime.start_session({"context_id": "ctx-1", "trust_mode": "interactive"})
    runtime.stop_session({"context_id": "ctx-1"})
    second = runtime.start_session({"context_id": "ctx-1", "trust_mode": "interactive"})

    assert first["session_id"] != second["session_id"]
    assert "restore_token" not in first
    assert "restore_token" not in second


def test_windows_runtime_capture_returns_inline_png_payload(tmp_path: Path) -> None:
    runtime = WindowsComputerUseRuntime(driver=_FakeDriver(), state_dir=tmp_path / "state")
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


def test_windows_runtime_capture_writes_requested_path_without_inline_payload(tmp_path: Path) -> None:
    runtime = WindowsComputerUseRuntime(driver=_FakeDriver(), state_dir=tmp_path / "state")
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


def test_windows_desktop_automation_prefers_dxcam_numpy_processor(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []

    class FakeCamera:
        def grab(self):
            return np.zeros((1, 1, 4), dtype=np.uint8)

    def create(**kwargs):
        calls.append(dict(kwargs))
        return FakeCamera()

    automation = windows_runtime_mod._WindowsDesktopAutomation.__new__(windows_runtime_mod._WindowsDesktopAutomation)
    automation._camera = None
    monkeypatch.setattr(windows_runtime_mod, "_load_dxcam_module", lambda: types.SimpleNamespace(create=create))

    png_bytes, width, height = automation.capture_png()

    assert png_bytes
    assert (width, height) == (1, 1)
    assert calls == [{"output_idx": 0, "processor_backend": "numpy"}]


def test_windows_runtime_normalizes_actions_and_routes_input(tmp_path: Path) -> None:
    driver = _FakeDriver()
    runtime = WindowsComputerUseRuntime(driver=driver, state_dir=tmp_path / "state")
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
    runtime.key({"context_id": "ctx-1", "keys": ["ctrl", "alt", "t"]})
    runtime.type_text({"context_id": "ctx-1", "text": "hello", "submit": True})

    assert [call[0] for call in driver.calls if call[0] != "screen_size"] == [
        "move",
        "click",
        "scroll",
        "key",
        "type_text",
    ]


@pytest.mark.skipif(os.name != "nt", reason="Windows desktop support probe is Windows-only")
def test_windows_support_probe_is_true_when_dependencies_exist() -> None:
    # This is a smoke check for the real Windows path; it stays skipped on Linux.
    assert WINDOWS_BACKEND_SPEC.detect() is True
