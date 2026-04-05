from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Literal

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Center, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static, Switch, TabPane, TabbedContent


FieldKind = Literal["text", "password", "int", "bool"]


@dataclass(frozen=True)
class SettingFieldSpec:
    key: str
    label: str
    section: str
    kind: FieldKind
    placeholder: str = ""
    help_text: str = ""
    password: bool = False


@dataclass(frozen=True)
class SettingsResult:
    settings: dict[str, Any]
    changed_keys: tuple[str, ...]


_SECTIONS: tuple[tuple[str, str], ...] = (
    ("agent", "Agent"),
    ("workdir", "Workdir"),
    ("authentication", "Authentication"),
    ("runtime", "Runtime"),
)

_FIELD_SPECS: tuple[SettingFieldSpec, ...] = (
    SettingFieldSpec("agent_profile", "Agent profile", "agent", "text"),
    SettingFieldSpec(
        "agent_knowledge_subdir",
        "Agent knowledge subdir",
        "agent",
        "text",
    ),
    SettingFieldSpec(
        "chat_inherit_project",
        "Inherit project into new chats",
        "agent",
        "bool",
    ),
    SettingFieldSpec("workdir_path", "Workdir path", "workdir", "text"),
    SettingFieldSpec("workdir_show", "Show workdir in UI", "workdir", "bool"),
    SettingFieldSpec("workdir_max_depth", "Workdir max depth", "workdir", "int"),
    SettingFieldSpec("workdir_max_files", "Workdir max files", "workdir", "int"),
    SettingFieldSpec("workdir_max_folders", "Workdir max folders", "workdir", "int"),
    SettingFieldSpec("workdir_max_lines", "Workdir max lines", "workdir", "int"),
    SettingFieldSpec("auth_login", "Login username", "authentication", "text"),
    SettingFieldSpec(
        "auth_password",
        "Login password",
        "authentication",
        "password",
        password=True,
    ),
    SettingFieldSpec(
        "update_check_enabled",
        "Check for updates",
        "runtime",
        "bool",
    ),
    SettingFieldSpec(
        "websocket_server_restart_enabled",
        "Allow websocket server restart",
        "runtime",
        "bool",
    ),
    SettingFieldSpec(
        "uvicorn_access_logs_enabled",
        "Enable Uvicorn access logs",
        "runtime",
        "bool",
    ),
    SettingFieldSpec(
        "mcp_server_enabled",
        "Enable MCP server",
        "runtime",
        "bool",
    ),
    SettingFieldSpec(
        "mcp_client_init_timeout",
        "MCP client init timeout",
        "runtime",
        "int",
    ),
    SettingFieldSpec(
        "mcp_client_tool_timeout",
        "MCP client tool timeout",
        "runtime",
        "int",
    ),
    SettingFieldSpec(
        "a2a_server_enabled",
        "Enable A2A server",
        "runtime",
        "bool",
    ),
)


def _fields_for_section(section_key: str) -> tuple[SettingFieldSpec, ...]:
    return tuple(field for field in _FIELD_SPECS if field.section == section_key)


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off", ""}:
            return False
    return bool(value)


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _as_int_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    text = str(value).strip()
    if not text:
        return ""
    try:
        return str(int(text))
    except ValueError:
        return text


