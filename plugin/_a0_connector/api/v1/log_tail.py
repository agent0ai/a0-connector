"""POST /api/plugins/_a0_connector/v1/log_tail."""
from __future__ import annotations

from helpers.api import Request, Response
import plugins._a0_connector.api.v1.base as connector_base


class LogTail(connector_base.ProtectedConnectorApiHandler):
    async def process(self, input: dict, request: Request) -> dict | Response:
        from plugins._a0_connector.helpers.event_bridge import (
            get_context_log_entries,
        )

        context_id = str(input.get("context_id", "")).strip()
        if not context_id:
            return Response(
                response='{"error": "context_id is required"}',
                status=400,
                mimetype="application/json",
            )

        after = int(input.get("after", 0) or 0)
        limit = min(int(input.get("limit", 50) or 50), 250)

        events, last_sequence = get_context_log_entries(context_id, after=after)
        limited_events = events[:limit]
        return {
            "context_id": context_id,
            "events": limited_events,
            "last_sequence": last_sequence,
            "has_more": len(events) > len(limited_events),
        }
