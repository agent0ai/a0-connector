"""POST /api/plugins/_a0_connector/v1/agents_list."""
from __future__ import annotations

from helpers.api import Request, Response
import plugins._a0_connector.api.v1.base as connector_base


class AgentsList(connector_base.ProtectedConnectorApiHandler):
    async def process(self, input: dict, request: Request) -> dict | Response:
        from helpers import subagents

        return {
            "ok": True,
            "data": subagents.get_all_agents_list(),
        }
