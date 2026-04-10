"""POST /api/plugins/_a0_connector/v1/compact_chat."""
from __future__ import annotations

from helpers.api import Request, Response
import plugins._a0_connector.api.v1.base as connector_base


def _coerce_bool(value: object, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if not normalized:
            return default
        return normalized in {"1", "true", "yes", "on"}
    if value is None:
        return default
    return bool(value)


async def _run_compaction_task(context, use_chat_model: bool, preset_name: str | None) -> None:
    from helpers.state_monitor_integration import mark_dirty_all
    from plugins._chat_compaction.helpers.compactor import run_compaction

    try:
        await run_compaction(context, use_chat_model, preset_name)
    except Exception as exc:
        context.log.log(
            type="error",
            heading="Compaction Failed",
            content=str(exc),
        )
        mark_dirty_all(reason="plugins._a0_connector.compact_chat_error")


class CompactChat(connector_base.ProtectedConnectorApiHandler):
    async def process(self, input: dict, request: Request) -> dict | Response:
        from agent import AgentContext
        from plugins._chat_compaction.helpers.compactor import (
            MIN_COMPACTION_TOKENS,
            get_compaction_stats,
        )

        action = str(input.get("action", "compact")).strip() or "compact"
        context_id = str(
            input.get("context", input.get("context_id", input.get("ctxid", "")))
        ).strip()

        if not context_id:
            return Response("Missing context id", 400)

        context = AgentContext.get(context_id)
        if not context:
            return Response("Context not found", 404)

        if context.is_running():
            return Response("Cannot compact while agent is running", 409)

        stats = await get_compaction_stats(context)
        if stats["token_count"] < MIN_COMPACTION_TOKENS:
            return {
                "ok": False,
                "message": f"Not enough content to compact (minimum {MIN_COMPACTION_TOKENS:,} tokens)",
            }

        if action == "stats":
            return {"ok": True, "stats": stats}

        if action == "compact":
            use_chat_model = _coerce_bool(input.get("use_chat_model", True), default=True)
            preset_name = str(input.get("preset_name", "")).strip() or None
            context.run_task(_run_compaction_task, context, use_chat_model, preset_name)
            return {"ok": True, "message": "Compaction started"}

        return Response(f"Unknown action: {action}", 400)
