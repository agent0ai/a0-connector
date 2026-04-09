from __future__ import annotations

from typing import Any, Mapping


def format_provider_label(provider: str) -> str:
    value = provider.strip().lower()
    if not value:
        return ""
    return value.replace("_", " ").title()


def coerce_positive_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        if value.is_integer():
            integer = int(value)
            return integer if integer >= 0 else None
        return None
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    try:
        integer = int(text)
    except ValueError:
        return None
    return integer if integer >= 0 else None


def extract_token_limit(stats: Mapping[str, object] | None) -> int | None:
    if not isinstance(stats, Mapping):
        return None
    for key in (
        "context_window",
        "context_limit",
        "max_context_tokens",
        "max_tokens",
        "token_limit",
    ):
        value = coerce_positive_int(stats.get(key))
        if value is not None and value > 0:
            return value
    return None


def coerce_model_config(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    payload: dict[str, str] = {}
    for key in ("provider", "name", "api_key"):
        text = str(value.get(key) or "").strip()
        if text:
            payload[key] = text
    # Standardize on api_base as expected by Agent Zero core models.py
    base_url = str(value.get("api_base") or value.get("base_url") or "").strip()
    if base_url:
        payload["api_base"] = base_url

    return payload


def format_model_label(value: object, *, default: str = "Connector default") -> str:
    if isinstance(value, Mapping):
        label = str(value.get("label") or "").strip()
        provider = str(value.get("provider") or "").strip()
        name = str(value.get("name") or "").strip()
        if label:
            return label
        if provider and name:
            return f"{provider}/{name}"
        return name or provider or default
    text = str(value or "").strip()
    return text or default


def apply_model_switcher_state(payload: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    """
    Returns (allowed, state_kwargs)
    where state_kwargs can be unpacked into ModelSwitcherBar.set_state(**state_kwargs)
    """
    presets = payload.get("presets") if isinstance(payload.get("presets"), list) else []
    override = payload.get("override") if isinstance(payload.get("override"), dict) else {}
    selected_preset = str(override.get("preset_name") or "").strip()
    override_label = ""
    main_model = payload.get("main_model") if isinstance(payload.get("main_model"), Mapping) else {}

    if override and not selected_preset:
        override_label = str(override.get("name") or override.get("provider") or "Custom override").strip()
    elif selected_preset:
        preset_names = {
            str(item.get("name") or item.get("value") or "").strip()
            for item in presets
            if isinstance(item, dict)
        }
        if selected_preset not in preset_names:
            override_label = f"Preset: {selected_preset}"

    allowed = bool(payload.get("allowed"))

    state_kwargs = {
        "main_model": main_model,
        "utility_model": payload.get("utility_model"),
        "presets": presets,
        "allowed": allowed,
        "selected_preset": selected_preset,
        "override_label": override_label,
    }
    
    return allowed, state_kwargs


def collect_provider_options(switcher_payload: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    ordered: list[tuple[str, str]] = []
    seen: set[str] = set()

    def _add(provider: object, label: object = "") -> None:
        value = str(provider or "").strip().lower()
        if not value:
            return
        if value in seen:
            return
        seen.add(value)
        label_text = str(label or "").strip() or format_provider_label(value)
        ordered.append((label_text or value, value))

    chat_providers = switcher_payload.get("chat_providers")
    if isinstance(chat_providers, list):
        for provider in chat_providers:
            if not isinstance(provider, Mapping):
                continue
            _add(
                provider.get("value") or provider.get("id"),
                provider.get("label") or provider.get("name"),
            )

    _add(coerce_model_config(switcher_payload.get("main_model")).get("provider"))
    _add(coerce_model_config(switcher_payload.get("utility_model")).get("provider"))

    override = switcher_payload.get("override")
    if isinstance(override, Mapping):
        _add(coerce_model_config(override.get("chat")).get("provider"))
        _add(coerce_model_config(override.get("utility")).get("provider"))

    presets = switcher_payload.get("presets")
    if isinstance(presets, list):
        for item in presets:
            if not isinstance(item, Mapping):
                continue
            _add(coerce_model_config(item.get("chat") or item.get("main")).get("provider"))
            _add(coerce_model_config(item.get("utility")).get("provider"))

    return tuple(ordered)


def collect_provider_api_key_status(switcher_payload: Mapping[str, Any]) -> dict[str, bool]:
    status: dict[str, bool] = {}

    chat_providers = switcher_payload.get("chat_providers")
    if isinstance(chat_providers, list):
        for provider in chat_providers:
            if not isinstance(provider, Mapping):
                continue
            value = str(provider.get("value") or provider.get("id") or "").strip().lower()
            if not value:
                continue
            status[value] = bool(provider.get("has_api_key"))

    for key in ("main_model", "utility_model"):
        model_payload = switcher_payload.get(key)
        if not isinstance(model_payload, Mapping):
            continue
        provider = str(model_payload.get("provider") or "").strip().lower()
        if not provider:
            continue
        if provider not in status:
            status[provider] = bool(model_payload.get("has_api_key"))

    return status
