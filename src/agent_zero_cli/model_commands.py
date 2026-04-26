from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from agent_zero_cli.model_config import (
    apply_model_switcher_state,
    coerce_model_config,
    collect_provider_options,
    collect_provider_api_key_status,
)
from agent_zero_cli.state_sync import snapshot_signature
from agent_zero_cli.screens.model_presets import ModelPresetsResult, ModelPresetsScreen
from agent_zero_cli.screens.model_runtime import ModelRuntimeResult, ModelRuntimeScreen
from agent_zero_cli.widgets.model_switcher_bar import ModelSwitcherBar

if TYPE_CHECKING:
    from agent_zero_cli.app import AgentZeroCLI


def clear_model_switcher(app: AgentZeroCLI) -> None:
    app._model_switch_allowed = False
    try:
        app.query_one("#model-switcher-bar", ModelSwitcherBar).clear()
    except Exception:
        pass


async def refresh_model_switcher(app: AgentZeroCLI, *, silent: bool = True) -> None:
    if "model_switcher" not in app.connector_features or not app.current_context:
        clear_model_switcher(app)
        return

    widget = app.query_one("#model-switcher-bar", ModelSwitcherBar)
    widget.set_busy(True)
    try:
        payload = await app.client.get_model_switcher(app.current_context)
    except Exception as exc:
        clear_model_switcher(app)
        if not silent:
            app._show_notice(f"Failed to load model switcher: {exc}", error=True)
        return

    allowed, state_kwargs = apply_model_switcher_state(payload)
    app._model_switcher_signature = snapshot_signature(payload)
    app._model_switch_allowed = allowed
    widget.set_state(**state_kwargs)
    widget.set_busy(False)


async def set_model_preset(
    app: AgentZeroCLI,
    preset_name: str | None,
    *,
    bar: ModelSwitcherBar | None = None,
) -> None:
    if "model_switcher" not in app.connector_features:
        app._show_notice("Model presets are unavailable on this connector build.", error=True)
        return
    if not app.current_context:
        app._show_notice("Open or create a chat context before switching model presets.", error=True)
        return

    target_bar = bar
    if target_bar is None:
        try:
            target_bar = app.query_one("#model-switcher-bar", ModelSwitcherBar)
        except Exception:
            target_bar = None

    if target_bar is not None:
        target_bar.set_busy(True)

    try:
        payload = await app.client.set_model_preset(app.current_context, preset_name or None)
    except Exception as exc:
        if target_bar is not None:
            target_bar.set_busy(False)
        await refresh_model_switcher(app)
        app._show_notice(f"Failed to update model preset: {exc}", error=True)
        return

    allowed, state_kwargs = apply_model_switcher_state(payload)
    app._model_switcher_signature = snapshot_signature(payload)
    app._model_switch_allowed = allowed
    if target_bar is not None:
        target_bar.set_state(**state_kwargs)
        target_bar.set_busy(False)
    
    # We call back into app to refresh tokens (which is a global state logic)
    await app._refresh_token_usage()


