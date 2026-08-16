from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

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
    connection_options.add_argument(
        "--connect",
        action="store_true",
        help="Connect to the configured host immediately instead of opening the host picker.",
    )
    chat_options = connection_options.add_mutually_exclusive_group()
    chat_options.add_argument(
        "--chat",
        metavar="CONTEXT_ID",
        help=(
            "Open a specific chat context after connecting. "
            "Defaults to AGENT_ZERO_DEFAULT_CONTEXT_ID, A0_DEFAULT_CHAT, "
            "or the last remembered chat for the host."
        ),
    )
    chat_options.add_argument(
        "--chat-last",
        action="store_true",
        help="Ignore any configured default chat and restore the last remembered chat for the host.",
    )
    subparsers = parser.add_subparsers(dest="command", title="commands")
    subparsers.add_parser(
        "update",
        help="Update the installed a0 tool and exit.",
    )
    headless = subparsers.add_parser(
        "headless",
        help="Run a plain stdin/stdout connector session without the Textual TUI.",
    )
    headless_connection = headless.add_argument_group("connection")
    headless_connection.add_argument(
        "--host",
        dest="headless_host",
        metavar="URL",
        help="Agent Zero base URL. Defaults to AGENT_ZERO_HOST or single-instance Docker discovery.",
    )
    headless_chat = headless_connection.add_mutually_exclusive_group()
    headless_chat.add_argument(
        "--chat",
        dest="headless_chat",
        metavar="CONTEXT_ID",
        help="Open a specific chat context after connecting.",
    )
    headless_chat.add_argument(
        "--chat-last",
        dest="headless_chat_last",
        action="store_true",
        help="Restore the last remembered chat for the selected host.",
    )
    headless_chat.add_argument(
        "--new-chat",
        dest="headless_new_chat",
        action="store_true",
        help="Create a new chat context after connecting.",
    )
    headless_connection.add_argument(
        "--no-docker-discovery",
        dest="headless_no_docker_discovery",
        action="store_true",
        help="Skip Docker host discovery when no host is configured.",
    )
    headless.add_argument(
        "--output",
        choices=("text", "jsonl"),
        default="text",
        help="Select human text or machine-readable JSONL output.",
    )
    headless.add_argument(
        "--print",
        "-p",
        dest="print_prompt",
        nargs="?",
        const="",
        metavar="PROMPT",
        help="One-shot mode: send PROMPT or stdin, stream output, and exit on completion.",
    )
    headless.add_argument(
        "--workspace",
        metavar="DIR",
        default=".",
        help="Local workspace root for remote file and exec operations.",
    )
    acp = subparsers.add_parser(
        "acp",
        help="Run an Agent Client Protocol stdio server through Agent Zero.",
    )
    acp.add_argument("--host", dest="acp_host", metavar="URL", help="Agent Zero base URL.")
    acp.add_argument("--workspace", metavar="DIR", default=".", help="Fallback workspace when the ACP client does not provide one.")
    acp.add_argument("--no-docker-discovery", action="store_true", help="Skip local Docker instance discovery.")
    acp.add_argument("--check", action="store_true", help="Verify ACP support and exit.")
    acp.add_argument("--debug", action="store_true", help="Write ACP diagnostics to stderr.")
    acp.add_argument("--transport", choices=("connector", "container"), help=argparse.SUPPRESS)
    acp.add_argument("--container-id", help=argparse.SUPPRESS)
    gateway = subparsers.add_parser(
        "gateway",
        help="Run a Launcher-supervised host-tools gateway over JSONL.",
    )
    gateway.add_argument(
        "--host",
        dest="gateway_host",
        metavar="URL",
        help="Agent Zero base URL. Defaults to AGENT_ZERO_HOST or the saved host.",
    )
    gateway.add_argument("--workspace", metavar="DIR", default=".", help="Host file root and initial command directory.")
    gateway.add_argument("--gateway-id", required=True, metavar="ID", help="Stable sanitized Launcher gateway identity.")
    gateway.add_argument("--host-label", default="", metavar="LABEL", help="Human-readable host name shown in Agent Zero.")
    gateway_master = gateway.add_mutually_exclusive_group()
    gateway_master.add_argument("--master", dest="gateway_master", action="store_true", help="Start host access enabled.")
    gateway_master.add_argument("--no-master", dest="gateway_master", action="store_false", help="Start host access paused.")
    gateway.set_defaults(gateway_master=True)
    gateway.add_argument(
        "--scopes",
        default="file_read,file_write,code_execution",
        metavar="LIST",
        help="Comma-separated file_read, file_write, code_execution, browser, and computer_use scopes.",
    )
    gateway.add_argument(
        "--browser-selection",
        default="",
        metavar="ID",
        help="Chromium profile identifier to use.",
    )
    return parser


