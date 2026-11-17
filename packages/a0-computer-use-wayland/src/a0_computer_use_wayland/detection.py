from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .paths import SYSTEM_PYTHON


def detect_wayland_support() -> bool:
    return wayland_support_reason() == "Wayland portal backend is available."


def wayland_support_reason() -> str:
    if not Path(SYSTEM_PYTHON).exists():
        return f"Required system Python interpreter not found at {SYSTEM_PYTHON}."

    session_type = (os.environ.get("XDG_SESSION_TYPE") or "").strip().lower()
    if session_type != "wayland":
        return f"XDG_SESSION_TYPE={session_type or 'unset'} is not supported by the Wayland portal backend."

    try:
        probe = subprocess.run(
            [SYSTEM_PYTHON, "-I", "-c", (
                "import dbus; from dbus.mainloop.glib import DBusGMainLoop; "
                "import gi; from PIL import Image; "
                "gi.require_version('Gdk', '3.0'); gi.require_version('Gst', '1.0'); "
                "gi.require_version('Atspi', '2.0'); "
                "from gi.repository import Gdk, GLib, Gst, Atspi"
            )],
            capture_output=True, text=True, timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"Wayland system Python dependency check failed: {exc}. See the Wayland backend README."
    if probe.returncode:
        detail = probe.stderr.strip().splitlines()
        error = detail[-1][:500] if detail else f"exit status {probe.returncode}"
        return f"Wayland system Python dependencies are unavailable: {error}. See the Wayland backend README."

    return "Wayland portal backend is available."
