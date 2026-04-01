from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from typing import Any


@dataclass
class PendingFileOperation:
    sid: str
    loop: asyncio.AbstractEventLoop
    future: asyncio.Future[dict[str, Any]]
    context_id: str | None = None


_context_subscriptions: dict[str, set[str]] = {}
_sid_contexts: dict[str, set[str]] = {}
_pending_file_ops: dict[str, PendingFileOperation] = {}
_state_lock = threading.RLock()


def register_sid(sid: str) -> None:
    with _state_lock:
        _sid_contexts.setdefault(sid, set())


def unregister_sid(sid: str) -> set[str]:
    with _state_lock:
        contexts = _sid_contexts.pop(sid, set())
        for context_id in contexts:
            subscribers = _context_subscriptions.get(context_id)
            if not subscribers:
                continue
            subscribers.discard(sid)
            if not subscribers:
                _context_subscriptions.pop(context_id, None)
    return contexts


def subscribe_sid_to_context(sid: str, context_id: str) -> None:
    with _state_lock:
        _sid_contexts.setdefault(sid, set()).add(context_id)
        _context_subscriptions.setdefault(context_id, set()).add(sid)


def unsubscribe_sid_from_context(sid: str, context_id: str) -> None:
    with _state_lock:
        contexts = _sid_contexts.get(sid)
        if contexts is not None:
            contexts.discard(context_id)
            if not contexts:
                _sid_contexts.pop(sid, None)

        subscribers = _context_subscriptions.get(context_id)
        if subscribers is not None:
            subscribers.discard(sid)
            if not subscribers:
                _context_subscriptions.pop(context_id, None)


def subscribed_contexts_for_sid(sid: str) -> set[str]:
    with _state_lock:
        return set(_sid_contexts.get(sid, set()))


def subscribed_sids_for_context(context_id: str) -> set[str]:
    with _state_lock:
        return set(_context_subscriptions.get(context_id, set()))


def select_target_sid(context_id: str) -> str | None:
    with _state_lock:
        subscribers = _context_subscriptions.get(context_id, set())
        if not subscribers:
            return None
        return sorted(subscribers)[0]


def store_pending_file_op(
    op_id: str,
    *,
    sid: str,
    future: asyncio.Future[dict[str, Any]],
    loop: asyncio.AbstractEventLoop,
    context_id: str | None = None,
) -> None:
    with _state_lock:
        _pending_file_ops[op_id] = PendingFileOperation(
            sid=sid,
            loop=loop,
            future=future,
            context_id=context_id,
        )


def clear_pending_file_op(op_id: str) -> None:
    with _state_lock:
        _pending_file_ops.pop(op_id, None)


def resolve_pending_file_op(
    op_id: str,
    *,
    sid: str,
    payload: dict[str, Any],
) -> bool:
    with _state_lock:
        pending = _pending_file_ops.get(op_id)
        if pending is None or pending.sid != sid:
            return False
        _pending_file_ops.pop(op_id, None)

    pending.loop.call_soon_threadsafe(_set_future_result, pending.future, dict(payload))
    return True


def fail_pending_file_op(
    op_id: str,
    *,
    sid: str | None = None,
    error: str,
) -> bool:
    with _state_lock:
        pending = _pending_file_ops.get(op_id)
        if pending is None:
            return False
        if sid is not None and pending.sid != sid:
            return False
        _pending_file_ops.pop(op_id, None)

    payload = {"op_id": op_id, "ok": False, "error": error}
    pending.loop.call_soon_threadsafe(_set_future_result, pending.future, payload)
    return True


def fail_pending_file_ops_for_sid(sid: str, *, error: str) -> None:
    with _state_lock:
        matches = [
            (op_id, pending)
            for op_id, pending in _pending_file_ops.items()
            if pending.sid == sid
        ]
        for op_id, _pending in matches:
            _pending_file_ops.pop(op_id, None)

    for op_id, pending in matches:
        payload = {"op_id": op_id, "ok": False, "error": error}
        pending.loop.call_soon_threadsafe(_set_future_result, pending.future, payload)


def _set_future_result(
    future: asyncio.Future[dict[str, Any]],
    payload: dict[str, Any],
) -> None:
    if not future.done():
        future.set_result(payload)
