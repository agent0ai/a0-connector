from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from textual.css.query import NoMatches

from agent_zero_cli.rendering import (
    _EVENT_CATEGORY,
    _STATUS_LABEL,
    extract_detail,
    render_connector_event,
)
from agent_zero_cli.widgets.chat_log import ChatLog

if TYPE_CHECKING:
    from agent_zero_cli.app import AgentZeroCLI


def _chat_log_or_none(app: AgentZeroCLI) -> ChatLog | None:
    try:
        return app.query_one("#chat-log", ChatLog)
    except NoMatches:
        return None


async def _compaction_context_reload(app: AgentZeroCLI, context_id: str) -> None:
    try:
        if not app.connected or app.current_context != context_id:
            return
        await app._switch_context(context_id, has_messages_hint=True)
    except Exception as exc:
        app._show_notice(f"Failed to refresh compacted chat: {exc}", error=True)
    finally:
        app._finalize_compaction_refresh(context_id)


def handle_context_snapshot(app: AgentZeroCLI, data: dict[str, Any]) -> None:
    context_id = data.get("context_id", "")
    if context_id != app.current_context:
        return

    log = _chat_log_or_none(app)
    if log is None:
        return

    events = data.get("events", [])

    for event in events:
        event_type = event.get("event", "")
        category = _EVENT_CATEGORY.get(event_type, "info")

        if app._message_flag_for_event(event_type):
            app._mark_context_has_messages()

        if category in ("user", "response", "warning", "error", "code", "info"):
            app._show_chat_intro(log, category)
            render_connector_event(log, event)
        elif category == "util":
            if app.show_utility_messages:
                render_connector_event(log, event)
        else:
            label = _STATUS_LABEL.get(event_type)
            if label:
                event_data = event.get("data", {})
                detail = extract_detail(event_type, event_data)
                seq = event.get("sequence", -1)
                log.append_or_update_status(
                    seq,
                    label,
                    detail,
                    event_data.get("meta"),
                    active=False,
                )

    app._sync_body_mode()


def handle_context_event(app: AgentZeroCLI, data: dict[str, Any]) -> None:
    context_id = data.get("context_id", "")
    if context_id != app.current_context:
        return

    event_type = data.get("event", "")

    if app._message_flag_for_event(event_type):
        app._mark_context_has_messages()

    category = _EVENT_CATEGORY.get(event_type, "info")
    log = _chat_log_or_none(app)
    if log is None:
        return

    sequence = data.get("sequence", -1)

    post_complete = app._context_run_complete

    if not app._pause_latched and not post_complete:
        app.agent_active = True
        app._sync_ready_actions()

    if category == "response":
        app._response_delivered = True
        app._focus_message_input()
        app._set_idle()
        app._show_chat_intro(log, category)
        render_connector_event(log, data)
        if app._compaction_refresh_context == context_id and event_type == "assistant_message":
            app._compaction_refresh_context = None
            asyncio.create_task(_compaction_context_reload(app, context_id))
        return

    label = _STATUS_LABEL.get(event_type)
    if label:
        event_data = data.get("data", {})
        detail = extract_detail(event_type, event_data)
        if category == "code":
            if not post_complete:
                app._set_activity(label, detail)
        elif post_complete:
            log.append_or_update_status(
                sequence,
                label,
                detail,
                event_data.get("meta"),
                active=False,
            )
        else:
            app._set_activity(label, detail)
            log.set_active_status(data.get("sequence", -1), label, detail, event_data.get("meta"))

    if category in ("warning", "error", "user", "code", "info"):
        app._show_chat_intro(log, category)
        if render_connector_event(log, data):
            if log._active_seq == data.get("sequence"):
                log.stop_active_status()
    elif category == "util" and app.show_utility_messages:
        if render_connector_event(log, data) and log._active_seq == data.get("sequence"):
            log.stop_active_status()


def handle_context_complete(app: AgentZeroCLI, data: dict[str, Any]) -> None:
    context_id = data.get("context_id", "")
    if context_id != app.current_context:
        return

    app._set_pause_latched(False)
    app.agent_active = False
    app._context_run_complete = True
    app._sync_ready_actions()
    app._focus_message_input()
    app._set_idle()
    asyncio.create_task(app._refresh_token_usage(context_id=context_id))
    if app._compaction_refresh_context == context_id:
        app._compaction_refresh_context = None
        asyncio.create_task(_compaction_context_reload(app, context_id))


def handle_connector_error(app: AgentZeroCLI, data: dict[str, Any]) -> None:
    code = data.get("code", "ERROR")
    message = data.get("message", "Unknown error")
    app._show_notice(f"{code}: {message}", error=True)


def handle_file_op(app: AgentZeroCLI, data: dict[str, Any]) -> dict[str, Any]:
    return app._remote_files.handle_file_op(data)


async def handle_exec_op(app: AgentZeroCLI, data: dict[str, Any]) -> dict[str, Any]:
    return await app._python_tty.handle_exec_op(data)


async def handle_computer_use_op(app: AgentZeroCLI, data: dict[str, Any]) -> dict[str, Any]:
    return await app._computer_use.handle_op(data)


def start_remote_tree_publisher(app: AgentZeroCLI) -> None:
    app._stop_remote_tree_publisher()
    app._remote_tree_task = asyncio.create_task(app._remote_tree_publish_loop())


def stop_remote_tree_publisher(app: AgentZeroCLI) -> None:
    task = app._remote_tree_task
    app._remote_tree_task = None
    if task is not None and not task.done():
        task.cancel()


async def remote_tree_publish_loop(app: AgentZeroCLI) -> None:
    try:
        await app._publish_remote_tree_snapshot()
        while app.connected:
            await asyncio.sleep(30.0)
            await app._publish_remote_tree_snapshot()
    except asyncio.CancelledError:
        return


async def publish_remote_tree_snapshot(app: AgentZeroCLI) -> None:
    if not app.connected:
        return

    snapshot = app._remote_files.build_tree_snapshot()
    if snapshot.tree_hash == app._last_remote_tree_hash:
        return

    try:
        await app.client.send_remote_tree_update(snapshot.as_payload())
    except Exception:
        return

    app._last_remote_tree_hash = snapshot.tree_hash
