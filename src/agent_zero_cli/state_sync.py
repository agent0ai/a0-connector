from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any, Mapping

from agent_zero_cli.widgets.model_switcher_bar import ModelSwitcherBar

if TYPE_CHECKING:
    from agent_zero_cli.app import AgentZeroCLI


SYNC_INTERVAL_SECONDS = 2.0


def snapshot_signature(payload: object) -> str:
    try:
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    except TypeError:
        return repr(payload)


def _settings_from_payload(payload: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping):
        return {}
    settings = payload.get("settings", payload)
    return settings if isinstance(settings, Mapping) else {}


def _apply_workspace_from_settings(app: AgentZeroCLI, payload: Mapping[str, Any] | None) -> None:
    settings = _settings_from_payload(payload)
    remote_workspace = str(settings.get("workdir_path") or "").strip()
    app._set_workspace_context(remote_workspace=remote_workspace)


async def refresh_settings_snapshot(
    app: AgentZeroCLI,
    payload: Mapping[str, Any] | None = None,
    *,
    silent: bool = True,
) -> bool:
    if "settings_get" not in app.connector_features:
        app._settings_snapshot_signature = ""
        app._set_workspace_context(remote_workspace="")
        return False

    if payload is None:
        try:
            payload = await app.client.get_settings()
        except Exception as exc:
            if not silent:
                app._show_notice(f"Failed to refresh Agent Zero settings: {exc}", error=True)
            return False

    signature = snapshot_signature(payload)
    if signature == app._settings_snapshot_signature:
        return False

    app._settings_snapshot_signature = signature
    _apply_workspace_from_settings(app, payload)
    app._sync_ready_actions()

    if app._is_profile_menu_open():
        await app._open_profile_menu()

    return True


async def refresh_model_switcher_snapshot(app: AgentZeroCLI, *, silent: bool = True) -> bool:
    if "model_switcher" not in app.connector_features or not app.current_context:
        app._model_switcher_signature = ""
        app._clear_model_switcher()
        return False

    try:
        payload = await app.client.get_model_switcher(app.current_context)
    except Exception as exc:
        if not silent:
            app._show_notice(f"Failed to refresh model switcher: {exc}", error=True)
        return False

    signature = snapshot_signature(payload)
    if signature == app._model_switcher_signature:
        return False

    app._model_switcher_signature = signature
    app._apply_model_switcher_state(payload)
    try:
        app.query_one("#model-switcher-bar", ModelSwitcherBar).set_busy(False)
    except Exception:
        pass
    return True


async def refresh_state_snapshot(app: AgentZeroCLI, *, silent: bool = True) -> None:
    settings_changed = await refresh_settings_snapshot(app, silent=silent)
    model_changed = await refresh_model_switcher_snapshot(app, silent=silent)
    if settings_changed or model_changed:
        await app._refresh_token_usage()


async def state_sync_loop(app: AgentZeroCLI) -> None:
    try:
        while app.connected:
            await refresh_state_snapshot(app)
            await asyncio.sleep(SYNC_INTERVAL_SECONDS)
    except asyncio.CancelledError:
        raise


def start_state_sync(app: AgentZeroCLI) -> None:
    stop_state_sync(app)
    app._state_sync_task = asyncio.create_task(state_sync_loop(app))


def stop_state_sync(app: AgentZeroCLI) -> None:
    task = app._state_sync_task
    app._state_sync_task = None
    if task is not None and not task.done():
        task.cancel()