async def cmd_model_presets(app: AgentZeroCLI) -> None:
    availability = app._model_presets_availability()
    if not availability.available:
        app._show_notice(availability.reason or "Model presets are unavailable.", error=True)
        return

    context_id = app.current_context or ""
    try:
        switcher_payload, presets = await asyncio.gather(
            app.client.get_model_switcher(context_id),
            app.client.get_model_presets(),
        )
    except Exception as exc:
        app._show_notice(f"Failed to load model presets: {exc}", error=True)
        return

    allowed, state_kwargs = apply_model_switcher_state(switcher_payload)
    app._model_switcher_signature = snapshot_signature(switcher_payload)
    app._model_switch_allowed = allowed
    try:
        app.query_one("#model-switcher-bar", ModelSwitcherBar).set_state(**state_kwargs)
    except Exception:
        pass

    availability = app._model_presets_availability()
    if not availability.available:
        app._show_notice(availability.reason or "Model presets are unavailable.", error=True)
        return

    override = switcher_payload.get("override") if isinstance(switcher_payload.get("override"), dict) else {}
    current_preset = str(override.get("preset_name") or "").strip()
    custom_override_label = ""
    if override and not current_preset:
        custom_override_label = str(override.get("name") or override.get("provider") or "Custom override").strip()

    result = await app.push_screen_wait(
        ModelPresetsScreen(
            presets=presets,
            current_preset=current_preset,
            switch_allowed=bool(switcher_payload.get("allowed")),
            reason="Model preset switching is unavailable for this chat.",
            current_override_label=custom_override_label,
        )
    )
    if result is None:
        return
    if not isinstance(result, ModelPresetsResult):
        raise TypeError(f"Unexpected model presets result: {result!r}")

    selected = result.preset_name or ""
    has_custom_override = bool(override) and not current_preset
    if selected == current_preset and not has_custom_override:
        return
    await set_model_preset(app, selected or None)


async def cmd_models(app: AgentZeroCLI, *, focus_target: str = "main") -> None:
    availability = app._model_runtime_availability()
    if not availability.available:
        app._show_notice(availability.reason or "Model runtime editing is unavailable.", error=True)
        return

    context_id = app.current_context or ""
    try:
        switcher_payload = await app.client.get_model_switcher(context_id)
    except Exception as exc:
        app._show_notice(f"Failed to load model runtime settings: {exc}", error=True)
        return

    allowed, state_kwargs = apply_model_switcher_state(switcher_payload)
    app._model_switcher_signature = snapshot_signature(switcher_payload)
    app._model_switch_allowed = allowed
    try:
        app.query_one("#model-switcher-bar", ModelSwitcherBar).set_state(**state_kwargs)
    except Exception:
        pass

    availability = app._model_runtime_availability()
    if not availability.available:
        app._show_notice(availability.reason or "Model runtime editing is unavailable.", error=True)
        return

    override = switcher_payload.get("override") if isinstance(switcher_payload.get("override"), dict) else {}
    main_payload = switcher_payload.get("main_model") if isinstance(switcher_payload.get("main_model"), dict) else {}
    utility_payload = (
        switcher_payload.get("utility_model")
        if isinstance(switcher_payload.get("utility_model"), dict)
        else {}
    )
    main_model = coerce_model_config(override.get("chat") if isinstance(override, dict) else None)
    utility_model = coerce_model_config(override.get("utility") if isinstance(override, dict) else None)
    if not main_model:
        main_model = coerce_model_config(main_payload)
    if not utility_model:
        utility_model = coerce_model_config(utility_payload)

    result = await app.push_screen_wait(
        ModelRuntimeScreen(
            main_model=main_model,
            utility_model=utility_model,
            focus_target=focus_target,
            provider_options=collect_provider_options(switcher_payload),
            provider_api_key_status=collect_provider_api_key_status(switcher_payload),
            main_has_api_key=bool(main_payload.get("has_api_key")),
            utility_has_api_key=bool(utility_payload.get("has_api_key")),
        )
    )
    if result is None:
        return
    if not isinstance(result, ModelRuntimeResult):
        raise TypeError(f"Unexpected model runtime result: {result!r}")

    try:
        payload = await app.client.set_model_override(
            context_id,
            main_model=result.main_model,
            utility_model=result.utility_model,
        )
    except Exception as exc:
        app._show_notice(f"Failed to update model runtime override: {exc}", error=True)
        return
    if not payload.get("ok"):
        app._show_notice(str(payload.get("message") or "Failed to update model runtime override."), error=True)
        return

    allowed, state_kwargs = apply_model_switcher_state(payload)
    app._model_switcher_signature = snapshot_signature(payload)
    app._model_switch_allowed = allowed
    try:
        app.query_one("#model-switcher-bar", ModelSwitcherBar).set_state(**state_kwargs)
    except Exception:
        pass

    await app._refresh_token_usage(context_id=context_id)
