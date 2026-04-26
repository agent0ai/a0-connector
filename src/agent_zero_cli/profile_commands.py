from __future__ import annotations

from typing import TYPE_CHECKING, Any, Mapping, Sequence

if TYPE_CHECKING:
    from agent_zero_cli.app import AgentZeroCLI


ProfileOption = dict[str, str]


def _normalize_profile_match(value: str) -> str:
    return " ".join(value.strip().casefold().split())


def _normalize_profile_options(raw_options: object) -> list[ProfileOption]:
    if not isinstance(raw_options, list):
        return []

    options: list[ProfileOption] = []
    seen: set[str] = set()
    for raw_option in raw_options:
        if not isinstance(raw_option, Mapping):
            continue
        key = str(raw_option.get("key") or raw_option.get("value") or "").strip()
        if not key or key in seen:
            continue
        label = str(raw_option.get("label") or key).strip() or key
        options.append({"key": key, "label": label})
        seen.add(key)
    return options


def profile_menu_state_from_settings(
    payload: Mapping[str, Any] | None,
    *,
    current_profile: str | None = None,
) -> tuple[str, list[ProfileOption]]:
    if not isinstance(payload, Mapping):
        return "", []

    settings = payload.get("settings", payload)
    additional = payload.get("additional")

    selected_profile = str(current_profile or "").strip()
    if not selected_profile and isinstance(settings, Mapping):
        selected_profile = str(settings.get("agent_profile") or "").strip()

    raw_options = additional.get("agent_subdirs") if isinstance(additional, Mapping) else None
    options = _normalize_profile_options(raw_options)
    if selected_profile and selected_profile not in {option["key"] for option in options}:
        options.insert(0, {"key": selected_profile, "label": selected_profile})
    return selected_profile, options


def profile_label(options: Sequence[Mapping[str, object]], profile_key: str) -> str:
    normalized_key = profile_key.strip()
    if not normalized_key:
        return ""

    for option in options:
        key = str(option.get("key") or option.get("value") or "").strip()
        if key != normalized_key:
            continue
        label = str(option.get("label") or key).strip()
        return label or normalized_key
    return normalized_key


def resolve_profile_selection(
    options: Sequence[Mapping[str, object]],
    query: str,
) -> tuple[ProfileOption | None, str | None]:
    normalized_query = _normalize_profile_match(query)
    if not normalized_query:
        return None, "Choose an agent profile first."

    exact_key_matches: list[ProfileOption] = []
    exact_label_matches: list[ProfileOption] = []
    prefix_matches: list[ProfileOption] = []

    for option in options:
        key = str(option.get("key") or "").strip()
        label = str(option.get("label") or key).strip() or key
        if not key:
            continue

        normalized_option = {"key": key, "label": label}
        normalized_key = _normalize_profile_match(key)
        normalized_label = _normalize_profile_match(label)

        if normalized_key == normalized_query:
            exact_key_matches.append(normalized_option)
            continue
        if normalized_label == normalized_query:
            exact_label_matches.append(normalized_option)
            continue
        if normalized_key.startswith(normalized_query) or normalized_label.startswith(normalized_query):
            prefix_matches.append(normalized_option)

    if exact_key_matches:
        return exact_key_matches[0], None
    if len(exact_label_matches) == 1:
        return exact_label_matches[0], None
    if len(prefix_matches) == 1:
        return prefix_matches[0], None

    if len(exact_label_matches) > 1 or len(prefix_matches) > 1:
        matches = exact_label_matches if len(exact_label_matches) > 1 else prefix_matches
        labels = ", ".join(profile_label(matches, option["key"]) for option in matches[:6])
        suffix = "..." if len(matches) > 6 else ""
        return None, f"Profile '{query.strip()}' is ambiguous. Matches: {labels}{suffix}"

    available = ", ".join(
        option["key"]
        for option in options[:8]
        if str(option.get("key") or "").strip()
    )
    suffix = ", ..." if len(options) > 8 else ""
    return None, f"Unknown profile: {query.strip()}. Available profiles: {available}{suffix}"


async def load_profile_menu_state(
    app: AgentZeroCLI,
    *,
    silent: bool = True,
) -> tuple[str, list[ProfileOption]]:
    try:
        payload = await app.client.get_settings()
    except Exception as exc:
        if not silent:
            app._show_notice(f"Failed to load agent profiles: {exc}", error=True)
        return "", []

    default_profile, options = profile_menu_state_from_settings(payload)
    current_profile = await _load_current_context_profile(app, fallback=default_profile)
    current_profile, options = profile_menu_state_from_settings(payload, current_profile=current_profile)
    if not options and not silent:
        app._show_notice("No agent profiles are available from Agent Zero Core.", error=True)
    return current_profile, options


async def _load_current_context_profile(app: AgentZeroCLI, *, fallback: str = "") -> str:
    context_id = app.current_context or ""
    if not context_id or "chat_get" not in app.connector_features:
        return fallback

    try:
        payload = await app.client.get_chat(context_id)
    except Exception:
        return fallback

    if not isinstance(payload, Mapping):
        return fallback
    profile = str(payload.get("agent_profile") or "").strip()
    return profile or fallback


async def apply_profile_selection(
    app: AgentZeroCLI,
    profile_key: str,
    *,
    options: Sequence[Mapping[str, object]] | None = None,
) -> bool:
    normalized_key = profile_key.strip()
    if not normalized_key:
        app._show_notice("Choose an agent profile first.", error=True)
        return False

    context_id = app.current_context or ""
    if not context_id:
        app._show_notice("Open or create a chat context before changing the agent profile.", error=True)
        return False

    try:
        payload = await app.client.set_agent_profile(context_id, normalized_key)
    except Exception as exc:
        app._show_notice(f"Failed to update agent profile: {exc}", error=True)
        return False

    if not payload.get("ok"):
        app._show_notice(str(payload.get("message") or "Failed to update agent profile."), error=True)
        return False

    updated_profile = str(payload.get("agent_profile") or normalized_key).strip()
    label = str(payload.get("agent_profile_label") or "").strip()
    if not label:
        label = profile_label(list(options or ()), updated_profile or normalized_key)
    app._show_notice(f"Agent profile set to {label}.")
    await app._refresh_model_switcher(silent=True)
    await app._refresh_token_usage(context_id=context_id)
    return True


async def cmd_profile(app: AgentZeroCLI, *, query: str = "") -> None:
    availability = app._profile_availability()
    if not availability.available:
        app._show_notice(availability.reason or "Agent profiles are unavailable right now.", error=True)
        return

    if query.strip():
        current_profile, options = await load_profile_menu_state(app, silent=False)
        del current_profile
        if not options:
            return

        resolved, error_message = resolve_profile_selection(options, query)
        if resolved is None:
            app._show_notice(error_message or "Unknown agent profile.", error=True)
            return

        await apply_profile_selection(app, resolved["key"], options=options)
        return

    await app._open_profile_menu()
