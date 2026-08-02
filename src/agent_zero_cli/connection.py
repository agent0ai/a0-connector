from __future__ import annotations

import asyncio
import contextlib
import os
from typing import TYPE_CHECKING, Any

from agent_zero_cli.client import (
    A0ConnectorPluginMissingError,
    DEFAULT_HOST,
)
from agent_zero_cli.config import delete_env, save_env, save_remember_host
from agent_zero_cli.protocol import connector_version_warning, validate_capabilities
from agent_zero_cli.widgets import ChatInput
from agent_zero_cli.widgets.chat_log import ChatLog

if TYPE_CHECKING:
    from agent_zero_cli.app import AgentZeroCLI

_RECOVERY_DELAYS_SECONDS = (1.0, 2.0, 5.0, 10.0, 20.0)
# After the initial fast retries, keep retrying at a steady cadence forever.
# A container restart or long server outage used to exhaust the 5 bounded
# attempts (~38s) and leave the CLI permanently disconnected until manual
# intervention, which also stranded remote execution server-side.
_RECOVERY_STEADY_DELAY_SECONDS = 30.0


def _environment_login_credentials() -> tuple[str, str]:
    username = os.environ.get("A0_USERNAME", "").strip()
    password = os.environ.get("A0_PASSWORD", "")
    return username, password


def _connection_login_credentials(username: str = "", password: str = "") -> tuple[str, str]:
    explicit_username = (username or "").strip()
    explicit_password = password or ""
    if explicit_username or explicit_password:
        return explicit_username, explicit_password
    return _environment_login_credentials()


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
    if app._connect_configured_host:
        await app._begin_connection(host)
        return
    app._start_instance_discovery(auto_connect_single=app._auto_connect_single_instance)


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


def _chat_identifier(chat: dict[str, Any]) -> str:
    return str(chat.get("id") or chat.get("context_id") or chat.get("ctxid") or "").strip()


