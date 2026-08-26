from __future__ import annotations

import asyncio
from datetime import datetime
import shlex
from typing import TYPE_CHECKING, Any, Mapping

from agent_zero_cli.attachments import AttachmentError, create_image_file_upload
from agent_zero_cli.project_utils import project_name, project_title

from agent_zero_cli.screens.chat_list import ChatListScreen
from agent_zero_cli.widgets import ChatInput
from agent_zero_cli.widgets.chat_log import ChatLog

if TYPE_CHECKING:
    from agent_zero_cli.app import AgentZeroCLI


def _parse_timestamp(value: object) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)

    raw = str(value or "").strip()
    if not raw:
        return None

    try:
        return float(raw)
    except ValueError:
        pass

    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None

    if parsed.tzinfo is None:
        return parsed.timestamp()
    return parsed.timestamp()


def _context_name(context: Mapping[str, object]) -> str:
    name = str(context.get("name", "") or "").strip()
    if name:
        return name
    try:
        chat_no = int(context.get("no", 0) or 0)
    except (TypeError, ValueError):
        chat_no = 0
    if chat_no > 0:
        return f"Chat #{chat_no}"
    return str(context.get("id", "") or "")


def _context_updated_at(context: Mapping[str, object]) -> float:
    return (
        _parse_timestamp(context.get("updated_at"))
        or _parse_timestamp(context.get("updated"))
        or _parse_timestamp(context.get("created_at"))
        or 0.0
    )


def _context_created_at(context: Mapping[str, object]) -> float:
    return _parse_timestamp(context.get("created_at")) or 0.0


def _normalize_project_name(value: object) -> str:
    text = str(value or "").strip()
    return text


def _context_project_names(context: Mapping[str, object]) -> set[str]:
    raw_names: set[str] = set()

    context_project = context.get("project_name")
    if isinstance(context_project, str):
        raw = _normalize_project_name(context_project)
        if raw:
            raw_names.add(raw)
    elif isinstance(context_project, Mapping):
        normalized = project_name(context_project) or project_title(context_project)
        raw = _normalize_project_name(normalized)
        if raw:
            raw_names.add(raw)

    context_project = context.get("project")
    if isinstance(context_project, str):
        raw = _normalize_project_name(context_project)
        if raw:
            raw_names.add(raw)
    elif isinstance(context_project, Mapping):
        normalized = project_name(context_project) or project_title(context_project)
        raw = _normalize_project_name(normalized)
        if raw:
            raw_names.add(raw)

    if isinstance(context.get("project_title"), str):
        raw = _normalize_project_name(context.get("project_title"))
        if raw:
            raw_names.add(raw)

    if isinstance(context.get("project_id"), str):
        raw = _normalize_project_name(context.get("project_id"))
        if raw:
            raw_names.add(raw)

    return {name.casefold() for name in raw_names if name}


def _project_filter_candidates(project: Mapping[str, str] | None) -> set[str]:
    if not project:
        return set()
    candidates: set[str] = set()

    name = _normalize_project_name(project.get("name", ""))
    if name:
        candidates.add(name.casefold())

    title = _normalize_project_name(project.get("title", ""))
    if title:
        candidates.add(title.casefold())

    project_id = _normalize_project_name(project.get("id", ""))
    if project_id:
        candidates.add(project_id.casefold())

    return candidates


def _apply_active_project_filter(
    contexts: list[dict[str, Any]],
    *,
    project: Mapping[str, str] | None,
) -> tuple[list[dict[str, Any]], bool]:
    active_project_names = _project_filter_candidates(project)
    if not active_project_names:
        return contexts, False

    filtered: list[dict[str, Any]] = []
    has_project_data = False
    for context in contexts:
        project_names = _context_project_names(context)
        if project_names:
            has_project_data = True
        if project_names and project_names.intersection(active_project_names):
            filtered.append(context)

    if not has_project_data:
        return contexts, False

    return filtered, True


def _sort_contexts(
    contexts: list[dict[str, Any]],
    *,
    sort_by: str,
) -> list[dict[str, Any]]:
    if sort_by == "name":
        return sorted(
            contexts,
            key=lambda context: (_context_name(context).casefold(), _context_created_at(context)),
        )

    if sort_by == "created":
        return sorted(
            contexts,
            key=lambda context: (_context_created_at(context), _context_name(context).casefold()),
            reverse=True,
        )

    return sorted(
        contexts,
        key=lambda context: (_context_updated_at(context), _context_name(context).casefold()),
        reverse=True,
    )


