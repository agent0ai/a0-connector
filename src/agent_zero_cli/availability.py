from __future__ import annotations

from typing import TYPE_CHECKING

from agent_zero_cli.commands import CommandAvailability

if TYPE_CHECKING:
    from agent_zero_cli.app import AgentZeroCLI


def require_connection(app: AgentZeroCLI) -> CommandAvailability:
    if not app.connected:
        return CommandAvailability(False, "Connect to an Agent Zero instance first.")
    return CommandAvailability(True)

def require_features(app: AgentZeroCLI, *features: str) -> CommandAvailability:
    base = app._require_connection()
    if not base.available:
        return base
    missing = [feature for feature in features if feature not in app.connector_features]
    if missing:
        joined = ", ".join(missing)
        return CommandAvailability(False, f"This connector build does not advertise: {joined}.")
    return CommandAvailability(True)


def compact_availability(app: AgentZeroCLI) -> CommandAvailability:
    base = app._require_features("compact_chat")
    if not base.available:
        return base
    if not app.current_context:
        return CommandAvailability(False, "Open or create a chat before compacting it.")
    if not app.current_context_has_messages:
        return CommandAvailability(False, "Start a conversation before compacting it.")
    if app.agent_active:
        return CommandAvailability(False, "Wait for the current run to finish before compacting.")
    return CommandAvailability(True)


def pause_availability(app: AgentZeroCLI) -> CommandAvailability:
    base = app._require_features("pause")
    if not base.available:
        return base
    if not app.current_context:
        return CommandAvailability(False, "Open or create a chat context first.")
    if not app.agent_active:
        return CommandAvailability(False, "Pause becomes available while the agent is running.")
    return CommandAvailability(True)


def resume_availability(app: AgentZeroCLI) -> CommandAvailability:
    base = app._require_features("pause")
    if not base.available:
        return base
    if not app.current_context:
        return CommandAvailability(False, "Open or create a chat context first.")
    if not app._pause_latched:
        return CommandAvailability(False, "Resume becomes available after pausing the active run.")
    return CommandAvailability(True)


def nudge_availability(app: AgentZeroCLI) -> CommandAvailability:
    base = app._require_features("nudge")
    if not base.available:
        return base
    if not app.current_context:
        return CommandAvailability(False, "Open or create a chat context first.")
    return CommandAvailability(True)


def project_availability(app: AgentZeroCLI) -> CommandAvailability:
    base = app._require_features("projects")
    if not base.available:
        return base
    if not app.current_context:
        return CommandAvailability(False, "Open or create a chat context first.")
    if app.agent_active:
        return CommandAvailability(False, "Wait for the current run to finish before changing projects.")
    return CommandAvailability(True)


def profile_availability(app: AgentZeroCLI) -> CommandAvailability:
    base = app._require_features("settings_get", "settings_set")
    if not base.available:
        return base
    if app.agent_active:
        return CommandAvailability(False, "Wait for the current run to finish before changing the agent profile.")
    return CommandAvailability(True)


def model_presets_availability(app: AgentZeroCLI) -> CommandAvailability:
    base = app._require_features("model_switcher", "model_presets")
    if not base.available:
        return base
    if not app.current_context:
        return CommandAvailability(False, "Open or create a chat context first.")
    if not app._model_switch_allowed:
        return CommandAvailability(False, "Model preset switching is unavailable for this chat.")
    return CommandAvailability(True)


def model_runtime_availability(app: AgentZeroCLI) -> CommandAvailability:
    base = app._require_features("model_switcher")
    if not base.available:
        return base
    if not app.current_context:
        return CommandAvailability(False, "Open or create a chat context first.")
    if not app._model_switch_allowed:
        return CommandAvailability(False, "Model runtime editing is unavailable for this chat.")
    return CommandAvailability(True)
