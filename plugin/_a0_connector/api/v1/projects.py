"""POST /api/plugins/_a0_connector/v1/projects."""
from __future__ import annotations

from typing import Any, Mapping

from helpers.api import Request, Response
import plugins._a0_connector.api.v1.base as connector_base


def _string(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _normalize_project_summary(value: object) -> dict[str, str] | None:
    if not isinstance(value, Mapping):
        return None

    name = _string(value.get("name"))
    if not name:
        return None

    return {
        "name": name,
        "title": _string(value.get("title")),
        "description": _string(value.get("description")),
        "color": _string(value.get("color")),
    }


class Projects(connector_base.ProtectedConnectorApiHandler):
    """Thin connector proxy around the core `api.projects.Projects` surface."""

    async def process(self, input: dict, request: Request) -> dict | Response:
        action = _string(input.get("action")).lower() or "list"
        if action not in {"list", "load", "update", "activate", "deactivate"}:
            return {"ok": False, "error": f"Unsupported action: {action or '<missing>'}"}

        core_response = await self._call_core(
            {
                "action": action,
                "context_id": _string(input.get("context_id")),
                "name": _string(input.get("name")),
                "project": input.get("project"),
            },
            request,
        )
        if isinstance(core_response, Response):
            return core_response
        if not isinstance(core_response, Mapping):
            return {"ok": False, "error": "Invalid response from core projects handler"}
        if not core_response.get("ok"):
            return {"ok": False, "error": _string(core_response.get("error")) or "Project request failed"}

        if action in {"activate", "deactivate", "list"}:
            return await self._normalized_list_state(_string(input.get("context_id")), request)

        project = core_response.get("data")
        return {
            "ok": True,
            "project": dict(project) if isinstance(project, Mapping) else {},
        }

    async def _normalized_list_state(self, context_id: str, request: Request) -> dict[str, Any] | Response:
        core_response = await self._call_core(
            {
                "action": "list",
                "context_id": context_id,
            },
            request,
        )
        if isinstance(core_response, Response):
            return core_response
        if not isinstance(core_response, Mapping):
            return {"ok": False, "error": "Invalid response from core projects handler"}
        if not core_response.get("ok"):
            return {"ok": False, "error": _string(core_response.get("error")) or "Project request failed"}

        projects: list[dict[str, str]] = []
        for item in core_response.get("data") or []:
            normalized = _normalize_project_summary(item)
            if normalized is not None:
                projects.append(normalized)

        return {
            "ok": True,
            "projects": projects,
            "current_project": self._load_current_project(context_id),
        }

    async def _call_core(self, payload: dict[str, Any], request: Request) -> dict | Response:
        from api.projects import Projects as CoreProjects

        handler = CoreProjects(self.app, self.thread_lock)
        return await handler.process(payload, request)

    def _load_current_project(self, context_id: str) -> dict[str, str] | None:
        if not context_id:
            return None

        from agent import AgentContext

        context = AgentContext.get(context_id)
        if context is None:
            return None

        return _normalize_project_summary(context.get_output_data("project"))
