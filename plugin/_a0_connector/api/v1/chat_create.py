"""POST /api/plugins/_a0_connector/v1/chat_create."""
from __future__ import annotations

from helpers.api import Request, Response
import plugins._a0_connector.api.v1.base as connector_base


class ChatCreate(connector_base.ProtectedConnectorApiHandler):
    async def process(self, input: dict, request: Request) -> dict | Response:
        from helpers import projects
        from plugins._a0_connector.helpers.chat_context import create_context

        current_context_id = (
            str(input.get("current_context", input.get("current_context_id", ""))).strip()
            or None
        )
        project_name = str(input.get("project_name", "")).strip() or None
        agent_profile = str(input.get("agent_profile", "")).strip() or None

        try:
            context = create_context(
                lock=self.thread_lock,
                current_context_id=current_context_id,
                agent_profile=agent_profile,
                project_name=project_name,
            )
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
            "project_name": context.get_data(projects.CONTEXT_DATA_KEY_PROJECT),
        }
