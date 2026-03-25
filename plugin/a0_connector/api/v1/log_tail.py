"""GET /api/plugins/a0_connector/v1/log_tail?context_id=...&after=0&limit=50

Fallback polling endpoint: returns normalized connector events for a context.
"""
from __future__ import annotations

from helpers.api import ApiHandler, Request, Response


class LogTail(ApiHandler):
    @classmethod
    def requires_auth(cls) -> bool:
        return True

    @classmethod
    def requires_csrf(cls) -> bool:
        return False

    @classmethod
    def requires_api_key(cls) -> bool:
        return False

    async def process(self, input: dict, request: Request) -> dict | Response:
        from usr.plugins.a0_connector.helpers.event_bridge import get_context_log_entries

        context_id: str = input.get("context_id", "")
        if not context_id:
            return Response('{"error": "context_id is required"}', status=400, mimetype="application/json")

        after: int = int(input.get("after", 0))
        limit: int = min(int(input.get("limit", 50)), 250)

        events, last_seq = get_context_log_entries(context_id, after=after)
        events = events[:limit]

        return {
            "context_id": context_id,
            "events": events,
            "last_sequence": last_seq,
            "has_more": len(events) == limit,
        }
