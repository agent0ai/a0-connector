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
from agent_zero_cli.config import delete_env, save_env
from agent_zero_cli.widgets import ChatInput
from agent_zero_cli.widgets.chat_log import ChatLog

if TYPE_CHECKING:
    from agent_zero_cli.app import AgentZeroCLI


async def startup(app: AgentZeroCLI) -> None:
    host = app.config.instance_url.strip() or DEFAULT_HOST
    app._set_splash_stage(
        "host",
        message="",
        detail="",
        host=host,
    )
    app._set_splash_state(
        discovery_status="loading",
        discovery_detail="",
        selected_host_url="",
        manual_entry_expanded=False,
    )
    app._sync_connection_status("disconnected", "")
    app._focus_splash_primary()
    app._start_instance_discovery(auto_connect_single=True)


async def fetch_capabilities(app: AgentZeroCLI) -> tuple[dict[str, Any] | None, bool, str]:
    try:
        return await app.client.fetch_capabilities(), False, ""
    except A0ConnectorPluginMissingError as exc:
        return None, True, str(exc)
    except Exception as exc:
        return None, False, str(exc)


async def _silently_disconnect_websocket(app: AgentZeroCLI) -> None:
    try:
        await app.client.disconnect(close_http=False, notify=False)
    except Exception:
        pass


def validate_capabilities(
    capabilities: dict[str, Any],
    protocol_version: str = PROTOCOL_VERSION,
    ws_namespace: str = WS_NAMESPACE,
    ws_handler: str = WS_HANDLER,
) -> None:
    protocol = capabilities.get("protocol")
    namespace = capabilities.get("websocket_namespace")
    handlers = capabilities.get("websocket_handlers") or []
    auth_modes = capabilities.get("auth")
    auth_required = capabilities.get("auth_required")
    features = capabilities.get("features") or []

    if protocol != protocol_version:
        raise ValueError(f"Unsupported connector protocol: expected {protocol_version}, got {protocol!r}")
    if namespace != ws_namespace:
        raise ValueError(f"Unsupported WebSocket namespace: expected {ws_namespace}, got {namespace!r}")
    if not isinstance(handlers, list) or ws_handler not in handlers:
        raise ValueError(f"Connector handler activation is missing {ws_handler!r} in capabilities")
    if auth_modes != ["session"]:
        raise ValueError(f"Unsupported connector auth contract: expected ['session'], got {auth_modes!r}")
    if not isinstance(auth_required, bool):
        raise ValueError("Connector capabilities must include boolean auth_required")
    if not isinstance(features, list):
        raise ValueError("Connector capabilities features payload is invalid")
    if "connector_login" in features:
        raise ValueError("Connector capabilities still advertise the removed connector_login feature")


def _chat_identifier(chat: dict[str, Any]) -> str:
    return str(chat.get("id") or chat.get("context_id") or chat.get("ctxid") or "").strip()


async def _resolve_initial_context(app: AgentZeroCLI, host: str) -> tuple[str, bool]:
    saved_context_id = app._saved_context_for_host(host)
    if saved_context_id:
        try:
            contexts = await app.client.list_chats()
        except Exception:
            contexts = []

        selected = next(
            (context for context in contexts if _chat_identifier(context) == saved_context_id),
            None,
        )
        if selected is not None:
            has_messages_hint = bool(selected.get("last_message"))
            if not has_messages_hint and "chat_get" in app.connector_features:
                try:
                    metadata = await app.client.get_chat(saved_context_id)
                except Exception:
                    metadata = {}
                has_messages_hint = bool(metadata.get("last_message") or metadata.get("log_entries"))
            return saved_context_id, has_messages_hint

    return await app.client.create_chat(), False