async def _resolve_initial_context(app: AgentZeroCLI, host: str) -> tuple[str, bool]:
    default_context_id = app.config.default_context_id.strip()
    if default_context_id:
        has_messages_hint = False
        if "chat_get" in app.connector_features:
            try:
                metadata = await app.client.get_chat(default_context_id)
            except Exception:
                metadata = {}
            has_messages_hint = bool(metadata.get("last_message") or metadata.get("log_entries"))
        return default_context_id, has_messages_hint

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
    username, password = _connection_login_credentials(username, password)
    app._stop_remote_tree_publisher()
    app._stop_token_refresh()
    app._stop_state_sync()
    app._clear_token_usage()
    await app._hide_project_menu()
    await app._hide_profile_menu()
    app._clear_project_state()
    app._last_remote_tree_hash = ""
    app._last_remote_tree_published_at = 0.0
    normalized_host = app._normalize_host(host)
    remembered_host_before_connect = app.config.instance_url.strip().rstrip("/")
    remember_flag_before_connect = app.config.remember_host
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
        if remember_host_flag:
            app.client.restore_session(normalized_host)
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

        if not session_ok:
            app.client.clear_session()
            if remember_host_flag:
                app.client.clear_persisted_session(normalized_host)

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
    app.client.on_disconnect = lambda: app._run_on_ui(_schedule_websocket_recovery, app)
    app.client.on_context_snapshot = lambda data: app._run_on_ui(app._handle_context_snapshot, data)
    app.client.on_context_event = lambda data: app._run_on_ui(app._handle_context_event, data)
    app.client.on_context_complete = lambda data: app._run_on_ui(app._handle_context_complete, data)
    app.client.on_message_queue_updated = lambda data: app._run_on_ui(app._handle_message_queue_updated, data)
    app.client.on_error = lambda data: app._run_on_ui(app._handle_connector_error, data)
    app.client.on_settings_updated = lambda data: app._run_on_ui(app._handle_settings_updated, data)
    app.client.on_file_op = app._handle_file_op
    app.client.on_exec_op = app._handle_exec_op
    app.client.on_computer_use_op = app._handle_computer_use_op
    app.client.on_computer_use_op_result_sent = (
        lambda request, _result: _refresh_metadata_after_computer_use_result(app, request)
    )
    app.client.on_browser_op = app._handle_browser_op
    app.client.on_browser_op_result_sent = (
        lambda request, _result: _refresh_metadata_after_browser_result(app, request)
    )

    try:
        await app.client.connect_websocket()
        hello = await app.client.send_hello(
            computer_use=app._computer_use_metadata(),
            host_browser=app._host_browser_metadata(),
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
    app._remember_context_tab(context_id, has_messages_hint=has_messages_hint)
    await app._refresh_context_tab_metadata(context_id, has_messages_hint=has_messages_hint)
    app._response_delivered = False
    app._context_run_complete = False
    app._chat_intro_pending = True
    app.query_one("#chat-log", ChatLog).clear()
    app._set_idle()
    app._set_message_queue([])

    try:
        await app.client.subscribe_context(context_id, history="tail")
        await app._refresh_remote_tool_metadata()
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
    app._sync_context_tabs()
    input_widget = app.query_one("#message-input", ChatInput)
    input_widget.set_history_context(context_id)
    input_widget.disabled = False
    app._start_remote_tree_publisher()
    if remember_host_flag:
        save_env("AGENT_ZERO_HOST", normalized_host)
        save_remember_host(True)
        delete_env("AGENT_ZERO_API_KEY")
        if auth_required:
            app.client.persist_session(normalized_host)
        app.config.remember_host = True
    else:
        app.client.clear_persisted_session(normalized_host)
        if remembered_host_before_connect == normalized_host:
            delete_env("AGENT_ZERO_HOST")
            save_remember_host(False)
            app.config.remember_host = False
        else:
            app.config.remember_host = remember_flag_before_connect
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
    if warning := connector_version_warning(capabilities):
        app._show_notice(warning, error=True)
    await app._refresh_goal_bar()
    await app._refresh_model_switcher()
    await app._refresh_settings_snapshot()
    await app._refresh_projects(context_id=context_id)
    await app._refresh_token_usage(context_id=context_id)
    app._start_state_sync()
    app._start_token_refresh()
    app._sync_body_mode()
    app._focus_message_input()


def _refresh_metadata_after_computer_use_result(
    app: AgentZeroCLI,
    request: dict[str, Any],
) -> None:
    action = str(request.get("action") or "").strip().lower().replace("-", "_")
    if action in {"start_session", "stop_session"}:
        _schedule_remote_tool_metadata_refresh(app)


def _refresh_metadata_after_browser_result(
    app: AgentZeroCLI,
    request: dict[str, Any],
) -> None:
    action = str(request.get("action") or "").strip().lower().replace("-", "_")
    if action in {"ensure", "open", "close", "close_all"}:
        _schedule_remote_tool_metadata_refresh(app)


def _schedule_remote_tool_metadata_refresh(app: AgentZeroCLI) -> None:
    async def refresh() -> None:
        with contextlib.suppress(Exception):
            await app._refresh_remote_tool_metadata()

    asyncio.create_task(refresh())


def _schedule_websocket_recovery(app: AgentZeroCLI) -> None:
    task = getattr(app, "_websocket_recovery_task", None)
    if task is not None and not task.done():
        return
    app._websocket_recovery_task = asyncio.create_task(_recover_websocket(app))


def _mark_reconnecting(app: AgentZeroCLI) -> None:
    app.connected = False
    app._sync_connection_status("connecting", app.config.instance_url or app._splash_host())
    app.query_one("#message-input", ChatInput).disabled = True
    app._stop_remote_tree_publisher()


async def _recover_websocket(app: AgentZeroCLI) -> None:
    context_id = str(app.current_context or "").strip()
    if not context_id:
        app._websocket_recovery_task = None
        set_connected(app, False)
        return

    try:
        _mark_reconnecting(app)
        attempt = 0
        last_error = ""
        base_url = app.client.base_url
        while str(app.current_context or "").strip() == context_id:
            if app.client.base_url != base_url:
                # The user connected to a different host meanwhile; that
                # connection owns the client now, so stop recovering this one.
                return
            attempt += 1
            delay = (
                _RECOVERY_DELAYS_SECONDS[attempt - 1]
                if attempt <= len(_RECOVERY_DELAYS_SECONDS)
                else _RECOVERY_STEADY_DELAY_SECONDS
            )
            detail = f"{app.config.instance_url or app._splash_host()} (attempt {attempt})"
            if last_error:
                detail = f"{detail} — last error: {last_error}"
            app._set_splash_stage(
                "connecting",
                message="Connection lost; reconnecting...",
                detail=detail,
                host=app._splash_host(),
            )
            await asyncio.sleep(delay)
            if str(app.current_context or "").strip() != context_id or (
                app.client.base_url != base_url
            ):
                # Re-check both stop conditions after the sleep: a host switch
                # updates client.base_url before it updates the context, so
                # the context check alone is not enough here.
                return
            try:
                await app.client.connect_websocket()
                hello = await app.client.send_hello(
                    context_id=context_id,
                    computer_use=app._computer_use_metadata(),
                    host_browser=app._host_browser_metadata(),
                    remote_files=app._remote_file_metadata(),
                    remote_exec=app._remote_exec_metadata(),
                )
                exec_config = hello.get("exec_config") if isinstance(hello, dict) else None
                app._python_tty.set_exec_config(exec_config)
                await app.client.subscribe_context(context_id, history="tail")
                await app._publish_remote_tree_snapshot(force=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last_error = str(exc).strip() or exc.__class__.__name__
                continue

            app.connected = True
            app.agent_active = False
            app._context_run_complete = True
            app.query_one("#message-input", ChatInput).disabled = False
            app._sync_connection_status("connected", app.config.instance_url or app._splash_host())
            app._sync_context_tabs()
            app._start_remote_tree_publisher()
            app._set_splash_stage(
                "ready",
                message="Reconnected.",
                detail=app.config.instance_url or app._splash_host(),
                host=app._splash_host(),
                actions=app._welcome_actions(),
            )
            return

        # The context changed (or was cleared) while reconnecting; whoever
        # changed it owns the connection state now, so exit quietly.
        return
    finally:
        # Only clear the registration if this task still owns it: a newer
        # recovery task may have registered itself before our cancellation
        # was delivered.
        if app._websocket_recovery_task is asyncio.current_task():
            app._websocket_recovery_task = None


def _reset_disconnected_state(app: AgentZeroCLI) -> None:
    app.connected = False
    app.agent_active = False
    app.current_context = None
    app.current_context_has_messages = False
    app._clear_context_tabs()
    app._response_delivered = False
    app._context_run_complete = False
    app._chat_intro_pending = True
    app.capabilities = {}
    app.connector_features = set()
    app._slash_palette_query = None
    app._sync_connection_status("disconnected")
    input_widget = app.query_one("#message-input", ChatInput)
    input_widget.set_history_context(None)
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
    app._stop_state_sync()
    app._clear_token_usage()
    app._clear_project_state()
    app._set_workspace_context(remote_workspace="")
    app._settings_snapshot_signature = ""
    app._model_switcher_signature = ""
    app._clear_goal_bar()
    app._model_switcher_signature_pending = ""
    app._model_switcher_signature_pending_retries = 0
    app._last_remote_tree_hash = ""
    app._last_remote_tree_published_at = 0.0
    app._python_tty.set_exec_config(None)
    task = getattr(app, "_websocket_recovery_task", None)
    app._websocket_recovery_task = None
    if task is not None and not task.done():
        task.cancel()
    asyncio.create_task(app._python_tty.close())
    asyncio.create_task(app._computer_use.disconnect())
    asyncio.create_task(app._host_browser.disconnect())
    app._sync_computer_use_status()
    app._clear_model_switcher()
    app._sync_body_mode()


def set_connected(app: AgentZeroCLI, value: bool) -> None:
    if value:
        app.connected = True
        app._sync_connection_status("connected")
        app._sync_context_tabs()
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
    app.client.clear_persisted_session(current_host)
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
    app._stop_state_sync()
    # Cancel any in-flight websocket recovery: with unbounded retries it can
    # otherwise outlive the shutdown until loop teardown.
    recovery_task = getattr(app, "_websocket_recovery_task", None)
    app._websocket_recovery_task = None
    if recovery_task is not None and not recovery_task.done():
        recovery_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await recovery_task
    await app._python_tty.close()
    app._computer_use.reset_enabled_for_shutdown()
    await app._computer_use.disconnect()
    await app._host_browser.disconnect()
    # Prevent the disconnect event from scheduling websocket recovery while
    # the app is shutting down (same guard disconnect_to_login uses).
    app.client.on_disconnect = None
    try:
        await app.client.disconnect()
    except Exception:
        pass
    app.exit()
