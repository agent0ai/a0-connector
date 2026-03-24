"""GET /api/plugins/a0_connector/v1/chats_list

Returns all active Agent Zero contexts in a connector-friendly format.
"""
from __future__ import annotations

from helpers.api import ApiHandler, Request, Response


class ChatsList(ApiHandler):
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

        chats = []
        for ctx in AgentContext.get_all():
            chats.append({
                "id": ctx.id,
                "name": getattr(ctx, "name", ctx.id),
                "agent_profile": getattr(ctx.agent0.config, "profile", "default")
                    if ctx.agent0 else "default",
            })

        return {"chats": chats}