class SettingsScreen(ModalScreen[SettingsResult | None]):
    """Curated connector-backed settings editor."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+s", "save", "Save", show=True, priority=True),
    ]

    def __init__(self, settings: Mapping[str, Any] | None = None) -> None:
        super().__init__()
        self._original_settings: dict[str, Any] = dict(settings or {})
        self._widgets: dict[str, Input | Switch] = {}
        self._status = ""

    def compose(self) -> ComposeResult:
        with Center():
            with Vertical(id="settings-box"):
                yield Static("Connector Settings", id="settings-title")
                yield Static(
                    "Only the curated connector-backed fields are shown here.",
                    id="settings-description",
                )
                with TabbedContent(initial="agent", id="settings-tabs"):
                    for section_key, section_title in _SECTIONS:
                        with TabPane(
                            section_title,
                            name=section_key,
                            id=f"settings-tab-{section_key}",
                        ):
                            with VerticalScroll(id=f"settings-panel-{section_key}"):
                                for field in _fields_for_section(section_key):
                                    yield from self._compose_field(field)
                yield Static("", id="settings-status")
                with Horizontal(id="settings-actions"):
                    yield Button("Cancel", id="settings-cancel")
                    yield Button("Save", id="settings-save", variant="primary")

    def on_mount(self) -> None:
        self._focus_first_control()
        self._sync_status()

    def _compose_field(self, field: SettingFieldSpec) -> ComposeResult:
        with Vertical(id=f"settings-field-{field.key}"):
            yield Static(field.label, id=f"settings-label-{field.key}")
            if field.kind == "bool":
                control = Switch(
                    value=_as_bool(self._original_settings.get(field.key, False)),
                    id=f"setting-{field.key}",
                )
            else:
                control = Input(
                    value=(
                        _as_int_text(self._original_settings.get(field.key))
                        if field.kind == "int"
                        else _as_text(self._original_settings.get(field.key))
                    ),
                    placeholder=field.placeholder or field.label,
                    password=field.password,
                    restrict="0123456789" if field.kind == "int" else None,
                    id=f"setting-{field.key}",
                )
            self._widgets[field.key] = control
            yield control
            if field.help_text:
                yield Static(field.help_text, id=f"settings-help-{field.key}")

    def _focus_first_control(self) -> None:
        first = next(iter(self._widgets.values()), None)
        if first is not None:
            first.focus()

    def _sync_status(self, message: str = "") -> None:
        text = message or self._status
        self.query_one("#settings-status", Static).update(Text.from_markup(text) if text else "")

    def _read_value(self, field: SettingFieldSpec) -> Any:
        widget = self._widgets[field.key]
        if field.kind == "bool":
            assert isinstance(widget, Switch)
            return widget.value
        assert isinstance(widget, Input)
        text = widget.value
        if field.kind == "int":
            stripped = text.strip()
            if not stripped:
                raise ValueError(f"{field.label} must be an integer")
            try:
                return int(stripped)
            except ValueError as exc:
                raise ValueError(f"{field.label} must be an integer") from exc
        return text if field.password else text.strip()

    def _collect_values(self) -> tuple[dict[str, Any], tuple[str, ...]]:
        values: dict[str, Any] = {}
        changed_keys: list[str] = []

        for field in _FIELD_SPECS:
            value = self._read_value(field)
            values[field.key] = value
            if value != self._original_settings.get(field.key):
                changed_keys.append(field.key)

        return values, tuple(changed_keys)

    def _save(self) -> None:
        try:
            values, changed_keys = self._collect_values()
        except ValueError as exc:
            self._status = f"[red]{exc}[/red]"
            self._sync_status()
            return

        self.dismiss(SettingsResult(settings=values, changed_keys=changed_keys))

    def action_save(self) -> None:
        self._save()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id == "settings-save":
            self._save()
        elif button_id == "settings-cancel":
            self.dismiss(None)

    def set_settings(self, settings: Mapping[str, Any]) -> None:
        """Replace the current snapshot and refresh the visible controls."""

        self._original_settings = dict(settings)
        for field in _FIELD_SPECS:
            widget = self._widgets.get(field.key)
            if widget is None:
                continue
            if field.kind == "bool":
                assert isinstance(widget, Switch)
                widget.value = _as_bool(settings.get(field.key, False))
            elif field.kind == "int":
                assert isinstance(widget, Input)
                widget.value = _as_int_text(settings.get(field.key))
            else:
                assert isinstance(widget, Input)
                widget.value = (
                    _as_text(settings.get(field.key))
                    if field.password
                    else _as_text(settings.get(field.key)).strip()
                )

    def current_settings(self) -> dict[str, Any]:
        """Return the full curated settings snapshot as currently edited."""

        values, _ = self._collect_values()
        return values


__all__ = [
    "SettingFieldSpec",
    "SettingsResult",
    "SettingsScreen",
]
