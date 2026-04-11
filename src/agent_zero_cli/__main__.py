from __future__ import annotations

import argparse
from collections.abc import Sequence

from agent_zero_cli import __version__


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="a0",
        description="Terminal chat interface for Agent Zero.",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="Print the installed a0 version and exit.",
    )
    return parser


def _run_app() -> None:
    from agent_zero_cli.app import AgentZeroCLI
    from agent_zero_cli.config import load_config

    config = load_config()
    app = AgentZeroCLI(config)
    app.run()


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.version:
        print(__version__)
        return 0

    _run_app()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
