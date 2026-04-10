from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Mapping

from agent_zero_cli.model_config import coerce_positive_int, extract_token_limit

if TYPE_CHECKING:
    from agent_zero_cli.app import AgentZeroCLI

_TOKEN_REFRESH_INTERVAL_SECONDS = 2.0


def stop_token_refresh(app: AgentZeroCLI) -> None:
    task = app._token_refresh_task
    app._token_refresh_task = None
    if task is not None and not task.done():
        task.cancel()


def start_token_refresh(app: AgentZeroCLI) -> None:
    stop_token_refresh(app)
    if not app.connected or not app.current_context:
        return
    if "token_status" not in app.connector_features and "compact_chat" not in app.connector_features:
        app._clear_token_usage()
        return
    context_id = app.current_context
    app._token_refresh_task = asyncio.create_task(refresh_token_usage_loop(app, context_id))


async def refresh_token_usage_loop(app: AgentZeroCLI, context_id: str) -> None:
    try:
        while app.connected and app.current_context == context_id:
            await refresh_token_usage(app, context_id=context_id, silent=True)
            await asyncio.sleep(_TOKEN_REFRESH_INTERVAL_SECONDS)
    except asyncio.CancelledError:
        return


async def refresh_token_usage(app: AgentZeroCLI, *, context_id: str | None = None, silent: bool = True) -> None:
    if "token_status" not in app.connector_features and "compact_chat" not in app.connector_features:
        app._clear_token_usage()
        return
    target_context = context_id or app.current_context
    if not target_context:
        app._clear_token_usage()
        return

    if "token_status" in app.connector_features:
        try:
            payload = await app.client.get_token_status(target_context)
        except Exception:
            if not silent:
                app._show_notice("Failed to refresh token usage.", error=True)
            return

        if not isinstance(payload, dict):
            return
        if not payload.get("ok"):
            if not silent:
                app._show_notice(str(payload.get("message") or "Token usage unavailable."), error=True)
            return

        token_count = coerce_positive_int(payload.get("token_count"))
        if token_count is None:
            app._clear_token_usage()
            return

        token_limit = coerce_positive_int(payload.get("context_window"))
        if token_limit is not None and token_limit <= 0:
            token_limit = None
        app._set_token_usage(token_count, token_limit)
        return

    # Backward-compatible fallback for connector builds without token_status.
    try:
        payload = await app.client.get_compaction_stats(target_context)
    except Exception:
        if not silent:
            app._show_notice("Failed to refresh token usage.", error=True)
        return

    if not isinstance(payload, dict):
        return
    if not payload.get("ok"):
        status_code = int(payload.get("status_code") or 0)
        if status_code == 409:
            return
        if not silent:
            app._show_notice(str(payload.get("message") or "Token usage unavailable."), error=True)
        return

    stats = payload.get("stats")
    if not isinstance(stats, Mapping):
        return

    token_count = coerce_positive_int(stats.get("token_count"))
    if token_count is None:
        return

    token_limit = extract_token_limit(stats)
    app._set_token_usage(token_count, token_limit)
