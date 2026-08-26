from __future__ import annotations

import asyncio
import sys
from time import monotonic
from typing import TYPE_CHECKING, Any

from rich.text import Text
from textual.css.query import NoMatches

from agent_zero_cli.rendering import (
    _EVENT_CATEGORY,
    _STATUS_LABEL,
    completion_notification_sequence,
    extract_detail,
    format_duration,
    render_connector_event,
)
from agent_zero_cli.media_refs import extract_image_references
from agent_zero_cli.widgets import ChatInput
from agent_zero_cli.widgets.chat_log import ChatLog

if TYPE_CHECKING:
    from agent_zero_cli.app import AgentZeroCLI


_REMOTE_TREE_KEEPALIVE_SECONDS = 60.0


def _chat_log_or_none(app: AgentZeroCLI) -> ChatLog | None:
    try:
        return app.query_one("#chat-log", ChatLog)
    except NoMatches:
        return None


def _notify_terminal_completion(app: AgentZeroCLI) -> None:
    if getattr(app, "is_headless", False) or getattr(app, "is_web", False):
        return
    stdout = sys.__stdout__
    sequence = completion_notification_sequence(
        is_tty=bool(stdout and getattr(stdout, "isatty", lambda: False)())
    )
    driver = getattr(app, "_driver", None)
    if not sequence or driver is None:
        return
    try:
        driver.write(sequence)
    except Exception:
        pass


def _remember_user_message(app: AgentZeroCLI, event: dict[str, Any]) -> None:
    if event.get("event") != "user_message":
        return

    event_data = event.get("data")
    if not isinstance(event_data, dict):
        return
    text = str(event_data.get("text") or "")
    if not text.strip():
        return

    try:
        app.query_one("#message-input", ChatInput).seed_history([text])
    except Exception:
        return


def _append_event_images(
    app: AgentZeroCLI,
    log: ChatLog,
    event: dict[str, Any],
    *,
    prepend: bool = False,
) -> None:
    """Attach normalized media after the owning event has established its entry."""
    references = extract_image_references(event, base_url=app.client.base_url)
    if not references:
        return
    log.append_or_update_images(references[0].sequence, references, prepend=prepend)


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
    queue_items = data.get("message_queue", [])
    if isinstance(queue_items, list):
        app._set_message_queue(queue_items)

    prepend = "history_before" in data and bool(log._seq_to_widget)
    if "history_before" not in data:
        app._run_started_at = None
        app._last_response_sequence = None
    if "history_before" in data:
        log.set_history_page(
            before=int(data.get("history_before") or 0),
            has_more=bool(data.get("has_more_history")),
        )

    event_items = reversed(events) if prepend and isinstance(events, list) else events
    for event in event_items:
        event_type = event.get("event", "")
        category = _EVENT_CATEGORY.get(event_type, "info")

        if app._message_flag_for_event(event_type):
            app._mark_context_has_messages()
        _remember_user_message(app, event)

        if category in ("user", "response", "warning", "error", "code", "info"):
            app._show_chat_intro(log, category)
            if prepend:
                render_connector_event(log, event, prepend=True)
            else:
                render_connector_event(log, event)
        elif category == "util":
            if app.show_utility_messages:
                if prepend:
                    render_connector_event(log, event, prepend=True)
                else:
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
                    **({"prepend": True} if prepend else {}),
                )
        _append_event_images(app, log, event, prepend=prepend)

    app._sync_body_mode()


def handle_message_queue_updated(app: AgentZeroCLI, data: dict[str, Any]) -> None:
    context_id = data.get("context_id", "")
    if context_id != app.current_context:
        return

    queue_items = data.get("message_queue", data.get("items", []))
    app._set_message_queue(queue_items if isinstance(queue_items, list) else [])


def handle_context_event(app: AgentZeroCLI, data: dict[str, Any]) -> None:
    context_id = data.get("context_id", "")
    if context_id != app.current_context:
        return

    event_type = data.get("event", "")

    if app._message_flag_for_event(event_type):
        app._mark_context_has_messages()
    _remember_user_message(app, data)

    category = _EVENT_CATEGORY.get(event_type, "info")
    log = _chat_log_or_none(app)
    if log is None:
        return

    sequence = data.get("sequence", -1)

    post_complete = app._context_run_complete

    if event_type == "user_message":
        app._run_started_at = monotonic()
        app._last_response_sequence = None
    elif not post_complete and app._run_started_at is None and category != "response":
        app._run_started_at = monotonic()

    if not app._pause_latched and not post_complete:
        app.agent_active = True
        app._sync_ready_actions()

    if category == "response":
        app._response_delivered = True
        app._focus_message_input()
        app._set_idle()
        app._show_chat_intro(log, category)
        if render_connector_event(log, data):
            try:
                app._last_response_sequence = int(sequence)
            except (TypeError, ValueError):
                app._last_response_sequence = None
        _append_event_images(app, log, data)
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

    _append_event_images(app, log, data)


def handle_context_complete(app: AgentZeroCLI, data: dict[str, Any]) -> None:
    context_id = data.get("context_id", "")
    if context_id != app.current_context:
        return

    was_active = app.agent_active
    started_at = app._run_started_at
    response_sequence = app._last_response_sequence
    app._run_started_at = None
    app._last_response_sequence = None
    if started_at is not None:
        log = _chat_log_or_none(app)
        if log is not None:
            completion = Text(
                f"Completed in {format_duration(monotonic() - started_at)}",
                style="#7f8c98",
            )
            if response_sequence is None:
                log.write(completion)
            else:
                log.write_before(response_sequence, completion)

    response = data.get("response")
    if not app._response_delivered and isinstance(response, str) and response.strip():
        app._response_delivered = True
        app._show_notice(response)

    app._set_pause_latched(False)
    app.agent_active = False
    app._context_run_complete = True
    app._sync_ready_actions()
    app._focus_message_input()
    app._set_idle()
    asyncio.create_task(app._refresh_token_usage(context_id=context_id))
    asyncio.create_task(app._refresh_goal_bar())
    asyncio.create_task(app._refresh_context_tab_metadata(context_id, has_messages_hint=True))
    if app._compaction_refresh_context == context_id:
        app._compaction_refresh_context = None
        asyncio.create_task(_compaction_context_reload(app, context_id))
    if was_active:
        _notify_terminal_completion(app)


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


async def handle_browser_op(app: AgentZeroCLI, data: dict[str, Any]) -> dict[str, Any]:
    return await app._host_browser.handle_op(data)


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
        await app._publish_remote_tree_snapshot(force=True)
        while app.connected:
            await asyncio.sleep(30.0)
            await app._publish_remote_tree_snapshot()
    except asyncio.CancelledError:
        return


async def publish_remote_tree_snapshot(app: AgentZeroCLI, *, force: bool = False) -> None:
    if not app.connected:
        return

    snapshot = app._remote_files.build_tree_snapshot()
    now = monotonic()
    if (
        not force
        and snapshot.tree_hash == app._last_remote_tree_hash
        and now - app._last_remote_tree_published_at < _REMOTE_TREE_KEEPALIVE_SECONDS
    ):
        return

    try:
        await app.client.send_remote_tree_update(snapshot.as_payload())
    except Exception:
        return

    app._last_remote_tree_hash = snapshot.tree_hash
    app._last_remote_tree_published_at = now
