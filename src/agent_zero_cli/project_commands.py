from __future__ import annotations

import re
from typing import TYPE_CHECKING, Mapping, Sequence

from agent_zero_cli.project_utils import (
    display_project_title,
    normalize_project_list,
    normalize_project_summary,
    project_name,
    project_title,
)
from agent_zero_cli.screens.project_instructions import (
    ProjectInstructionsResult,
    ProjectInstructionsScreen,
)
if TYPE_CHECKING:
    from agent_zero_cli.app import AgentZeroCLI


_CLEAR_VALUES = {"", "default", "none", "clear", "off"}


async def cmd_project(app: AgentZeroCLI, query: str = "") -> None:
    availability = app._project_availability()
    if not availability.available:
        app._show_notice(availability.reason or "Projects are unavailable right now.", error=True)
        return

    desired = _strip_quotes(query)
    if desired:
        await _switch_project_from_query(app, desired)
        return

    await app._open_project_menu()

async def handle_project_menu_action(
    app: AgentZeroCLI,
    action: str,
    *,
    project_name_value: str | None = None,
) -> None:
    if action == "activate" and project_name_value:
        await _activate_project(app, project_name_value)
        return

    if action == "deactivate":
        await _deactivate_project(app)
        return

    if action == "edit" and project_name_value:
        await _edit_project_instructions(app, project_name_value)


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


async def _switch_project_from_query(app: AgentZeroCLI, desired: str) -> None:
    payload = await _load_projects_payload(app)
    if payload is None:
        return

    current_project = normalize_project_summary(payload.get("current_project"))
    projects = normalize_project_list(payload.get("projects"))
    normalized = _normalize_lookup(desired)

    if normalized in _CLEAR_VALUES:
        if current_project is None:
            app._show_notice("No project is active.")
            app._focus_message_input()
            return
        await _deactivate_project(app)
        return

    match, ambiguous = _match_named_project(projects, desired)
    if ambiguous:
        names = ", ".join(_format_project_entry(item) for item in ambiguous)
        app._show_notice(f"Project name is ambiguous. Matches: {names}", error=True)
        app._focus_message_input()
        return

    if match is None:
        available = ", ".join(_format_project_entry(item) for item in projects) or "none"
        app._show_notice(f"Project '{desired}' was not found. Available projects: {available}", error=True)
        app._focus_message_input()
        return

    match_name = project_name(match)
    if match_name and match_name == project_name(current_project):
        app._show_notice(f"Already using project {display_project_title(match)}.")
        app._focus_message_input()
        return

    await _activate_project(app, match_name)


async def _load_projects_payload(app: AgentZeroCLI) -> Mapping[str, object] | None:
    try:
        payload = await app.client.get_projects(app.current_context or "")
    except Exception as exc:
        app._show_notice(f"Failed to refresh projects: {exc}", error=True)
        return None

    if not isinstance(payload, Mapping):
        app._clear_project_state()
        app._show_notice("Project state unavailable.", error=True)
        return None

    if not payload.get("ok"):
        app._clear_project_state()
        app._show_notice(str(payload.get("error") or "Project state unavailable."), error=True)
        return None

    app._apply_projects_payload(payload)
    return payload


def _strip_quotes(value: str) -> str:
    trimmed = value.strip()
    if len(trimmed) >= 2 and trimmed[0] == trimmed[-1] and trimmed[0] in {'"', "'"}:
        return trimmed[1:-1].strip()
    return trimmed


def _normalize_lookup(value: str) -> str:
    lowered = value.lower().strip()
    lowered = re.sub(r"[\s_\-]+", " ", lowered)
    lowered = re.sub(r"[^a-z0-9 ]+", "", lowered)
    return lowered.strip()


def _match_named_project(
    items: Sequence[Mapping[str, object]],
    desired: str,
) -> tuple[dict[str, str] | None, list[dict[str, str]]]:
    normalized = _normalize_lookup(desired)
    if not normalized:
        return None, []

    normalized_items = [item for item in (normalize_project_summary(item) for item in items) if item is not None]

    exact_matches: list[dict[str, str]] = []
    for item in normalized_items:
        values = [_normalize_lookup(project_name(item)), _normalize_lookup(project_title(item))]
        if normalized in {value for value in values if value}:
            exact_matches.append(item)

    if len(exact_matches) == 1:
        return exact_matches[0], []
    if len(exact_matches) > 1:
        return None, exact_matches

    partial_matches: list[dict[str, str]] = []
    for item in normalized_items:
        values = [_normalize_lookup(project_name(item)), _normalize_lookup(project_title(item))]
        if any(normalized in value for value in values if value):
            partial_matches.append(item)

    if len(partial_matches) == 1:
        return partial_matches[0], []
    if len(partial_matches) > 1:
        return None, partial_matches

    return None, []


def _format_project_entry(project: Mapping[str, object]) -> str:
    title = project_title(project)
    name = project_name(project)
    if title and title.casefold() != name.casefold():
        return f"{title} ({name})"
    return name or title
