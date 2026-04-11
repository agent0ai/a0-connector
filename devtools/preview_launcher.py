#!/usr/bin/env python
"""Launch the browser preview app and tie it to the preview parent on Linux."""

from __future__ import annotations

import ctypes
import os
import signal
import sys

_PR_SET_PDEATHSIG = 1


def _arm_parent_death_signal() -> None:
    """Best-effort Linux guard so orphaned preview sessions terminate promptly."""

    if sys.platform != "linux":
        return

    try:
        libc = ctypes.CDLL(None, use_errno=True)
    except OSError:
        return

    if libc.prctl(_PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0) != 0:
        return

    if os.getppid() == 1:
        raise SystemExit(0)


def main() -> None:
    _arm_parent_death_signal()
    os.execv(sys.executable, [sys.executable, "-m", "agent_zero_cli"])


if __name__ == "__main__":
    main()