async def cmd_help(app: AgentZeroCLI) -> None:
    app._surface_help()


async def cmd_keys(app: AgentZeroCLI) -> None:
    help_panel_visible = False
    try:
        help_panel_visible = bool(app.screen.query("HelpPanel"))
    except Exception:
        help_panel_visible = False

    if help_panel_visible:
        app.action_hide_help_panel()
    else:
        app.action_show_help_panel()


async def cmd_quit(app: AgentZeroCLI) -> None:
    await app.action_quit()


async def cmd_disconnect(app: AgentZeroCLI) -> None:
    await app.action_disconnect()


async def cmd_clear(app: AgentZeroCLI) -> None:
    app._invalidate_image_loads()
    app.image_store.cancel_pending()
    log = app.query_one("#chat-log", ChatLog)
    log.clear(preserve_intro=True)
    log.ensure_intro_banner()
    app._set_idle()


async def switch_context(app: AgentZeroCLI, context_id: str, *, has_messages_hint: bool) -> None:
    app._invalidate_image_loads()
    if app._compaction_refresh_context and app._compaction_refresh_context != context_id:
        app._cancel_compaction_refresh()
    app._stop_token_refresh()
    await app._hide_project_menu()
    await app._hide_profile_menu()

    if app.current_context:
        await app.client.unsubscribe_context(app.current_context)

    app.current_context = context_id
    app.query_one("#message-input", ChatInput).set_history_context(context_id)
    app._set_pause_latched(False)
    app.agent_active = False
    app.current_context_has_messages = has_messages_hint
    app._remember_context_tab(context_id, has_messages_hint=has_messages_hint)
    app._set_message_queue([])
    app._response_delivered = False
    app._context_run_complete = False
    log = app.query_one("#chat-log", ChatLog)
    app.image_store.cancel_pending()
    log.clear()
    app._set_idle()
    app._clear_project_state()
    app._sync_body_mode()
    await app.client.subscribe_context(context_id, from_seq=0, history="tail")
    await app._refresh_context_tab_metadata(context_id, has_messages_hint=has_messages_hint)
    await app._refresh_remote_tool_metadata()
    await app._publish_remote_tree_snapshot(force=True)
    app._remember_context(context_id)
    await app._refresh_projects(context_id=context_id)
    await app._refresh_goal_bar()
    await app._refresh_model_switcher()
    await app._refresh_token_usage(context_id=context_id)
    app._start_token_refresh()


async def cmd_chats(
    app: AgentZeroCLI,
    *,
    sort_by: str = "updated",
    active_project_only: bool = False,
) -> None:
    sort_by = "name" if sort_by == "name" else "created" if sort_by == "created" else "updated"

    try:
        contexts = await app.client.list_chats()
    except Exception as exc:
        app._show_notice(f"Error listing chats: {exc}", error=True)
        return

    if active_project_only:
        contexts, has_project_data = _apply_active_project_filter(
            contexts,
            project=app.current_project,
        )
        if has_project_data and not contexts:
            app._show_notice("No chats for the active project.")
            return
        if not has_project_data and app.current_project is not None:
            app._show_notice("Project metadata is not available for chat list entries; showing all chats.")

    if not contexts:
        app._show_notice("No previous chats found.")
        return

    contexts = _sort_contexts(contexts, sort_by=sort_by)

    result = await app.push_screen_wait(ChatListScreen(contexts))
    if not result:
        return

    selected = next((context for context in contexts if str(context.get("id")) == result), {})
    has_messages_hint = bool(selected.get("last_message"))
    if not has_messages_hint and "chat_get" in app.connector_features:
        try:
            metadata = await app.client.get_chat(result)
        except Exception:
            metadata = {}
        has_messages_hint = bool(metadata.get("last_message") or metadata.get("log_entries"))

    app._remember_context_tab(result, selected, has_messages_hint=has_messages_hint)
    await app._switch_context(result, has_messages_hint=has_messages_hint)


