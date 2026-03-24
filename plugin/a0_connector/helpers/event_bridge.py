"""Context event streaming bridge for the a0-connector plugin.

Translates internal Agent Zero log entries into normalized connector events
that are suitable for external/CLI clients.

This is intentionally transport-neutral: it does not write to websockets
directly; callers decide how to deliver events.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, AsyncIterator, Callable

from helpers.print_style import PrintStyle


# --- Connector event types ---------------------------------------------------

EVENT_USER_MESSAGE = "user_message"
EVENT_ASSISTANT_DELTA = "assistant_delta"
EVENT_ASSISTANT_MESSAGE = "assistant_message"
EVENT_TOOL_START = "tool_start"
EVENT_TOOL_OUTPUT = "tool_output"
EVENT_TOOL_END = "tool_end"
EVENT_CODE_START = "code_start"
EVENT_CODE_OUTPUT = "code_output"
EVENT_WARNING = "warning"
EVENT_ERROR = "error"
EVENT_STATUS = "status"
EVENT_MESSAGE_COMPLETE = "message_complete"
EVENT_CONTEXT_UPDATED = "context_updated"

# Map from A0 log entry types to connector event types
_LOG_TYPE_MAP: dict[str, str] = {
    "user": EVENT_USER_MESSAGE,
    "ai_response": EVENT_ASSISTANT_MESSAGE,
    "tool": EVENT_TOOL_START,
    "tool_output": EVENT_TOOL_OUTPUT,
    "code": EVENT_CODE_START,
    "code_output": EVENT_CODE_OUTPUT,
    "warning": EVENT_WARNING,
    "error": EVENT_ERROR,
    "info": EVENT_STATUS,
}


def log_entry_to_connector_event(
    log_entry: dict[str, Any],
    sequence: int,
    context_id: str,
) -> dict[str, Any] | None:
    """Convert a raw A0 log entry dict to a connector event dict.

    Returns ``None`` if the entry should be skipped.
    """
    entry_type = log_entry.get("type", "")
    event_type = _LOG_TYPE_MAP.get(entry_type, EVENT_STATUS)

    content = log_entry.get("content", "")
    heading = log_entry.get("heading", "")
    kvps = log_entry.get("kvps") or {}

    data: dict[str, Any] = {}
    if content:
        data["text"] = content
    if heading:
        data["heading"] = heading
    if kvps:
        data["meta"] = kvps

    return {
        "context_id": context_id,
        "sequence": sequence,
        "event": event_type,
        "timestamp": log_entry.get("timestamp", ""),
        "data": data,
    }


def get_context_log_entries(
    context_id: str,
    after: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """Return normalized connector events for a context log.

    Args:
        context_id: The Agent Zero context ID.
        after: Return only entries with sequence number > after (0 = all).

    Returns:
        A tuple of (events list, last_sequence).
    """
    try:
        from agent import AgentContext
        context = AgentContext.get(context_id)
        if context is None:
            return [], 0

        raw_entries = context.log.output()
        events: list[dict[str, Any]] = []
        last_seq = 0
        for i, entry in enumerate(raw_entries):
            seq = i + 1
            if seq <= after:
                continue
            # entry might be a LogItem or dict depending on version
            if hasattr(entry, "to_dict"):
                entry_dict = entry.to_dict()
            elif hasattr(entry, "__dict__"):
                entry_dict = vars(entry)
            elif isinstance(entry, dict):
                entry_dict = entry
            else:
                continue

            event = log_entry_to_connector_event(entry_dict, seq, context_id)
            if event is not None:
                events.append(event)
                last_seq = seq

        return events, last_seq
    except Exception as e:
        PrintStyle.error(f"[a0-connector] event_bridge error for context {context_id}: {e}")
        return [], 0


async def stream_context_events(
    context_id: str,
    from_sequence: int = 0,
    poll_interval: float = 0.5,
    timeout: float = 300.0,
    emit_fn: Callable[[dict[str, Any]], Any] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Async generator that streams new connector events as they appear.

    Args:
        context_id: The context to watch.
        from_sequence: Only yield events with sequence > this value.
        poll_interval: How often to poll for new log entries (seconds).
        timeout: Maximum streaming duration (seconds).
        emit_fn: Optional async callable to call for each event (in addition to yielding).
    """
    last_seq = from_sequence
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        events, new_seq = get_context_log_entries(context_id, after=last_seq)
        for event in events:
            if emit_fn is not None:
                try:
                    result = emit_fn(event)
                    if asyncio.iscoroutine(result):
                        await result
                except Exception as e:
                    PrintStyle.error(f"[a0-connector] emit_fn error: {e}")
            yield event
        if new_seq > last_seq:
            last_seq = new_seq

        await asyncio.sleep(poll_interval)
