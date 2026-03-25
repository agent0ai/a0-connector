"""POST /api/plugins/a0_connector/v1/chat_reset

Resets the agent state for a specific context.
"""
from __future__ import annotations

from helpers.api import ApiHandler, Request, Response


class ChatReset(ApiHandler):
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
        from agent import AgentContext

        context_id: str = input.get("context_id", "")
        if not context_id:
            return Response('{"error": "context_id is required"}', status=400, mimetype="application/json")

        context = AgentContext.get(context_id)
        if context is None:
            return Response('{"error": "Context not found"}', status=404, mimetype="application/json")

        context.reset()
        return {"context_id": context_id, "status": "reset"}
