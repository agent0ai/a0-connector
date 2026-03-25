"""GET /api/plugins/a0_connector/v1/projects_list

Returns a list of available Agent Zero projects.
"""
from __future__ import annotations

from helpers.api import ApiHandler, Request, Response


class ProjectsList(ApiHandler):
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
        from helpers import projects as proj_module

        result = []
        try:
            all_projects = proj_module.get_projects()
            for p in all_projects:
                name = p.get("name") if isinstance(p, dict) else str(p)
                result.append({"name": name})
        except Exception:
            pass

        return {"projects": result}
