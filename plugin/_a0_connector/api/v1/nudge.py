"""POST /api/plugins/_a0_connector/v1/nudge."""
from __future__ import annotations

from helpers.api import Request, Response
import plugins._a0_connector.api.v1.base as connector_base


class Nudge(connector_base.ProtectedConnectorApiHandler):
    async def process(self, input: dict, request: Request) -> dict | Response:
        from agent import AgentContext

        context_id = str(input.get("context_id", "")).strip()
        if not context_id:
            return Response(
                response='{"error": "context_id is required"}',
                status=400,
                mimetype="application/json",
            )

        context = AgentContext.get(context_id)
        if context is None:
            return Response(
                response='{"error": "Context not found"}',
                status=404,
                mimetype="application/json",
            )

        context.nudge()
        message = "Process reset, agent nudged."
        context.log.log(type="info", content=message)
        return {
            "ok": True,
            "context_id": context_id,
            "status": "nudged",
            "message": message,
        }
