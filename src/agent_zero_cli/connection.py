from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from agent_zero_cli.client import (
    A0ConnectorPluginMissingError,
    DEFAULT_HOST,
    PROTOCOL_VERSION,
    WS_HANDLER,
    WS_NAMESPACE,
)
from agent_zero_cli.config import save_env
from agent_zero_cli.widgets import ChatInput
from agent_zero_cli.widgets.chat_log import ChatLog

if TYPE_CHECKING:
    from agent_zero_cli.app import AgentZeroCLI


async def startup(app: AgentZeroCLI) -> None:
    host = app.config.instance_url.strip()
    if not host:
        app._set_splash_stage(
            "host",
            message="Enter the Agent Zero WebUI URL and port.",
            detail="",
            host=DEFAULT_HOST,
        )
        app._sync_connection_status("disconnected", "")
        app._focus_splash_primary()
        return

    await app._begin_connection(host)


async def fetch_capabilities(app: AgentZeroCLI) -> tuple[dict[str, Any] | None, bool, str]:
    try:
        return await app.client.fetch_capabilities(), False, ""
    except A0ConnectorPluginMissingError as exc:
        return None, True, str(exc)
    except Exception as exc:
        return None, False, str(exc)


def validate_capabilities(
    capabilities: dict[str, Any],
    protocol_version: str = PROTOCOL_VERSION,
    ws_namespace: str = WS_NAMESPACE,
    ws_handler: str = WS_HANDLER,
) -> None:
    protocol = capabilities.get("protocol")
    namespace = capabilities.get("websocket_namespace")
    handlers = capabilities.get("websocket_handlers") or []
    auth_modes = capabilities.get("auth") or []

    if protocol != protocol_version:
        raise ValueError(f"Unsupported connector protocol: expected {protocol_version}, got {protocol!r}")
    if namespace != ws_namespace:
        raise ValueError(f"Unsupported WebSocket namespace: expected {ws_namespace}, got {namespace!r}")
    if not isinstance(handlers, list) or ws_handler not in handlers:
        raise ValueError(f"Connector handler activation is missing {ws_handler!r} in capabilities")
    if "api_key" not in auth_modes:
        raise ValueError("Connector capabilities do not advertise API-key auth")


