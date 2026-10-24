from __future__ import annotations

from typing import TYPE_CHECKING

from agent_zero_cli.screens.composer_choice import ComposerChoiceScreen
from agent_zero_cli.widgets.model_switcher_bar import ModelSwitcherBar

if TYPE_CHECKING:
    from agent_zero_cli.app import AgentZeroCLI


def clear_controls(app: AgentZeroCLI) -> None:
    app._composer_controls_generation += 1
    app.composer_controls = {}
    app._composer_controls_scope = None
    try:
        app.query_one("#model-switcher-bar", ModelSwitcherBar).set_composer_controls([])
    except Exception:
        pass


async def refresh_controls(app: AgentZeroCLI, *, silent: bool = True) -> None:
    if not app.connected or not app.current_context or "composer_controls" not in app.connector_features:
        clear_controls(app)
        return
    if app._composer_controls_saving:
        return
    app._composer_controls_generation += 1
    generation = app._composer_controls_generation
    client = app.client
    scope = (client.base_url, app.current_context)
    if app._composer_controls_scope != scope:
        app.composer_controls = {}
        app.query_one("#model-switcher-bar", ModelSwitcherBar).set_composer_controls([])
    try:
        payload = await client.composer_controls_action("list", scope[1])
    except Exception as exc:
        payload = {}
        if not silent:
            app._show_notice(f"Failed to load composer controls: {exc}", error=True)
    if (
        generation != app._composer_controls_generation or not app.connected
        or app.client is not client or (client.base_url, app.current_context) != scope
    ):
        return
    controls = {}
    items = payload.get("controls")
    if payload.get("ok") and isinstance(items, list):
        for item in items:
            if (
                isinstance(item, dict) and item.get("type") == "choice"
                and isinstance(item.get("id"), str) and item["id"]
                and isinstance(item.get("options"), list)
                and isinstance(item.get("value"), str)
            ):
                controls.setdefault(item["id"], item)
    app.composer_controls = controls
    app._composer_controls_scope = scope
    app.query_one("#model-switcher-bar", ModelSwitcherBar).set_composer_controls(list(controls.values()))


async def choose_control(app: AgentZeroCLI, control_id: str, anchor_id: str) -> None:
    await refresh_controls(app, silent=False)
    control = app.composer_controls.get(control_id)
    if not control:
        return
    client = app.client
    scope = app._composer_controls_scope
    custom_value = error = ""
    while app.connected and app.client is client and (client.base_url, app.current_context) == scope:
        choice = await app.push_screen_wait(
            ComposerChoiceScreen(control, anchor_id=anchor_id, custom_value=custom_value, error=error)
        )
        if choice is None:
            break
        if not app.connected or app.client is not client or (client.base_url, app.current_context) != scope:
            break
        app._composer_controls_saving = True
        app._composer_controls_generation += 1
        try:
            result = await client.composer_controls_action(
                "set", scope[1], control_id=control_id, value=choice.value,
                custom=choice.custom, revision=control.get("revision"),
            )
        except Exception as exc:
            result = {"ok": False, "message": str(exc)}
        finally:
            app._composer_controls_saving = False
        if app.client is not client or (client.base_url, app.current_context) != scope or not app.connected:
            break
        if result.get("ok"):
            await refresh_controls(app)
            break
        error = str(result.get("message") or "Could not update this control.")
        if result.get("status_code") in {401, 403, 404, 409}:
            app._show_notice(error, error=True)
            await refresh_controls(app)
            break
        custom_value = choice.value if choice.custom else ""
    app._focus_message_input()
