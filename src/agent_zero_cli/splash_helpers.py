from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any, Mapping

from rich.text import Text
from textual.widgets import ContentSwitcher

from agent_zero_cli.client import DEFAULT_HOST
from agent_zero_cli.widgets import ChatInput, ComputerUseBanner, DynamicFooter, SplashAction, SplashView
from agent_zero_cli.widgets.chat_log import ChatLog

if TYPE_CHECKING:
    from agent_zero_cli.app import AgentZeroCLI


async def refresh_workspace_from_settings(app: AgentZeroCLI) -> None:
    if "settings_get" not in app.connector_features:
        app._set_workspace_context(remote_workspace="")
        return
    try:
        payload = await app.client.get_settings()
    except Exception:
        return
    settings = payload.get("settings", payload) if isinstance(payload, Mapping) else {}
    remote_workspace = ""
    if isinstance(settings, Mapping):
        remote_workspace = str(settings.get("workdir_path") or "").strip()
    app._set_workspace_context(remote_workspace=remote_workspace)


def splash_host(app: AgentZeroCLI) -> str:
    return app._splash_state.host or app.config.instance_url or DEFAULT_HOST


def normalize_host(host: str) -> str:
    return host.strip() or DEFAULT_HOST


def set_splash_state(app: AgentZeroCLI, **changes: Any) -> None:
    app._splash_state = replace(app._splash_state, **changes)
    try:
        app.query_one("#splash-view", SplashView).set_state(app._splash_state)
    except Exception:
        pass
    app._sync_composer_visibility()


def sync_workspace_widgets(app: AgentZeroCLI) -> None:
    try:
        app.query_one("#chat-log", ChatLog).set_workspace(
            local_workspace=app._local_workspace,
            remote_workspace=app._remote_workspace,
        )
    except Exception:
        pass

    app._set_splash_state(
        local_workspace=app._local_workspace,
        remote_workspace=app._remote_workspace,
    )


def set_workspace_context(
    app: AgentZeroCLI,
    *,
    local_workspace: str | None = None,
    remote_workspace: str | None = None,
) -> None:
    if local_workspace is not None:
        app._local_workspace = local_workspace.strip()
    if remote_workspace is not None:
        app._remote_workspace = remote_workspace.strip()
    app._sync_workspace_widgets()


def set_splash_stage(
    app: AgentZeroCLI,
    stage: str,
    *,
    message: str = "",
    detail: str = "",
    host: str | None = None,
    username: str | None = None,
    password: str | None = None,
    remember_host: bool | None = None,
    login_error: str | None = None,
    actions: tuple[SplashAction, ...] | None = None,
) -> None:
    updates: dict[str, Any] = {
        "stage": stage,
        "message": message,
        "detail": detail,
    }
    if host is not None:
        updates["host"] = host
    if username is not None:
        updates["username"] = username
    if password is not None:
        updates["password"] = password
    if remember_host is not None:
        updates["remember_host"] = remember_host
    if login_error is not None:
        updates["login_error"] = login_error
    if actions is not None:
        updates["actions"] = actions
    app._set_splash_state(**updates)


def sync_ready_actions(app: AgentZeroCLI) -> None:
    if app._splash_state.stage != "ready":
        return
    app._set_splash_state(actions=app._welcome_actions())


def sync_body_mode(app: AgentZeroCLI) -> None:
    body = app.query_one("#body-switcher", ContentSwitcher)
    if app.connected and app.current_context_has_messages:
        body.current = "chat-log"
    else:
        body.current = "splash-view"
        app._sync_ready_actions()
    app._sync_composer_visibility()


def sync_composer_visibility(app: AgentZeroCLI) -> None:
    show_composer = app.connected and (app.current_context_has_messages or app._splash_state.stage == "ready")
    try:
        input_widget = app.query_one("#message-input", ChatInput)
        input_widget.display = show_composer
        if not show_composer:
            input_widget.disabled = True
            input_widget.set_idle()
    except Exception:
        pass

    try:
        banner = app.query_one("#computer-use-banner", ComputerUseBanner)
        if not show_composer:
            banner.display = False
        else:
            app._sync_computer_use_status()
    except Exception:
        pass

    try:
        footer = app.query_one(DynamicFooter)
        footer.display = show_composer
    except Exception:
        pass


def focus_splash_primary(app: AgentZeroCLI) -> None:
    callback = lambda: app.query_one("#splash-view", SplashView).focus_primary()
    if app.is_running:
        app.call_after_refresh(callback)
    else:
        callback()


def focus_message_input(app: AgentZeroCLI) -> None:
    callback = lambda: app.query_one("#message-input", ChatInput).focus()
    if app.is_running:
        app.call_after_refresh(callback)
    else:
        callback()


def show_notice(app: AgentZeroCLI, message: str, *, error: bool = False) -> None:
    if app.connected and not app.current_context_has_messages:
        splash_message = app._splash_state.message
        if app._splash_state.stage == "ready":
            splash_message = message if error else "Ready when you are."
        app._set_splash_state(
            message=splash_message,
            detail=message,
            actions=app._welcome_actions() if app._splash_state.stage == "ready" else app._splash_state.actions,
        )
        return

    log = app.query_one("#chat-log", ChatLog)
    log.write(Text(message, style="red") if error else message)


def available_help_lines(app: AgentZeroCLI) -> tuple[list[str], list[str]]:
    available: list[str] = []
    unavailable: list[str] = []
    for spec, availability in app._iter_ui_commands():
        line = f"{app._command_display(spec)} - {spec.description}"
        if availability.available:
            available.append(line)
        else:
            reason = availability.reason or "Unavailable right now."
            unavailable.append(f"{line} [{reason}]")
    return available, unavailable


def surface_help(app: AgentZeroCLI) -> None:
    available, unavailable = app._available_help_lines()
    if app.connected and not app.current_context_has_messages:
        lines = ["Available commands:"]
        lines.extend(f"- {line}" for line in available)
        if unavailable:
            lines.append("")
            lines.append("Unavailable right now:")
            lines.extend(f"- {line}" for line in unavailable)
        app._set_splash_state(
            message="Available commands",
            detail="\n".join(lines),
            actions=app._welcome_actions(),
        )
        return

    log = app.query_one("#chat-log", ChatLog)
    log.write(Text("Available commands:", style="bold"))
    for line in available:
        log.write(line)
    if unavailable:
        log.write(Text("Unavailable right now:", style="dim"))
        for line in unavailable:
            log.write(line)


def welcome_actions(app: AgentZeroCLI) -> tuple[SplashAction, ...]:
    hidden = set(getattr(app, "_splash_hidden_commands", ()))
    return tuple(
        SplashAction(
            key=spec.canonical_name,
            title=spec.canonical_name,
            description=spec.description,
            enabled=availability.available,
            disabled_reason="" if availability.available else (availability.reason or ""),
        )
        for spec, availability in app._iter_ui_commands()
        if spec.canonical_name not in hidden
    )
