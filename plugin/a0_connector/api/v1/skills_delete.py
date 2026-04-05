"""POST /api/plugins/a0_connector/v1/skills_delete."""
from __future__ import annotations

from helpers.api import Request, Response
import usr.plugins.a0_connector.api.v1.base as connector_base


class SkillsDelete(connector_base.ProtectedConnectorApiHandler):
    async def process(self, input: dict, request: Request) -> dict | Response:
        from helpers import skills

        skill_path = str(input.get("skill_path") or "").strip()
        if not skill_path:
            return Response(
                response='{"error":"skill_path is required"}',
                status=400,
                mimetype="application/json",
            )

        skills.delete_skill(skill_path)
        return {
            "ok": True,
            "data": {
                "skill_path": skill_path,
            },
        }