async def begin_connection(
    app: AgentZeroCLI,
    host: str,
    *,
    username: str = "",
    password: str = "",
    remember_host_flag: bool = False,
) -> None:
    app._stop_remote_tree_publisher()
    app._stop_token_refresh()
    app._clear_token_usage()
    await app._hide_project_menu()
    await app._hide_profile_menu()
    app._clear_project_state()
    app._last_remote_tree_hash = ""
    normalized_host = app._normalize_host(host)
    app.config.instance_url = normalized_host
    app.client.base_url = normalized_host.rstrip("/")
    await _silently_disconnect_websocket(app)
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
        remember_host=remember_host_flag,
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
            remember_host=remember_host_flag,
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
            remember_host=remember_host_flag,
        )
        return

    app.capabilities = capabilities
    app.connector_features = set(capabilities.get("features") or [])
    auth_required = bool(capabilities.get("auth_required"))
    if auth_required:
        try:
            session_ok = await app.client.verify_session()
        except Exception as exc:
            app._sync_connection_status("disconnected", normalized_host)
            app._set_splash_stage(
                "error",
                message="Session verification failed",
                detail=str(exc),
                host=normalized_host,
                username=username,
                password="",
                remember_host=remember_host_flag,
            )
            return

        if not session_ok and username and password:
            app._set_splash_stage(
                "connecting",
                message="Signing in...",
                detail=normalized_host,
                host=normalized_host,
                username=username,
                password=password,
                remember_host=remember_host_flag,
            )
            try:
                session_ok = await app.client.login(username, password)
            except Exception as exc:
                app._sync_connection_status("disconnected", normalized_host)
                app._set_splash_stage(
                    "error",
                    message="Login failed",
                    detail=str(exc),
                    host=normalized_host,
                    username=username,
                    password="",
                    remember_host=remember_host_flag,
                )
                return

        if not session_ok:
            app._sync_connection_status("disconnected", normalized_host)
            app._set_splash_stage(
                "login",
                message="",
                detail="",
                host=normalized_host,
                username=username,
                password="",
                remember_host=remember_host_flag,
                login_error="Wrong username or password: retry." if username or password else "",
            )
            app._focus_splash_primary()
            return

    app.client.on_connect = lambda: app._run_on_ui(app._set_connected, True)
    app.client.on_disconnect = lambda: app._run_on_ui(app._set_connected, False)
    app.client.on_context_snapshot = lambda data: app._run_on_ui(app._handle_context_snapshot, data)
    app.client.on_context_event = lambda data: app._run_on_ui(app._handle_context_event, data)
    app.client.on_context_complete = lambda data: app._run_on_ui(app._handle_context_complete, data)
    app.client.on_error = lambda data: app._run_on_ui(app._handle_connector_error, data)
    app.client.on_file_op = app._handle_file_op
    app.client.on_exec_op = app._handle_exec_op
    app.client.on_computer_use_op = app._handle_computer_use_op

    try:
        await app.client.connect_websocket()
        hello = await app.client.send_hello(
            computer_use=app._computer_use_metadata(),
            remote_files=app._remote_file_metadata(),
            remote_exec=app._remote_exec_metadata(),
        )
        app._python_tty.set_exec_config(hello.get("exec_config") if isinstance(hello, dict) else None)
    except Exception as exc:
        await _silently_disconnect_websocket(app)
        app._sync_connection_status("disconnected", normalized_host)
        app._set_splash_stage(
            "error",
            message="WebSocket connection failed",
            detail=str(exc),
            host=normalized_host,
        )
        return

    try:
        context_id, has_messages_hint = await _resolve_initial_context(app, normalized_host)
    except Exception as exc:
        await _silently_disconnect_websocket(app)
        app._sync_connection_status("disconnected", normalized_host)
        app._set_splash_stage(
            "error",
            message="Failed to create the initial chat",
            detail=str(exc),
            host=normalized_host,
        )
        return

    app.current_context = context_id
    app.current_context_has_messages = has_messages_hint
    app._response_delivered = False
    app._context_run_complete = False
    app._chat_intro_pending = True
    app.query_one("#chat-log", ChatLog).clear()
    app._set_idle()

    try:
        await app.client.subscribe_context(context_id)
    except Exception as exc:
        await _silently_disconnect_websocket(app)
        app._sync_connection_status("disconnected", normalized_host)
        app._set_splash_stage(
            "error",
            message="Failed to subscribe to the initial chat",
            detail=str(exc),
            host=normalized_host,
        )
        return

    app._remember_context(context_id, host=normalized_host)
    app.connected = True
    app._sync_connection_status("connected", normalized_host)
    input_widget = app.query_one("#message-input", ChatInput)
    input_widget.disabled = False
    app._start_remote_tree_publisher()
    if remember_host_flag:
        save_env("AGENT_ZERO_HOST", normalized_host)
        delete_env("AGENT_ZERO_API_KEY")
    app._set_splash_stage(
        "ready",
        message="Ready when you are.",
        detail=normalized_host,
        host=normalized_host,
        username=username if auth_required else "",
        password="",
        remember_host=remember_host_flag,
        login_error="",
        actions=app._welcome_actions(),
    )
    await app._refresh_model_switcher()
    await app._refresh_workspace_from_settings()
    await app._refresh_projects(context_id=context_id)
    await app._refresh_token_usage(context_id=context_id)
    app._start_token_refresh()
    app._sync_body_mode()
    app._focus_message_input()


