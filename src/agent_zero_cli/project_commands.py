from __future__ import annotations

from typing import TYPE_CHECKING, Mapping

from agent_zero_cli.project_utils import display_project_title, normalize_project_summary, project_name
from agent_zero_cli.screens.project_instructions import (
    ProjectInstructionsResult,
    ProjectInstructionsScreen,
)
from agent_zero_cli.screens.project_menu import ProjectMenuResult, ProjectMenuScreen

if TYPE_CHECKING:
    from agent_zero_cli.app import AgentZeroCLI


async def cmd_project(app: AgentZeroCLI) -> None:
    availability = app._project_availability()
    if not availability.available:
        app._show_notice(availability.reason or "Projects are unavailable right now.", error=True)
        return

    await app._refresh_projects(context_id=app.current_context, silent=False)
    result = await app.push_screen_wait(
        ProjectMenuScreen(
            app.project_list,
            current_project=app.current_project,
        )
    )
    if not isinstance(result, ProjectMenuResult):
        return

    if result.action == "activate" and result.project_name:
        await _activate_project(app, result.project_name)
        return

    if result.action == "deactivate":
        await _deactivate_project(app)
        return

    if result.action == "edit" and result.project_name:
        await _edit_project_instructions(app, result.project_name)


async def _activate_project(app: AgentZeroCLI, project_name_value: str) -> None:
    try:
        payload = await app.client.activate_project(app.current_context or "", project_name_value)
    except Exception as exc:
        app._show_notice(f"Failed to activate project: {exc}", error=True)
        return

    if not payload.get("ok"):
        app._show_notice(str(payload.get("error") or "Failed to activate project."), error=True)
        return

    app._apply_projects_payload(payload)
    app._focus_message_input()


async def _deactivate_project(app: AgentZeroCLI) -> None:
    try:
        payload = await app.client.deactivate_project(app.current_context or "")
    except Exception as exc:
        app._show_notice(f"Failed to deactivate project: {exc}", error=True)
        return

    if not payload.get("ok"):
        app._show_notice(str(payload.get("error") or "Failed to deactivate project."), error=True)
        return

    app._apply_projects_payload(payload)
    app._focus_message_input()


async def _edit_project_instructions(app: AgentZeroCLI, project_name_value: str) -> None:
    try:
        payload = await app.client.load_project(project_name_value)
    except Exception as exc:
        app._show_notice(f"Failed to load project: {exc}", error=True)
        return

    if not payload.get("ok"):
        app._show_notice(str(payload.get("error") or "Failed to load project."), error=True)
        return

    project = payload.get("project")
    if not isinstance(project, Mapping):
        app._show_notice("Project payload was invalid.", error=True)
        return

    normalized_current = normalize_project_summary(app.current_project)
    display_title = display_project_title(project, default=project_name_value)
    result = await app.push_screen_wait(
        ProjectInstructionsScreen(
            title=display_title,
            name=project_name(project) or project_name_value,
            instructions=str(project.get("instructions") or ""),
        )
    )
    if not isinstance(result, ProjectInstructionsResult):
        return

    updated_project = dict(project)
    updated_project["instructions"] = result.instructions

    try:
        update_payload = await app.client.update_project(updated_project)
    except Exception as exc:
        app._show_notice(f"Failed to save project instructions: {exc}", error=True)
        return

    if not update_payload.get("ok"):
        app._show_notice(str(update_payload.get("error") or "Failed to save project instructions."), error=True)
        return

    await app._refresh_projects(context_id=app.current_context, silent=False)
    if normalized_current is not None:
        app._show_notice(f"Saved instructions for {display_project_title(normalized_current)}.")
    app._focus_message_input()
