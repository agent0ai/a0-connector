"""POST /api/plugins/a0_connector/v1/chat_create

Creates a new Agent Zero context (chat).
"""
from __future__ import annotations

from helpers.api import ApiHandler, Request, Response


class ChatCreate(ApiHandler):
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
        from agent import AgentContext, AgentContextType
        from initialize import initialize_agent
        from helpers import projects

        project_name: str | None = input.get("project_name")
        agent_profile: str | None = input.get("agent_profile")

        override_settings: dict = {}
        if agent_profile:
            override_settings["agent_profile"] = agent_profile

        config = initialize_agent(override_settings=override_settings)
        context = AgentContext(config=config, type=AgentContextType.USER)
        AgentContext.use(context.id)

        if project_name:
            try:
                projects.activate_project(context.id, project_name)
            except Exception as e:
                return Response(
                    f'{{"error": "Failed to activate project: {str(e)}"}}',
                    status=400,
                    mimetype="application/json",
                )

        return {
            "context_id": context.id,
            "agent_profile": agent_profile or "default",
            "project_name": project_name,
        }
