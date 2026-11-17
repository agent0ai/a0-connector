from __future__ import annotations

from pathlib import Path

from agent_zero_cli.computer_use_backend import (
    ComputerUseBackendSpec,
    register_builtin_backend_spec,
)

_HELPER_TARGET = str(Path(__file__).with_name("computer_use_helper.py"))


def _detect_wayland_support() -> bool:
    from a0_computer_use_wayland.detection import detect_wayland_support

    return detect_wayland_support()


def _support_reason() -> str:
    from a0_computer_use_wayland.detection import wayland_support_reason

    return wayland_support_reason()


WAYLAND_BACKEND_SPEC = register_builtin_backend_spec(
    ComputerUseBackendSpec(
        backend_id="wayland",
        backend_family="linux",
        priority=100,
        detect=_detect_wayland_support,
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
        helper_target=_HELPER_TARGET,
        trust_mode_support=("interactive", "persistent", "allow"),
        support_reason=_support_reason,
    )
)
