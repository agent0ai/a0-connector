from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from agent_zero_cli.computer_use_backend import COMPUTER_USE_CONTRACT_VERSION


PROJECT_ROOT = Path(__file__).resolve().parents[1]
for package in (
    "a0-computer-use-windows",
    "a0-computer-use-macos",
    "a0-computer-use-wayland",
):
    package_src = PROJECT_ROOT / "packages" / package / "src"
    if str(package_src) not in sys.path:
        sys.path.insert(0, str(package_src))

from a0_computer_use_macos.backend import MACOS_BACKEND_SPEC  # noqa: E402
from a0_computer_use_wayland import WAYLAND_BACKEND_SPEC  # noqa: E402
from a0_computer_use_windows.backend import WINDOWS_BACKEND_SPEC  # noqa: E402


@pytest.mark.parametrize("backend", ("macos", "windows"))
@pytest.mark.parametrize("legacy_cli", (False, True), ids=("current-cli", "without-text-utils"))
def test_backend_boolean_payloads_work_without_new_cli_modules(backend: str, legacy_cli: bool) -> None:
    result = subprocess.run(
        [
            sys.executable, "-I", "-c",
            """
import importlib
import sys

sys.path[:0] = sys.argv[1:3]
if sys.argv[4] == "True":
    sys.modules["agent_zero_cli.text_utils"] = None
shared = importlib.import_module(sys.argv[3] + ".shared")

for value in (True, 1, -1, 0.5, "1", " TRUE ", "yes", "on", " EnAbLeD "):
    assert shared.coerce_bool(value) is True, value
    payload = shared.normalize_action_payload("type", {"text": "hello", "submit": value}, context_id="test")
    assert payload["submit"] is True, payload
for value in (False, 0, 0.0, "0", " FALSE ", "no", "off", " DiSaBlEd ", "", "  "):
    assert shared.coerce_bool(value, default=True) is False, value
    payload = shared.normalize_action_payload("type", {"text": "hello", "submit": value}, context_id="test")
    assert "submit" not in payload, payload
for value in (None, "unknown", [], {}):
    for default in (False, True):
        assert shared.coerce_bool(value, default=default) is default, value
""",
            str(PROJECT_ROOT / "src"),
            str(PROJECT_ROOT / "packages" / f"a0-computer-use-{backend}" / "src"),
            f"a0_computer_use_{backend}",
            str(legacy_cli),
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_windows_uia_advertises_full_background_contract() -> None:
    capabilities = WINDOWS_BACKEND_SPEC.capabilities()

    assert capabilities["contract_version"] == COMPUTER_USE_CONTRACT_VERSION
    assert capabilities["backend"] == {"id": "windows", "family": "windows"}
    assert capabilities["identity"] == {
        "pid": True,
        "window_id": True,
        "element_index": True,
    }
    assert capabilities["windows"] == {"list": True, "state": True}
    assert capabilities["elements"]["tree_backends"] == ["uia"]
    assert capabilities["elements"]["tree"] is True
    assert capabilities["elements"]["structural_targeting"] is True
    assert capabilities["elements"]["action"] is True
    assert capabilities["dispatch"]["default"] == "background"
    assert capabilities["dispatch"]["background"] is True
    assert capabilities["dispatch"]["foreground_fallback"] is True


def test_macos_ax_advertises_same_identity_and_dispatch_contract() -> None:
    capabilities = MACOS_BACKEND_SPEC.capabilities()

    assert capabilities["contract_version"] == COMPUTER_USE_CONTRACT_VERSION
    assert capabilities["backend"] == {"id": "macos", "family": "macos"}
    assert capabilities["identity"] == {
        "pid": True,
        "window_id": True,
        "element_index": True,
    }
    assert capabilities["windows"] == {"list": True, "state": True}
    assert capabilities["elements"]["tree_backends"] == ["ax"]
    assert capabilities["elements"]["tree"] is True
    assert capabilities["dispatch"]["default"] == "background"
    assert capabilities["dispatch"]["background"] is True
    assert capabilities["input"]["keyboard"] is True


def test_wayland_atspi_advertises_same_portable_contract_with_linux_tree_backend() -> None:
    capabilities = WAYLAND_BACKEND_SPEC.capabilities()

    assert capabilities["contract_version"] == COMPUTER_USE_CONTRACT_VERSION
    assert capabilities["backend"] == {"id": "wayland", "family": "linux"}
    assert capabilities["identity"] == {
        "pid": True,
        "window_id": True,
        "element_index": True,
    }
    assert capabilities["windows"] == {"list": True, "state": True}
    assert capabilities["elements"]["tree_backends"] == ["ax", "at-spi"]
    assert capabilities["elements"]["tree"] is True
    assert capabilities["elements"]["set_value"] is True
    assert capabilities["dispatch"]["default"] == "background"
    assert capabilities["dispatch"]["background"] is True
    assert capabilities["input"]["pointer"] is True
