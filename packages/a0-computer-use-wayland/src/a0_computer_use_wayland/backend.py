from __future__ import annotations

from agent_zero_cli.computer_use_backend import ComputerUseBackendSpec

from .detection import detect_wayland_support, wayland_support_reason
from .paths import HELPER_SCRIPT


WAYLAND_BACKEND_SPEC = ComputerUseBackendSpec(
    backend_id="wayland",
    backend_family="linux",
    priority=100,
    detect=detect_wayland_support,
    features=(
        "portal-remote-desktop",
        "portal-screencast",
        "inline-png-capture",
        "fresh-frame-capture",
        "normalized-screen-coordinates",
        "global-pixel-actions",
        "pointer-injection",
        "keyboard-injection",
        "atspi-tree-snapshot",
        "atspi-structural-targeting",
        "atspi-element-action",
        "atspi-set-value",
        "native-window-list",
        "window-state",
        "window-scoped-tree-snapshot",
        "verified-window-focus",
        "target-verified-keyboard-input",
        "element-index-targeting",
        "background-dispatch",
        "foreground-dispatch-fallback",
        "accessibility-tree-snapshot",
        "accessibility-structural-targeting",
        "real-cursor-may-move",
        "a0-tag",
    ),
    interpreter_strategy="system_python",
    helper_target=str(HELPER_SCRIPT),
    trust_mode_support=("interactive", "persistent", "allow"),
    support_reason=wayland_support_reason,
)


def get_backend_spec() -> ComputerUseBackendSpec:
    return WAYLAND_BACKEND_SPEC
