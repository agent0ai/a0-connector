"""GET /api/plugins/a0_connector/v1/chat_get?context_id=...

Returns metadata and state summary for a specific context.
"""
from __future__ import annotations

from helpers.api import ApiHandler, Request, Response


class ChatGet(ApiHandler):
    @classmethod
    def requires_auth(cls) -> bool:
        return False

    @classmethod
    def requires_csrf(cls) -> bool:
        return False

    @classmethod
    def requires_api_key(cls) -> bool:
        return True

    async def process(self, input: dict, request: Request) -> dict | Response:
        from agent import AgentContext
        from usr.plugins.a0_connector.helpers.event_bridge import get_context_log_entries

        context_id: str = input.get("context_id", "")
        if not context_id:
            return Response('{"error": "context_id is required"}', status=400, mimetype="application/json")

        context = AgentContext.get(context_id)
        if context is None:
            return Response('{"error": "Context not found"}', status=404, mimetype="application/json")

        events, last_seq = get_context_log_entries(context_id)

        return {
            "context_id": context.id,
            "name": getattr(context, "name", context.id),
            "agent_profile": getattr(context.agent0.config, "profile", "default")
                if context.agent0 else "default",
            "log_entries": len(events),
            "last_sequence": last_seq,
        }
