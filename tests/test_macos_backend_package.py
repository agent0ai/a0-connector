from __future__ import annotations

import sys
from pathlib import Path

import pytest


PACKAGE_SRC = Path(__file__).resolve().parents[1] / "packages/a0-computer-use-macos/src"
if str(PACKAGE_SRC) not in sys.path:
    sys.path.insert(0, str(PACKAGE_SRC))

from a0_computer_use_macos import MACOS_BACKEND_SPEC
from a0_computer_use_macos import detection as macos_detection


def test_macos_backend_spec_exposes_expected_metadata() -> None:
    spec = MACOS_BACKEND_SPEC

    assert spec.backend_id == "macos"
    assert spec.backend_family == "macos"
    assert spec.priority == 100
    assert spec.interpreter_strategy == "current_python"
    assert Path(spec.helper_target).name == "runtime.py"
    assert spec.supports_trust_mode("interactive") is True
    assert spec.supports_trust_mode("persistent") is True
    assert spec.supports_trust_mode("free_run") is True
    assert "inline-png-capture" in spec.features
    assert "quartz-input-events" in spec.features


def test_macos_detection_and_support_reason_are_additive_and_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(macos_detection.sys, "platform", "darwin")
    monkeypatch.setattr(macos_detection.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr(macos_detection.shutil, "which", lambda name: "/usr/sbin/screencapture")

    assert macos_detection.macos_backend_supported() is True
    assert macos_detection.macos_backend_support_reason() == "macOS desktop backend is available."

    monkeypatch.setattr(macos_detection.shutil, "which", lambda name: None)
    assert macos_detection.macos_backend_supported() is False
    assert "screencapture utility is unavailable" in macos_detection.macos_backend_support_reason()

    monkeypatch.setattr(macos_detection.sys, "platform", "linux")
    assert macos_detection.macos_backend_supported() is False
    assert "only available on macOS" in macos_detection.macos_backend_support_reason()