async def begin_connection(
    app: AgentZeroCLI,
    host: str,
    *,
    username: str = "",
    password: str = "",
    save_credentials_flag: bool = False,
) -> None:
    app._stop_remote_tree_publisher()
    app._stop_token_refresh()
    app._clear_token_usage()
    app._last_remote_tree_hash = ""
    normalized_host = app._normalize_host(host)
    app.config.instance_url = normalized_host
    app.client.base_url = normalized_host.rstrip("/")
    app.client.api_key = app.config.api_key
    app._sync_connection_status("connecting", normalized_host)
    app.query_one("#message-input", ChatInput).disabled = True
    app._slash_palette_query = None
    app._set_splash_stage(
        "connecting",
        message="Probing connector capabilities...",
        detail=normalized_host,
        host=normalized_host,
        username=username,
        password=password,
        save_credentials=save_credentials_flag,
    )

    capabilities, plugin_missing, capability_error = await app._fetch_capabilities()
    if capabilities is None:
        message = "Connector unavailable" if not plugin_missing else "Connector plugin missing"
        app._sync_connection_status("disconnected", normalized_host)
        app._set_splash_stage(
            "error",
            message=message,
            detail=capability_error or normalized_host,
            host=normalized_host,
            username=username,
            password="",
            save_credentials=save_credentials_flag,
        )
        app._focus_splash_primary()
        return

    try:
        app._validate_capabilities(capabilities)
    except ValueError as exc:
        app._sync_connection_status("disconnected", normalized_host)
        app._set_splash_stage(
            "error",
            message="Connector contract mismatch",
            detail=str(exc),
            host=normalized_host,
            username=username,
            password="",
            save_credentials=save_credentials_flag,
        )
        return

    app.capabilities = capabilities
    app.connector_features = set(capabilities.get("features") or [])
    auth_modes = capabilities.get("auth") or []

    if not app.config.api_key and "login" in auth_modes and username and password:
        app._set_splash_stage(
            "connecting",
            message="Signing in...",
            detail=normalized_host,
            host=normalized_host,
            username=username,
            password=password,
            save_credentials=save_credentials_flag,
        )
        api_key = await app.client.login(username, password)
        if not api_key:
            app._sync_connection_status("disconnected", normalized_host)
            app._set_splash_stage(
                "login",
                message="",
                detail="",
                host=normalized_host,
                username=username,
                password="",
                save_credentials=save_credentials_flag,
                login_error="Wrong username or password: retry.",
            )
            app._focus_splash_primary()
            return

        app.config.api_key = api_key
        app.client.api_key = api_key
        if save_credentials_flag:
            save_env("AGENT_ZERO_HOST", normalized_host)
            save_env("AGENT_ZERO_API_KEY", api_key)

    elif not app.config.api_key and "login" in auth_modes:
        app._sync_connection_status("disconnected", normalized_host)
        app._set_splash_stage(
            "login",
            message="",
            detail="",
            host=normalized_host,
            username=username,
            password="",
            save_credentials=save_credentials_flag,
            login_error="",
        )
        app._focus_splash_primary()
        return

    elif not app.config.api_key and "api_key" in auth_modes:
        app._sync_connection_status("disconnected", normalized_host)
        app._set_splash_stage(
            "error",
            message="No API key available",
            detail="Set AGENT_ZERO_API_KEY or connect to a server that supports login auth.",
            host=normalized_host,
        )
        return

    if app.config.api_key:
        try:
            api_key_ok = await app.client.verify_api_key()
        except Exception as exc:
            app._sync_connection_status("disconnected", normalized_host)
            app._set_splash_stage(
                "error",
                message="API-key verification failed",
                detail=str(exc),
                host=normalized_host,
            )
            return

        if not api_key_ok:
            app.config.api_key = ""
            app.client.api_key = ""
            app._sync_connection_status("disconnected", normalized_host)
            if "login" in auth_modes:
                app._set_splash_stage(
                    "login",
                    message="",
                    detail="",
                    host=normalized_host,
                    username=username,
                    password="",
                    save_credentials=save_credentials_flag,
                    login_error="Saved API key was rejected. Sign in again to refresh the connector token.",
                )
                app._focus_splash_primary()
            else:
                app._set_splash_stage(
                    "error",
                    message="API key rejected",
                    detail="The connector rejected the configured API key.",
                    host=normalized_host,
                )
            return

    app.client.on_connect = lambda: app._run_on_ui(app._set_connected, True)
    app.client.on_disconnect = lambda: app._run_on_ui(app._set_connected, False)
    app.client.on_context_snapshot = lambda data: app._run_on_ui(app._handle_context_snapshot, data)
    app.client.on_context_event = lambda data: app._run_on_ui(app._handle_context_event, data)
    app.client.on_context_complete = lambda data: app._run_on_ui(app._handle_context_complete, data)
    app.client.on_error = lambda data: app._run_on_ui(app._handle_connector_error, data)
    app.client.on_file_op = app._handle_file_op
    app.client.on_exec_op = app._handle_exec_op

    try:
        await app.client.connect_websocket()
        await app.client.send_hello()
    except Exception as exc:
        app._sync_connection_status("disconnected", normalized_host)
        app._set_splash_stage(
            "error",
            message="WebSocket connection failed",
            detail=str(exc),
            host=normalized_host,
        )
        return

    try:
        context_id = await app.client.create_chat()
    except Exception as exc:
        app._sync_connection_status("disconnected", normalized_host)
        app._set_splash_stage(
            "error",
            message="Failed to create the initial chat",
            detail=str(exc),
            host=normalized_host,
        )
        return

    app.current_context = context_id
    app.current_context_has_messages = False
    app._response_delivered = False
    app._context_run_complete = False
    app._chat_intro_pending = True
    app.query_one("#chat-log", ChatLog).clear()
    app._set_idle()

    try:
        await app.client.subscribe_context(context_id)
    except Exception as exc:
        app._sync_connection_status("disconnected", normalized_host)
        app._set_splash_stage(
            "error",
            message="Failed to subscribe to the initial chat",
            detail=str(exc),
            host=normalized_host,
        )
        return

    app.connected = True
    app._sync_connection_status("connected", normalized_host)
    input_widget = app.query_one("#message-input", ChatInput)
    input_widget.disabled = False
    app._start_remote_tree_publisher()
    app._set_splash_stage(
        "ready",
        message="Ready when you are.",
        detail=normalized_host,
        host=normalized_host,
        actions=app._welcome_actions(),
    )
    await app._refresh_model_switcher()
    await app._refresh_workspace_from_settings()
    await app._refresh_token_usage(context_id=context_id)
    app._start_token_refresh()
    app._sync_body_mode()
    app._focus_message_input()


def set_connected(app: AgentZeroCLI, value: bool) -> None:
    app.connected = value
    app._sync_connection_status("connected" if value else "disconnected")
    input_widget = app.query_one("#message-input", ChatInput)
    input_widget.disabled = not value
    if not value:
        app._cancel_compaction_refresh()
        app._set_pause_latched(False)
        app._stop_remote_tree_publisher()
        app._stop_token_refresh()
        app._clear_token_usage()
        app._set_workspace_context(remote_workspace="")
        asyncio.create_task(app._python_tty.close())
        app._clear_model_switcher()
        app._set_splash_stage(
            "error",
            message="Connection lost",
            detail=app.config.instance_url or app._splash_host(),
            host=app._splash_host(),
        )
        app._sync_body_mode()


async def disconnect_and_exit(app: AgentZeroCLI) -> None:
    app._stop_remote_tree_publisher()
    app._stop_token_refresh()
    await app._python_tty.close()
    try:
        await app.client.disconnect()
    except Exception:
        pass
    app.exit()
