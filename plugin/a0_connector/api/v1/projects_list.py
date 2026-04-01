"""POST /api/plugins/a0_connector/v1/projects_list."""
from __future__ import annotations

from helpers.api import Request, Response
import usr.plugins.a0_connector.api.v1.base as connector_base


class ProjectsList(connector_base.ProtectedConnectorApiHandler):
    async def process(self, input: dict, request: Request) -> dict | Response:
        from helpers import projects as projects_helper

        projects: list[dict[str, str]] = []
        for item in projects_helper.get_active_projects_list() or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).strip()
            if not name:
                continue
            projects.append(
                {
                    "name": name,
                    "title": str(item.get("title", "")).strip() or name,
                }
            )

        return {"projects": projects}