async def cmd_new(app: AgentZeroCLI) -> None:
    try:
        context_id = await app.client.create_chat(current_context_id=app.current_context)
    except Exception as exc:
        app._show_notice(f"Failed to create a new chat: {exc}", error=True)
        return

    await app._switch_context(context_id, has_messages_hint=False)
    app._set_splash_stage(
        "ready",
        message="Ready when you are.",
        detail=app.config.instance_url or app._splash_host(),
        host=app._splash_host(),
        actions=app._welcome_actions(),
    )
    app._focus_message_input()


async def cmd_pause(app: AgentZeroCLI) -> None:
    availability = app._pause_availability()
    if not availability.available:
        app._show_notice(availability.reason or "Pause is unavailable.", error=True)
        return

    try:
        response = await app.client.pause_agent(app.current_context)
    except Exception as exc:
        app._show_notice(f"Pause failed: {exc}", error=True)
        return

    if not response.get("ok"):
        app._show_notice(str(response.get("message") or "Pause failed."), error=True)
        return

    app._set_pause_latched(True)
    app.agent_active = False
    input_widget = app.query_one("#message-input", ChatInput)
    input_widget.disabled = False
    app._focus_message_input()
    app._set_idle()


async def cmd_resume(app: AgentZeroCLI) -> None:
    availability = app._resume_availability()
    if not availability.available:
        app._show_notice(availability.reason or "Resume is unavailable.", error=True)
        return

    try:
        response = await app.client.pause_agent(app.current_context, paused=False)
    except Exception as exc:
        app._show_notice(f"Resume failed: {exc}", error=True)
        return

    if not response.get("ok"):
        app._show_notice(str(response.get("message") or "Resume failed."), error=True)
        return

    app._set_pause_latched(False)
    app.agent_active = True
    app._set_activity("Resuming")


async def cmd_nudge(app: AgentZeroCLI) -> None:
    availability = app._nudge_availability()
    if not availability.available:
        app._show_notice(availability.reason or "Nudge is unavailable.", error=True)
        return

    app._set_pause_latched(False)
    app.agent_active = True
    app._response_delivered = False
    app._context_run_complete = False
    app._sync_ready_actions()
    try:
        response = await app.client.nudge_agent(app.current_context)
    except Exception as exc:
        app._show_notice(f"Nudge failed: {exc}", error=True)
        app.agent_active = False
        app._sync_ready_actions()
        return

    if not response.get("ok"):
        app._show_notice(str(response.get("message") or "Nudge failed."), error=True)
        app.agent_active = False
        app._sync_ready_actions()


async def cmd_queue(app: AgentZeroCLI, *, query: str = "") -> None:
    query = query.strip()
    if not query:
        _show_queue_summary(app)
        return

    try:
        tokens = shlex.split(query)
    except ValueError as exc:
        app._show_notice(f"Could not parse queue command: {exc}", error=True)
        return

    if not tokens:
        _show_queue_summary(app)
        return

    action = tokens[0].lower()
    if action in {"send", "all", "flush"}:
        await cmd_queue_send(app)
        return

    if action in {"clear", "delete"} and len(tokens) == 1:
        await cmd_queue_clear(app)
        return

    if action in {"remove", "rm", "delete"}:
        if len(tokens) < 2:
            app._show_notice("Usage: /queue remove <number|id>", error=True)
            return
        await cmd_queue_remove(app, tokens[1])
        return

    app._show_notice("Usage: /queue [send|clear|remove <number|id>]", error=True)


async def cmd_queue_send(app: AgentZeroCLI) -> None:
    availability = app._require_features("message_queue")
    if not availability.available:
        app._show_notice(availability.reason or "Message queue is unavailable.", error=True)
        return
    if not app.current_context:
        app._show_notice("Open or create a chat context first.", error=True)
        return
    if not app._has_message_queue():
        app._show_notice("No queued messages to send.")
        return

    previous_agent_active = app.agent_active
    previous_pause_latched = app._pause_latched
    app._set_pause_latched(False)
    app.agent_active = True
    app._response_delivered = False
    app._context_run_complete = False
    app._sync_ready_actions()

    try:
        response = await app.client.send_message_queue(app.current_context, send_all=True)
    except Exception as exc:
        app.agent_active = previous_agent_active
        app._set_pause_latched(previous_pause_latched)
        app._sync_ready_actions()
        app._show_notice(f"Error sending queued messages: {exc}", error=True)
        return

    queue_items = response.get("message_queue") if isinstance(response, Mapping) else None
    if isinstance(queue_items, list):
        app._set_message_queue(queue_items)

    sent_count = int(response.get("sent_count", 0) or 0) if isinstance(response, Mapping) else 0
    if sent_count <= 0:
        app.agent_active = previous_agent_active
        app._sync_ready_actions()
        app._show_notice("Queue is empty.")


