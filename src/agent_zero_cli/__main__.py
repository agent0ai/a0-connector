from __future__ import annotations

import argparse
from collections.abc import Sequence

from agent_zero_cli import __version__
from agent_zero_cli.client import DEFAULT_HOST


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="a0",
        description="Terminal chat interface for Agent Zero.",
        epilog=(
            "Connection defaults resolve from --host, then AGENT_ZERO_HOST, then "
            f"~/.agent-zero/.env, falling back to {DEFAULT_HOST}."
        ),
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="Print the installed a0 version and exit.",
    )
    connection_options = parser.add_argument_group("connection")
    connection_options.add_argument(
        "--host",
        metavar="URL",
        help=(
            "Prefill the host picker with an Agent Zero base URL. "
            f"Defaults to AGENT_ZERO_HOST or {DEFAULT_HOST}."
        ),
    )
    connection_options.add_argument(
        "--no-auto-connect",
        action="store_true",
        help="Do not auto-connect when Docker discovery finds exactly one local Agent Zero instance.",
    )
    connection_options.add_argument(
        "--no-docker-discovery",
        action="store_true",
        help="Skip Docker host discovery and open manual URL entry immediately.",
    )
    subparsers = parser.add_subparsers(dest="command", title="commands")
    subparsers.add_parser(
        "update",
        help="Update the installed a0 tool and exit.",
    )
    return parser


def _run_app(
    *,
    host: str = "",
    auto_connect_single: bool = True,
    discover_instances: bool = True,
) -> None:
    from agent_zero_cli.app import AgentZeroCLI
    from agent_zero_cli.config import load_config

    config = load_config()
    if host:
        config.instance_url = host
    app = AgentZeroCLI(
        config,
        auto_connect_single_instance=auto_connect_single,
        discover_instances=discover_instances,
    )
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

    _run_app(
        host=args.host or "",
        auto_connect_single=not args.no_auto_connect,
        discover_instances=not args.no_docker_discovery,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
