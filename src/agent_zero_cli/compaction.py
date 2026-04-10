from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from agent_zero_cli.screens.compact_modal import CompactResult, CompactScreen
if TYPE_CHECKING:
    from agent_zero_cli.app import AgentZeroCLI


def cancel_compaction_refresh(app: AgentZeroCLI) -> None:
    app._compaction_refresh_context = None


def finalize_compaction_refresh(app: AgentZeroCLI, context_id: str) -> None:
    if app._compaction_refresh_context == context_id:
        app._compaction_refresh_context = None

    if app.current_context != context_id:
        return

    app.agent_active = False
    app._sync_ready_actions()
    app._focus_message_input()
    app._set_idle()


def begin_compaction_refresh(app: AgentZeroCLI, context_id: str) -> None:
    app._cancel_compaction_refresh()
    app._compaction_refresh_context = context_id
    app._set_pause_latched(False)
    app.agent_active = True
    app._sync_ready_actions()
    app._set_activity("Compacting chat history", "Updating context")


async def wait_for_compaction_and_reload(app: AgentZeroCLI, context_id: str) -> None:
    try:
        if not app.connected or app.current_context != context_id:
            return
        await app._switch_context(context_id, has_messages_hint=True)
    finally:
        app._finalize_compaction_refresh(context_id)


async def cmd_compact(app: AgentZeroCLI) -> None:
    availability = app._compact_availability()
    if not availability.available:
        app._show_notice(availability.reason or "Compaction is unavailable.", error=True)
        return

    context_id = app.current_context or ""
    screen = CompactScreen(
        stats=None,
        available=False,
        reason="Loading compaction data...",
    )
    result_task = asyncio.create_task(app.push_screen_wait(screen))

    stats_payload: dict[str, Any]
    try:
        stats_result = await app.client.get_compaction_stats(context_id)
    except Exception as exc:
        stats_payload = {
            "ok": False,
            "message": f"Failed to load compaction stats: {exc}",
        }
    else:
        if isinstance(stats_result, dict):
            stats_payload = stats_result
        else:
            stats_payload = {
                "ok": False,
                "message": "Compaction stats returned an unexpected response.",
            }

    available = bool(stats_payload.get("ok"))
    reason = "" if available else str(stats_payload.get("message") or "Compaction is unavailable.")

    if not result_task.done():
        try:
            screen.set_compaction_data(
                stats=stats_payload.get("stats"),
                available=available,
                reason=reason,
            )
        except Exception:
            pass

    result = await result_task
    if result is None:
        return

    if not isinstance(result, CompactResult):
        app._show_notice(f"Unexpected compaction result: {result!r}", error=True)
        return

    try:
        response = await app.client.compact_chat(
            context_id,
            use_chat_model=result.use_chat_model,
            preset_name=result.preset_name,
        )
    except Exception as exc:
        app._show_notice(f"Failed to start compaction: {exc}", error=True)
        return
    if not response.get("ok"):
        app._show_notice(str(response.get("message") or "Compaction failed."), error=True)
        return

    app._begin_compaction_refresh(context_id)
