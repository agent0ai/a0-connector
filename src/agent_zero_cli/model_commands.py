from __future__ import annotations

import asyncio
from copy import deepcopy
from typing import TYPE_CHECKING, Any

from agent_zero_cli.model_config import (
    apply_model_switcher_state,
    coerce_model_config,
    collect_provider_options,
    override_main_model,
)
from agent_zero_cli.state_sync import model_switcher_signature
from agent_zero_cli.screens.model_presets import ModelPresetsResult, ModelPresetsScreen
from agent_zero_cli.screens.model_runtime import ModelRuntimeResult, ModelRuntimeScreen
from agent_zero_cli.widgets.model_switcher_bar import ModelSwitcherBar
from agent_zero_cli.text_utils import as_mapping, coerce_bool

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
    app._model_switcher_signature = model_switcher_signature(payload)
    app._model_switch_allowed = allowed
    widget.set_state(**state_kwargs)
    widget.set_busy(False)


async def _apply_model_switcher_payload(
    app: AgentZeroCLI,
    payload: dict[str, Any],
    *,
    bar: ModelSwitcherBar | None = None,
    optimistic: bool = False,
) -> None:
    if not payload or "main_model" not in payload or "presets" not in payload:
        await refresh_model_switcher(app, silent=True)
        return

    allowed, state_kwargs = apply_model_switcher_state(payload)
    signature = model_switcher_signature(payload)
    app._model_switcher_signature = signature
    if optimistic:
        app._model_switcher_signature_pending = signature
        app._model_switcher_signature_pending_retries = 0
    app._model_switch_allowed = allowed

    if bar is not None:
        bar.set_state(**state_kwargs)
        return

    try:
        app.query_one("#model-switcher-bar", ModelSwitcherBar).set_state(**state_kwargs)
    except Exception:
        pass


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
        if not app.connected or app.client.http.is_closed:
            return
        await refresh_model_switcher(app)
        if app.connected and not app.client.http.is_closed:
            app._show_notice(f"Failed to update model preset: {exc}", error=True)
        return

    await _apply_model_switcher_payload(app, payload, bar=target_bar, optimistic=True)
    if target_bar is not None:
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
    app._model_switcher_signature = model_switcher_signature(switcher_payload)
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
    override_preset = str(override.get("preset_name") or "").strip()
    configured_preset = str(switcher_payload.get("configured_preset") or "").strip()
    effective_preset = str(switcher_payload.get("effective_preset") or override_preset).strip()
    current_preset = "" if override and not override_preset else effective_preset
    custom_override_label = ""
    if override and not current_preset:
        custom_override_label = str(override.get("name") or override.get("provider") or "Custom override").strip()

    result = await app.push_screen_wait(
        ModelPresetsScreen(
            presets=presets,
            current_preset=current_preset,
            configured_preset=configured_preset,
            override_active=bool(override),
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
    has_custom_override = bool(override) and not override_preset
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
    app._model_switcher_signature = model_switcher_signature(switcher_payload)
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
    preset_name = str(
        switcher_payload.get("effective_preset")
        or override.get("preset_name")
        or switcher_payload.get("configured_preset")
        or "Default"
    ).strip()
    presets = switcher_payload.get("presets")
    selected_preset = next(
        (
            preset
            for preset in presets
            if isinstance(preset, dict)
            and str(preset.get("name") or "").strip().casefold() == preset_name.casefold()
        ),
        None,
    ) if isinstance(presets, list) else None
    if not isinstance(selected_preset, dict):
        app._show_notice(f"Agent Zero did not provide model preset '{preset_name}'.", error=True)
        return

    preset_name = str(selected_preset["name"])
    main_model = {
        **coerce_model_config(selected_preset.get("chat")),
        **coerce_model_config(override_main_model(override)),
        **coerce_model_config(switcher_payload.get("main_model")),
    }
    utility_model = {
        **coerce_model_config(selected_preset.get("utility")),
        **coerce_model_config(override.get("utility")),
        **coerce_model_config(switcher_payload.get("utility_model")),
    }
    default_preset = next(
        (item for item in presets if isinstance(item, dict) and item.get("name") == "Default"),
        {},
    )
    supports_vision = True
    for source in (
        default_preset.get("chat"), selected_preset.get("chat"),
        override_main_model(override), switcher_payload.get("main_model"),
    ):
        if "vision" in as_mapping(source):
            supports_vision = coerce_bool(source["vision"])

    result = await app.push_screen_wait(
        ModelRuntimeScreen(
            preset_name=preset_name,
            main_model=main_model,
            utility_model=utility_model,
            supports_vision=supports_vision,
            vision_model=as_mapping(selected_preset.get("vision")),
            focus_target=focus_target,
            provider_options=collect_provider_options(switcher_payload),
        )
    )
    if result is None:
        return
    if not isinstance(result, ModelRuntimeResult):
        raise TypeError(f"Unexpected model runtime result: {result!r}")

    if not result.main_changed and not result.utility_changed and result.main_vision is None and result.vision_model is None:
        return

    presets_to_save = deepcopy(presets)
    preset_to_save = next(
        preset
        for preset in presets_to_save
        if isinstance(preset, dict)
        and preset.get("name") == preset_name
    )
    for slot, model, changed in (
        ("chat", result.main_model, result.main_changed),
        ("utility", result.utility_model, result.utility_changed),
        ("vision", result.vision_model, result.vision_model is not None),
    ):
        if not changed:
            continue
        if slot == "vision" and not model:
            preset_to_save[slot] = {}
            continue
        current = preset_to_save.get(slot)
        updated = dict(current) if isinstance(current, dict) else {}
        updated.pop("api_base", None)
        updated.pop("base_url", None)
        if str(updated.get("provider") or "").strip().casefold() != str(
            model.get("provider") or ""
        ).strip().casefold():
            updated.pop("kwargs", None)
        updated.update(model)
        preset_to_save[slot] = updated
    if result.main_vision is not None:
        preset_to_save["chat"] = {
            **as_mapping(preset_to_save.get("chat")), **result.main_model,
            "vision": result.main_vision,
        }

    try:
        saved = await app.client.save_model_presets(presets_to_save)
    except Exception as exc:
        app._show_notice(f"Failed to update model preset '{preset_name}': {exc}", error=True)
        return
    if not saved.get("ok"):
        app._show_notice(str(saved.get("message") or f"Failed to update model preset '{preset_name}'."), error=True)
        return

    try:
        payload = await app.client.set_model_preset(context_id, preset_name)
    except Exception as exc:
        app._show_notice(f"Model preset '{preset_name}' saved, but failed to activate it: {exc}", error=True)
        return

    await _apply_model_switcher_payload(app, payload, optimistic=True)

    await app._refresh_token_usage(context_id=context_id)
