from __future__ import annotations

from typing import TYPE_CHECKING

from agent_zero_cli.screens.chat_list import ChatListScreen
from agent_zero_cli.widgets import ChatInput
from agent_zero_cli.widgets.chat_log import ChatLog

if TYPE_CHECKING:
    from agent_zero_cli.app import AgentZeroCLI


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


async def cmd_clear(app: AgentZeroCLI) -> None:
    app.query_one("#chat-log", ChatLog).clear()
    app._set_idle()


async def switch_context(app: AgentZeroCLI, context_id: str, *, has_messages_hint: bool) -> None:
    if app._compaction_refresh_context and app._compaction_refresh_context != context_id:
        app._cancel_compaction_refresh()
    app._stop_token_refresh()

    if app.current_context:
        await app.client.unsubscribe_context(app.current_context)

    app.current_context = context_id
    app._set_pause_latched(False)
    app.current_context_has_messages = has_messages_hint
    app._response_delivered = False
    app._context_run_complete = False
    log = app.query_one("#chat-log", ChatLog)
    log.clear()
    app._set_idle()
    app._sync_body_mode()
    await app.client.subscribe_context(context_id, from_seq=0)
    await app._refresh_model_switcher()
    await app._refresh_token_usage(context_id=context_id)
    app._start_token_refresh()


async def cmd_chats(app: AgentZeroCLI) -> None:
    try:
        contexts = await app.client.list_chats()
    except Exception as exc:
        app._show_notice(f"Error listing chats: {exc}", error=True)
        return

    if not contexts:
        app._show_notice("No previous chats found.")
        return

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

    await app._switch_context(result, has_messages_hint=has_messages_hint)


async def cmd_new(app: AgentZeroCLI) -> None:
    try:
        context_id = await app.client.create_chat()
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
    input_widget = app.query_one("#message-input", ChatInput)
    input_widget.disabled = True
    app._set_activity("Resuming")


async def cmd_nudge(app: AgentZeroCLI) -> None:
    availability = app._nudge_availability()
    if not availability.available:
        app._show_notice(availability.reason or "Nudge is unavailable.", error=True)
        return

    input_widget = app.query_one("#message-input", ChatInput)
    app._set_pause_latched(False)
    input_widget.disabled = True
    app.agent_active = True
    app._response_delivered = False
    app._context_run_complete = False
    app._sync_ready_actions()
    try:
        response = await app.client.nudge_agent(app.current_context)
    except Exception as exc:
        app._show_notice(f"Nudge failed: {exc}", error=True)
        input_widget.disabled = False
        app.agent_active = False
        app._sync_ready_actions()
        return

    if not response.get("ok"):
        app._show_notice(str(response.get("message") or "Nudge failed."), error=True)
        input_widget.disabled = False
        app.agent_active = False
        app._sync_ready_actions()
