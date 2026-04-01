"""POST /api/plugins/a0_connector/v1/chat_create."""
from __future__ import annotations

from helpers.api import Request, Response
import usr.plugins.a0_connector.api.v1.base as connector_base


class ChatCreate(connector_base.ProtectedConnectorApiHandler):
    async def process(self, input: dict, request: Request) -> dict | Response:
        from agent import AgentContext, AgentContextType
        from helpers import projects
        from initialize import initialize_agent

        project_name = str(input.get("project_name", "")).strip() or None
        agent_profile = str(input.get("agent_profile", "")).strip() or None

        override_settings: dict[str, str] = {}
        if agent_profile:
            override_settings["agent_profile"] = agent_profile

        context = AgentContext(
            config=initialize_agent(override_settings=override_settings),
            type=AgentContextType.USER,
        )
        AgentContext.use(context.id)

        if project_name:
            try:
                projects.activate_project(context.id, project_name)
            except Exception as exc:
                return Response(
                    response=f'{{"error": "Failed to activate project: {str(exc)}"}}',
                    status=400,
                    mimetype="application/json",
                )

        context_data = context.output()
        return {
            "context_id": context.id,
            "created_at": context_data.get("created_at"),
            "agent_profile": agent_profile or getattr(context.agent0.config, "profile", "default"),
            "project_name": project_name,
        }