def _reset_disconnected_state(app: AgentZeroCLI) -> None:
    app.connected = False
    app.agent_active = False
    app.current_context = None
    app.current_context_has_messages = False
    app._response_delivered = False
    app._context_run_complete = False
    app._chat_intro_pending = True
    app.capabilities = {}
    app.connector_features = set()
    app._slash_palette_query = None
    app._sync_connection_status("disconnected")
    input_widget = app.query_one("#message-input", ChatInput)
    input_widget.disabled = True
    app.query_one("#chat-log", ChatLog).clear()
    app._set_idle()
    if app.is_running:
        asyncio.create_task(app._hide_project_menu())
        asyncio.create_task(app._hide_profile_menu())
    app._cancel_compaction_refresh()
    app._set_pause_latched(False)
    app._stop_remote_tree_publisher()
    app._stop_token_refresh()
    app._clear_token_usage()
    app._clear_project_state()
    app._set_workspace_context(remote_workspace="")
    app._python_tty.set_exec_config(None)
    asyncio.create_task(app._python_tty.close())
    asyncio.create_task(app._computer_use.disconnect())
    app._sync_computer_use_status()
    app._clear_model_switcher()
    app._sync_body_mode()


def set_connected(app: AgentZeroCLI, value: bool) -> None:
    if value:
        app.connected = True
        app._sync_connection_status("connected")
        input_widget = app.query_one("#message-input", ChatInput)
        input_widget.disabled = False
        return

    _reset_disconnected_state(app)
    app._set_splash_stage(
        "error",
        message="Connection lost",
        detail=app.config.instance_url or app._splash_host(),
        host=app._splash_host(),
    )


async def disconnect_to_login(app: AgentZeroCLI) -> None:
    current_host = app.config.instance_url or app._splash_host()
    auth_required = bool(app.capabilities.get("auth_required"))
    username = app._splash_state.username
    remember_host = app._splash_state.remember_host

    app.client.on_disconnect = None
    try:
        await app.client.disconnect(close_http=False)
    except Exception:
        pass
    try:
        await app.client.logout()
    except Exception:
        pass
    app.client.clear_session()

    _reset_disconnected_state(app)
    app._sync_connection_status("disconnected", current_host)

    if auth_required and current_host:
        app._set_splash_stage(
            "login",
            message="",
            detail="",
            host=current_host,
            username=username,
            password="",
            remember_host=remember_host,
            login_error="",
        )
    else:
        app._set_splash_stage(
            "host",
            message="",
            detail="",
            host=current_host or DEFAULT_HOST,
            username="",
            password="",
            remember_host=remember_host,
            login_error="",
        )
    app._focus_splash_primary()


async def disconnect_and_exit(app: AgentZeroCLI) -> None:
    app._stop_remote_tree_publisher()
    app._stop_token_refresh()
    await app._python_tty.close()
    await app._computer_use.disconnect()
    try:
        await app.client.disconnect()
    except Exception:
        pass
    app.exit()
