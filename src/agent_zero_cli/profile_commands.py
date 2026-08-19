from __future__ import annotations

import shlex
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from agent_zero_cli.project_utils import project_name
from agent_zero_cli.screens.profile_editor import ProfileEditorResult, ProfileEditorScreen

if TYPE_CHECKING:
    from agent_zero_cli.app import AgentZeroCLI


ProfileOption = dict[str, str]
_SPECIFICS = "agent.system.main.specifics.md"


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
        if not key or key == "default" or key in seen:
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
    if selected_profile and selected_profile != "default" and selected_profile not in {
        option["key"] for option in options
    }:
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
        try:
            tokens = shlex.split(query)
        except ValueError as exc:
            app._show_notice(f"Could not parse profile command: {exc}", error=True)
            return

        current_profile, options = await load_profile_menu_state(app, silent=False)
        del current_profile

        exact = _exact_profile_selection(options, query)
        if len(tokens) >= 2 and exact is None:
            if "agent_editor" not in app.connector_features:
                app._show_notice("This Agent Zero version does not expose profile editing.", error=True)
                return
            await _quick_create_profile(app, tokens[0], " ".join(tokens[1:]))
            return

        resolved, error_message = resolve_profile_selection(options, query)
        if resolved is None:
            app._show_notice(error_message or "Unknown agent profile.", error=True)
            return

        await apply_profile_selection(app, resolved["key"], options=options)
        return

    await app._open_profile_menu()


async def handle_profile_menu_action(
    app: AgentZeroCLI,
    action: str,
    profile_key: str = "",
) -> None:
    if action == "select":
        await apply_profile_selection(app, profile_key)
        return
    if action == "create":
        await _open_profile_editor(app, creating=True)
        return
    if action == "edit":
        await _open_profile_editor(app, creating=False)


def _exact_profile_selection(
    options: Sequence[Mapping[str, object]],
    query: str,
) -> ProfileOption | None:
    normalized = _normalize_profile_match(query.strip().strip("\"'"))
    for option in options:
        key = str(option.get("key") or "").strip()
        label = str(option.get("label") or key).strip() or key
        if normalized in {_normalize_profile_match(key), _normalize_profile_match(label)}:
            return {"key": key, "label": label}
    return None


def _policy_allows(policy: Mapping[str, object], tool_id: str) -> bool:
    if str(policy.get("mode") or "inherit") != "custom":
        return True
    blocked = {str(value) for value in policy.get("blocked") or []}
    allowed = {str(value) for value in policy.get("allowed") or []}
    if tool_id in blocked:
        return False
    if tool_id in allowed:
        return True
    return str(policy.get("default") or "allow") != "block"


def _tool_policy(tool_ids: Sequence[str], allowed_tools: Sequence[str]) -> dict[str, object]:
    all_ids = set(tool_ids)
    allowed = set(allowed_tools) & all_ids
    if len(allowed) * 2 >= len(all_ids):
        return {
            "mode": "custom",
            "default": "allow",
            "allowed": [],
            "blocked": sorted(all_ids - allowed),
        }
    return {
        "mode": "custom",
        "default": "block",
        "allowed": sorted(allowed),
        "blocked": [],
    }


def _editor_values(state: Mapping[str, object]) -> tuple[str, str, list[dict[str, object]], tuple[str, ...]]:
    profile = state.get("profile") if isinstance(state.get("profile"), Mapping) else {}
    metadata = profile.get("metadata") if isinstance(profile, Mapping) and isinstance(profile.get("metadata"), Mapping) else {}
    title_state = metadata.get("title") if isinstance(metadata, Mapping) and isinstance(metadata.get("title"), Mapping) else {}
    title = str(title_state.get("effective") or "")

    prompts = state.get("prompts") if isinstance(state.get("prompts"), list) else []
    instructions = next(
        (
            str(prompt.get("effective") or "")
            for prompt in prompts
            if isinstance(prompt, Mapping) and prompt.get("filename") == _SPECIFICS
        ),
        "",
    )
    tools_state = state.get("tools") if isinstance(state.get("tools"), Mapping) else {}
    catalog = [dict(item) for item in tools_state.get("catalog") or [] if isinstance(item, Mapping)]
    policy = tools_state.get("effective_policy") if isinstance(tools_state.get("effective_policy"), Mapping) else {}
    allowed = tuple(
        str(item.get("id") or "")
        for item in catalog
        if str(item.get("id") or "") and _policy_allows(policy, str(item.get("id") or ""))
    )
    return title, instructions, catalog, allowed


