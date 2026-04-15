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
    parser.add_argument(
        "--server",
        type=str,
        default="",
        metavar="URL",
        help="Agent Zero server URL (overrides A0_CLI_SERVER env var).",
    )
    parser.add_argument(
        "--username",
        type=str,
        default="",
        metavar="NAME",
        help="Login username (overrides A0_CLI_USERNAME env var).",
    )
    parser.add_argument(
        "--password",
        type=str,
        default="",
        metavar="PASS",
        help="Login password (overrides A0_CLI_PASSWORD env var).",
    )
    parser.add_argument(
        "--codeexec",
        action="store_true",
        default=False,
        help="Enable remote code execution (F4) on startup.",
    )
    subparsers = parser.add_subparsers(dest="command", title="commands")
    subparsers.add_parser(
        "update",
        help="Update the installed a0 tool and exit.",
    )
    return parser


def _run_app(args: argparse.Namespace) -> None:
    from agent_zero_cli.app import AgentZeroCLI
    from agent_zero_cli.config import load_config

    config = load_config(
        cli_server=args.server,
        cli_username=args.username,
        cli_password=args.password,
        cli_codeexec=args.codeexec,
    )
    app = AgentZeroCLI(config)
    app.run()


def _run_self_update() -> int:
    from agent_zero_cli.self_update import run_self_update_handoff

    return run_self_update_handoff()


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.version:
        print(__version__)
        return 0

    if args.command == "update":
        return _run_self_update()

    _run_app(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
