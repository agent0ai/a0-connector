from __future__ import annotations

from typing import TYPE_CHECKING, Any, Mapping

from agent_zero_cli import profile_commands
from agent_zero_cli.screens.permissions import PermissionsResult, PermissionsScreen

if TYPE_CHECKING:
    from agent_zero_cli.app import AgentZeroCLI


def _policy_patch(policy: Mapping[str, Any]) -> dict[str, Any]:
    return {"mode": "inherit"} if policy.get("mode") != "custom" else dict(policy)


async def cmd_permissions(app: AgentZeroCLI) -> None:
    current_profile, options = await profile_commands.load_profile_menu_state(
        app, silent=False
    )
    if not current_profile:
        return
    if current_profile == "default":
        app._show_notice(
            "The Default utility profile has no editable permissions.", error=True
        )
        return

    state = await profile_commands._load_editor_state(app, current_profile)
    if state is None:
        return
    result = await app.push_screen_wait(PermissionsScreen(state))
    if not isinstance(result, PermissionsResult):
        return
    if not result.tool_changed and not result.skill_changed:
        app._show_notice("No permission changes to save.")
        return

    patch: dict[str, Any] = {
        "profile_id": current_profile,
        "creating": False,
        "editor_mode": "easy",
    }
    if result.tool_changed:
        patch["tool_policy"] = _policy_patch(result.tool_policy)
    if result.skill_changed:
        patch["skill_policy"] = _policy_patch(result.skill_policy)

    try:
        payload = await app.client.agent_editor(
            "save",
            context_id=app.current_context or "",
            patch=patch,
        )
    except Exception as exc:
        app._show_notice(f"Failed to save agent permissions: {exc}", error=True)
        return
    if not payload.get("ok"):
        app._show_notice(
            str(payload.get("message") or "Failed to save agent permissions."),
            error=True,
        )
        return

    label = profile_commands.profile_label(options, current_profile)
    app._show_notice(f"Saved permissions for {label}.")


__all__ = ["cmd_permissions"]