async def cmd_queue_clear(app: AgentZeroCLI) -> None:
    availability = app._require_features("message_queue")
    if not availability.available:
        app._show_notice(availability.reason or "Message queue is unavailable.", error=True)
        return
    if not app.current_context:
        app._show_notice("Open or create a chat context first.", error=True)
        return

    try:
        response = await app.client.remove_message_from_queue(app.current_context)
    except Exception as exc:
        app._show_notice(f"Error clearing queue: {exc}", error=True)
        return

    queue_items = response.get("message_queue") if isinstance(response, Mapping) else None
    app._set_message_queue(queue_items if isinstance(queue_items, list) else [])
    app._show_notice("Queue cleared.")


async def cmd_queue_remove(app: AgentZeroCLI, selector: str) -> None:
    availability = app._require_features("message_queue")
    if not availability.available:
        app._show_notice(availability.reason or "Message queue is unavailable.", error=True)
        return
    if not app.current_context:
        app._show_notice("Open or create a chat context first.", error=True)
        return

    item_id = _queue_selector_to_item_id(app, selector)
    if not item_id:
        app._show_notice(f"No queued message matches '{selector}'.", error=True)
        return

    try:
        response = await app.client.remove_message_from_queue(app.current_context, item_id=item_id)
    except Exception as exc:
        app._show_notice(f"Error removing queued message: {exc}", error=True)
        return

    queue_items = response.get("message_queue") if isinstance(response, Mapping) else None
    if isinstance(queue_items, list):
        app._set_message_queue(queue_items)


def _show_queue_summary(app: AgentZeroCLI) -> None:
    if not app._has_message_queue():
        app._show_notice("No queued messages.")
        return

    lines = [f"Queued messages ({len(app.message_queue)}):"]
    for index, item in enumerate(app.message_queue, start=1):
        text = str(item.get("text", "") or "").strip() or "(attachment only)"
        if len(text) > 100:
            text = text[:97].rstrip() + "..."
        try:
            attachment_count = int(item.get("attachment_count", 0) or 0)
        except (TypeError, ValueError):
            attachment_count = 0
        suffix = f" [{attachment_count} files]" if attachment_count else ""
        lines.append(f"{index}. {text}{suffix}")
    app._show_notice("\n".join(lines))


def _queue_selector_to_item_id(app: AgentZeroCLI, selector: str) -> str:
    value = selector.strip()
    if not value:
        return ""
    if value.isdigit():
        index = int(value) - 1
        if 0 <= index < len(app.message_queue):
            return str(app.message_queue[index].get("id", "") or "")
        return ""
    return value


async def cmd_attach(app: AgentZeroCLI, *, query: str = "") -> None:
    try:
        paths = shlex.split(query)
    except ValueError as exc:
        app._show_notice(f"Could not parse image path: {exc}", error=True)
        return

    if not paths:
        app._show_notice("Usage: /attach <image-path> [more-image-paths...]", error=True)
        return

    try:
        uploads = await asyncio.to_thread(
            lambda: [create_image_file_upload(path) for path in paths]
        )
    except AttachmentError as exc:
        app._show_notice(str(exc), error=True)
        return
    except OSError as exc:
        app._show_notice(f"Could not read image attachment: {exc}", error=True)
        return

    try:
        attachments = await app.client.upload_attachments(uploads)
    except Exception as exc:
        app._show_notice(f"Image upload failed: {exc}", error=True)
        return

    input_widget = app.query_one("#message-input", ChatInput)
    for attachment in attachments:
        input_widget.add_attachment(attachment)

    if len(attachments) == 1:
        app._show_notice(f"Attached {attachments[0].name}.")
        return

    app._show_notice(f"Attached {len(attachments)} images.")