def _run_app(
    *,
    host: str = "",
    chat: str = "",
    chat_last: bool = False,
    auto_connect_single: bool = True,
    discover_instances: bool = True,
    connect_configured_host: bool = False,
) -> None:
    from agent_zero_cli.config import load_config
    from agent_zero_cli.image_render import initialize_image_renderer
    from agent_zero_cli.textual_compat import install_textual_linux_input_decoder_guard

    install_textual_linux_input_decoder_guard()
    image_renderer = initialize_image_renderer()

    from agent_zero_cli.app import AgentZeroCLI

    config = load_config()
    if host:
        config.instance_url = host
    if chat:
        config.default_context_id = chat
    elif chat_last:
        config.default_context_id = ""
    app = AgentZeroCLI(
        config,
        auto_connect_single_instance=auto_connect_single,
        discover_instances=discover_instances,
        connect_configured_host=connect_configured_host,
        image_renderer=image_renderer,
    )
    app.run()


def _run_self_update() -> int:
    from agent_zero_cli.self_update import run_self_update_handoff

    return run_self_update_handoff()


def _run_headless(
    *,
    host: str = "",
    chat: str = "",
    chat_last: bool = False,
    new_chat: bool = False,
    output: str = "text",
    print_prompt: str | None = None,
    workspace: str = ".",
    discover_instances: bool = True,
) -> int:
    from agent_zero_cli.config import load_config
    from agent_zero_cli.headless.runner import HeadlessOptions, run_headless

    config = load_config()
    if host:
        config.instance_url = host
    if chat:
        config.default_context_id = chat
    elif chat_last or new_chat:
        config.default_context_id = ""

    return run_headless(
        HeadlessOptions(
            host=host,
            chat=chat,
            chat_last=chat_last,
            new_chat=new_chat,
            output=output,
            print_prompt=print_prompt,
            workspace=Path(workspace),
            discover_instances=discover_instances,
            config=config,
        )
    )


def _run_gateway(
    *,
    host: str,
    workspace: str,
    gateway_id: str,
    host_label: str,
    master_enabled: bool,
    scopes: str,
    browser_selection: str,
) -> int:
    from agent_zero_cli.config import load_config
    from agent_zero_cli.gateway import gateway_options, run_gateway

    config = load_config()
    resolved_host = host or config.instance_url
    options = gateway_options(
        host=resolved_host,
        workspace=workspace,
        gateway_id=gateway_id,
        host_label=host_label,
        master_enabled=master_enabled,
        scopes=scopes,
        browser_selection=browser_selection,
    )
    return run_gateway(options, config)


def _run_acp(
    *,
    host: str,
    workspace: str,
    discover_instances: bool,
    check: bool,
    debug: bool,
    transport: str | None,
    container_id: str | None,
) -> int:
    from agent_zero_cli.acp import AcpOptions, run_acp

    return run_acp(
        AcpOptions(
            host=host,
            workspace=Path(workspace),
            discover_instances=discover_instances,
            check=check,
            debug=debug,
            transport=transport or "",
            container_id=container_id or "",
        )
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.version:
        print(__version__)
        return 0

    if args.command == "update":
        return _run_self_update()

    if args.command == "headless":
        return _run_headless(
            host=(args.headless_host or args.host or ""),
            chat=(args.headless_chat or args.chat or ""),
            chat_last=bool(args.headless_chat_last or args.chat_last),
            new_chat=bool(args.headless_new_chat),
            output=args.output,
            print_prompt=args.print_prompt,
            workspace=args.workspace,
            discover_instances=not args.headless_no_docker_discovery,
        )

    if args.command == "gateway":
        return _run_gateway(
            host=(args.gateway_host or args.host or ""),
            workspace=args.workspace,
            gateway_id=args.gateway_id,
            host_label=args.host_label,
            master_enabled=args.gateway_master,
            scopes=args.scopes,
            browser_selection=args.browser_selection,
        )

    if args.command == "acp":
        return _run_acp(
            host=(args.acp_host or args.host or ""),
            workspace=args.workspace,
            discover_instances=not args.no_docker_discovery,
            check=bool(args.check),
            debug=bool(args.debug),
            transport=args.transport,
            container_id=args.container_id,
        )

    _run_app(
        host=args.host or "",
        chat=args.chat or "",
        chat_last=bool(args.chat_last),
        auto_connect_single=not args.no_auto_connect,
        discover_instances=not args.no_docker_discovery,
        connect_configured_host=bool(args.connect),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
