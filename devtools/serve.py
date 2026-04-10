#!/usr/bin/env python
"""Launch the Agent Zero CLI TUI in a browser via textual-serve.

Usage:
    python devtools/serve.py [--port PORT] [--host HOST]

Opens http://localhost:PORT in a browser where you can interact with the
full TUI exactly as you would in a terminal.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_VENV_PYTHON_CANDIDATES = (
    _PROJECT_ROOT / ".venv" / "Scripts" / "python.exe",
    _PROJECT_ROOT / ".venv" / "Scripts" / "python",
    _PROJECT_ROOT / ".venv" / "bin" / "python",
)


def _resolve_python() -> str:
    """Prefer the project's venv interpreter across Windows and POSIX."""

    for candidate in _VENV_PYTHON_CANDIDATES:
        try:
            if candidate.is_file():
                return str(candidate)
        except OSError:
            continue
    return sys.executable


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve the TUI in a browser")
    parser.add_argument("--port", type=int, default=8566, help="HTTP port (default: 8566)")
    parser.add_argument("--host", type=str, default="localhost", help="Bind host (default: localhost)")
    parser.add_argument("--debug", action="store_true", help="Enable textual debug/devtools mode")
    args = parser.parse_args()

    try:
        from textual_serve.server import Server
    except ImportError:
        python = _resolve_python()
        print("textual-serve is not installed. Run:")
        print(f"  {python} -m pip install textual-serve")
        sys.exit(1)

    python = _resolve_python()
    command = f'"{python}" -m agent_zero_cli'

    server = Server(
        command,
        host=args.host,
        port=args.port,
        title="Agent Zero CLI (dev)",
    )
    server.serve(debug=args.debug)


if __name__ == "__main__":
    main()