async def _load_editor_state(app: AgentZeroCLI, profile_id: str) -> Mapping[str, object] | None:
    try:
        payload = await app.client.agent_editor(
            "load",
            context_id=app.current_context or "",
            profile_id=profile_id,
        )
    except Exception as exc:
        app._show_notice(f"Failed to load profile editor: {exc}", error=True)
        return None
    if not payload.get("ok") or not isinstance(payload.get("state"), Mapping):
        app._show_notice(str(payload.get("message") or "Failed to load profile editor."), error=True)
        return None
    return payload["state"]


async def _open_profile_editor(app: AgentZeroCLI, *, creating: bool) -> None:
    if "agent_editor" not in app.connector_features:
        app._show_notice("This Agent Zero version does not expose profile editing.", error=True)
        return
    current_profile, _ = await load_profile_menu_state(app, silent=False)
    if not creating and not current_profile:
        return
    if not creating and current_profile == "default":
        app._show_notice("The Default utility profile cannot be edited.", error=True)
        return
    state = await _load_editor_state(app, "new-agent" if creating else current_profile)
    if state is None:
        return
    initial_title, initial_instructions, tools, initial_allowed = _editor_values(state)
    if creating:
        initial_title = ""
        initial_instructions = ""
    result = await app.push_screen_wait(
        ProfileEditorScreen(
            creating=creating,
            title=initial_title,
            instructions=initial_instructions,
            tools=tools,
            allowed_tools=initial_allowed,
        )
    )
    if not isinstance(result, ProfileEditorResult):
        return
    if creating:
        await _quick_create_profile(
            app,
            result.title,
            result.instructions,
            tool_ids=[str(item.get("id") or "") for item in tools],
            allowed_tools=result.allowed_tools,
            initial_allowed=initial_allowed,
        )
        return

    patch: dict[str, object] = {
        "profile_id": current_profile,
        "creating": False,
        "editor_mode": "easy",
    }
    if result.title != initial_title:
        patch["metadata"] = {"set": {"title": result.title}, "reset": []}
    if result.instructions != initial_instructions:
        patch["prompts"] = {"set": {_SPECIFICS: result.instructions}, "reset": []}
    tool_ids = [str(item.get("id") or "") for item in tools]
    if set(result.allowed_tools) != set(initial_allowed):
        patch["tool_policy"] = _tool_policy(tool_ids, result.allowed_tools)
    if len(patch) == 3:
        app._show_notice("No profile changes to save.")
        return
    try:
        payload = await app.client.agent_editor(
            "save",
            context_id=app.current_context or "",
            patch=patch,
        )
    except Exception as exc:
        app._show_notice(f"Failed to save profile: {exc}", error=True)
        return
    if not payload.get("ok"):
        app._show_notice(str(payload.get("message") or "Failed to save profile."), error=True)
        return
    app._show_notice(f"Saved {result.title}.")
    await app._refresh_model_switcher(silent=True)


async def _quick_create_profile(
    app: AgentZeroCLI,
    title: str,
    instructions: str,
    *,
    tool_ids: Sequence[str] = (),
    allowed_tools: Sequence[str] = (),
    initial_allowed: Sequence[str] = (),
) -> None:
    request: dict[str, object] = {
        "context_id": app.current_context or "",
        "title": title,
        "instructions": instructions,
    }
    if tool_ids and set(allowed_tools) != set(initial_allowed):
        request["tool_policy"] = _tool_policy(tool_ids, allowed_tools)
    try:
        payload = await app.client.agent_editor("quick_create", **request)
    except Exception as exc:
        app._show_notice(f"Failed to create profile: {exc}", error=True)
        return
    if not payload.get("ok"):
        app._show_notice(str(payload.get("message") or "Failed to create profile."), error=True)
        return
    profile_id = str(payload.get("profile_id") or "").strip()
    if not profile_id:
        app._show_notice("Profile created, but Agent Zero did not return its ID.", error=True)
        return
    try:
        context_id = await app.client.create_chat(
            current_context_id=app.current_context,
            project_name=project_name(app.current_project),
        )
    except Exception as exc:
        app._show_notice(f"Profile created, but the new chat could not be opened: {exc}", error=True)
        return
    try:
        activation = await app.client.set_agent_profile(context_id, profile_id)
    except Exception as exc:
        app._show_notice(f"Profile created, but it could not be activated: {exc}", error=True)
        return
    if not activation.get("ok"):
        message = str(activation.get("message") or "Agent Zero rejected the new profile.")
        app._show_notice(f"Profile created, but it could not be activated: {message}", error=True)
        return
    await app._switch_context(context_id, has_messages_hint=False)
    app._show_notice(f"Created and activated {title}.")
