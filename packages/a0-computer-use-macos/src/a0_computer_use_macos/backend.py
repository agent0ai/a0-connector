from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from agent_zero_cli.computer_use_backend import (
    ComputerUseBackendSpec,
    register_backend_spec,
)

from a0_computer_use_macos.detection import (
    macos_backend_support_reason,
    macos_backend_supported,
)
from a0_computer_use_macos.shared import (
    MACOS_BACKEND_FEATURES,
    MACOS_BACKEND_FAMILY,
    MACOS_BACKEND_ID,
    MACOS_BACKEND_PRIORITY,
    MACOS_TRUST_MODES,
)

_HELPER_TARGET = str(Path(__file__).with_name("runtime.py"))


def _detect() -> bool:
    return macos_backend_supported()


MACOS_BACKEND_SPEC = ComputerUseBackendSpec(
    backend_id=MACOS_BACKEND_ID,
    backend_family=MACOS_BACKEND_FAMILY,
    priority=MACOS_BACKEND_PRIORITY,
    detect=_detect,
    features=MACOS_BACKEND_FEATURES,
    interpreter_strategy="current_python",
    helper_target=_HELPER_TARGET,
    trust_mode_support=MACOS_TRUST_MODES,
    support_reason=macos_backend_support_reason,
)


class MacOSComputerUseBackend:
    spec = MACOS_BACKEND_SPEC

    def hello_metadata(self) -> dict[str, Any]:
        return {
            "supported": self.spec.detect(),
            "backend_id": self.spec.backend_id,
            "backend_family": self.spec.backend_family,
            "features": list(self.spec.features),
            "support_reason": macos_backend_support_reason(),
        }

    def helper_command(self) -> list[str]:
        return [sys.executable, self.spec.helper_target, "--stdio"]


def install_backend_spec() -> ComputerUseBackendSpec:
    return register_backend_spec(MACOS_BACKEND_SPEC)
