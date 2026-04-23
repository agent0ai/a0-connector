#!/usr/bin/env python
"""Capture an SVG snapshot of the TUI without a live Agent Zero instance.

Usage:
    python devtools/snapshot.py [--output PATH] [--width COLS] [--height ROWS] [--wait SECONDS]

Produces a pixel-perfect SVG of the initial screen (connection-pending state).
Useful for verifying layout, footer labels, and CSS without a running backend.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path


_OUT_DIR = Path(__file__).resolve().parent / "snapshots"


def _snapshot_config():
    from agent_zero_cli.config import CLIConfig

    # Use a dummy config so the app does not connect to a live Agent Zero instance.
    return CLIConfig(instance_url="http://127.0.0.1:19999")


async def _capture(output: Path, width: int, height: int, wait: float) -> None:
    from agent_zero_cli.app import AgentZeroCLI

    app = AgentZeroCLI(config=_snapshot_config())

    async with app.run_test(size=(width, height)) as pilot:
        # Give the app time to mount and render initial frame.
        await pilot.pause(delay=wait)

        svg = app.export_screenshot()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(svg, encoding="utf-8")
        print(f"Snapshot saved → {output}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Capture a TUI SVG snapshot")
    parser.add_argument(
        "--output", "-o",
        type=Path,
        default=_OUT_DIR / "tui_snapshot.svg",
        help="Output SVG path (default: devtools/snapshots/tui_snapshot.svg)",
    )
    parser.add_argument("--width", type=int, default=120, help="Terminal columns (default: 120)")
    parser.add_argument("--height", type=int, default=36, help="Terminal rows (default: 36)")
    parser.add_argument(
        "--wait", type=float, default=1.0,
        help="Seconds to wait before capture (default: 1.0)",
    )
    args = parser.parse_args()
    asyncio.run(_capture(args.output, args.width, args.height, args.wait))


if __name__ == "__main__":
    main()
