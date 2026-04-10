"""POST /api/plugins/_a0_connector/v1/skills_list."""
from __future__ import annotations

from helpers.api import Request, Response
import plugins._a0_connector.api.v1.base as connector_base


class SkillsList(connector_base.ProtectedConnectorApiHandler):
    async def process(self, input: dict, request: Request) -> dict | Response:
        from helpers import files, projects, runtime, skills

        skill_list = skills.list_skills()
        project_name = str(input.get("project_name", "")).strip() or None

        if project_name:
            project_folder = projects.get_project_folder(project_name)
            if runtime.is_development():
                project_folder = files.normalize_a0_path(project_folder)
            skill_list = [
                item
                for item in skill_list
                if files.is_in_dir(str(item.path), project_folder)
            ]

        agent_profile = str(input.get("agent_profile", "")).strip() or None
        if agent_profile:
            roots: list[str] = [
                files.get_abs_path("agents", agent_profile, "skills"),
                files.get_abs_path("usr", "agents", agent_profile, "skills"),
            ]
            if project_name:
                roots.append(
                    projects.get_project_meta(project_name, "agents", agent_profile, "skills")
                )

            skill_list = [
                item
                for item in skill_list
                if any(files.is_in_dir(str(item.path), root) for root in roots)
            ]

        result = [
            {
                "name": skill.name,
                "description": skill.description,
                "path": str(skill.path),
            }
            for skill in skill_list
        ]
        result.sort(key=lambda item: (item["name"], item["path"]))

        return {
            "ok": True,
            "data": result,
        }
