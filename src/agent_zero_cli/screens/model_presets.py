from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Center, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Select, Static

from agent_zero_cli.model_config import format_model_label


@dataclass(frozen=True)
class ModelPresetChoice:
    name: str
    label: str = ""
    description: str = ""
    main_model: str = ""
    utility_model: str = ""


@dataclass(frozen=True)
class ModelPresetsResult:
    preset_name: str | None


def _model_label(value: object) -> str:
    return format_model_label(value)


def _coerce_model_preset(value: object) -> ModelPresetChoice:
    if isinstance(value, ModelPresetChoice):
        return value
    if isinstance(value, str):
        clean = value.strip()
        return ModelPresetChoice(
            name=clean,
            label=clean or "Unnamed preset",
            main_model="Connector default",
            utility_model="Connector default",
        )
    if isinstance(value, Mapping):
        raw_name = str(value.get("name") or value.get("value") or "").strip()
        raw_label = str(value.get("label") or value.get("title") or raw_name).strip()
        raw_description = str(value.get("description") or value.get("summary") or "").strip()
        raw_main_model = (
            value.get("chat")
            or value.get("main")
            or value.get("main_model")
            or value.get("model")
        )
        raw_utility_model = value.get("utility") or value.get("utility_model")
        return ModelPresetChoice(
            name=raw_name,
            label=raw_label or raw_name or "Unnamed preset",
            description=raw_description,
            main_model=_model_label(raw_main_model),
            utility_model=_model_label(raw_utility_model),
        )
    clean = str(value).strip()
    return ModelPresetChoice(
        name=clean,
        label=clean or "Unnamed preset",
        main_model="Connector default",
        utility_model="Connector default",
    )


def _coerce_preset_list(
    presets: Sequence[ModelPresetChoice | Mapping[str, Any] | str] | None,
) -> tuple[ModelPresetChoice, ...]:
    items: list[ModelPresetChoice] = []
    seen: set[str] = set()
    for raw in presets or ():
        preset = _coerce_model_preset(raw)
        if not preset.name or preset.name in seen:
            continue
        seen.add(preset.name)
        items.append(preset)
    return tuple(items)


def _preset_options(presets: Sequence[ModelPresetChoice]) -> list[tuple[str, str]]:
    options: list[tuple[str, str]] = [("Default LLM", "")]
    for preset in presets:
        options.append((preset.label or preset.name, preset.name))
    return options


def _render_default_details() -> Text:
    return Text.from_markup(
        "\n".join(
            (
                "[bold]Default LLM[/bold]",
                "[dim]Main model:[/dim] Connector default",
                "[dim]Utility model:[/dim] Connector default",
            )
        )
    )


def _render_preset_details(preset: ModelPresetChoice) -> Text:
    lines = [
        f"[bold]{preset.label or preset.name}[/bold]",
        f"[dim]Main model:[/dim] {preset.main_model or 'Connector default'}",
        f"[dim]Utility model:[/dim] {preset.utility_model or 'Connector default'}",
    ]
    if preset.description:
        lines.append(f"[dim]Description:[/dim] {preset.description}")
    return Text.from_markup("\n".join(lines))


class ModelPresetsScreen(ModalScreen[ModelPresetsResult | None]):
    """Select a model preset with visible main/utility mapping details."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("enter", "apply", "Apply", show=True, priority=True),
        Binding("ctrl+s", "apply", "Apply", show=False),
    ]

    def __init__(
        self,
        presets: Sequence[ModelPresetChoice | Mapping[str, Any] | str] | None = None,
        *,
        current_preset: str = "",
        switch_allowed: bool = True,
        reason: str = "",
        current_override_label: str = "",
    ) -> None:
        super().__init__()
        self._presets = _coerce_preset_list(presets)
        self._preset_lookup = {preset.name: preset for preset in self._presets}
        self._current_preset = current_preset.strip()
        if self._current_preset not in self._preset_lookup:
            self._current_preset = ""
        self._selected_preset = self._current_preset
        self._switch_allowed = switch_allowed
        self._reason = reason
        self._current_override_label = current_override_label.strip()
        self._suppress_events = False

    def compose(self) -> ComposeResult:
        with Center():
            with Vertical(id="model-presets-box"):
                yield Static("Model Presets", id="model-presets-title")
                yield Static(
                    "Select a preset and inspect its Main/Utility model mapping before applying.",
                    id="model-presets-description",
                )
                yield Select(
                    _preset_options(self._presets),
                    prompt="Select preset",
                    allow_blank=False,
                    value=self._selected_preset,
                    id="model-presets-select",
                )
                yield Static("", id="model-presets-details")
                yield Static("", id="model-presets-status")
                with Horizontal(id="model-presets-actions"):
                    yield Button("Cancel", id="model-presets-cancel")
                    yield Button("Apply", id="model-presets-apply", variant="primary")

    def on_mount(self) -> None:
        self._sync_ui()
        self.query_one("#model-presets-select", Select).focus()

    def _sync_ui(self) -> None:
        self._suppress_events = True
        try:
            select = self.query_one("#model-presets-select", Select)
            select.set_options(_preset_options(self._presets))
            select.value = self._selected_preset
        finally:
            self._suppress_events = False
        self._sync_details()
        self._sync_status()
        self.query_one("#model-presets-apply", Button).disabled = not self._switch_allowed

    def _sync_details(self) -> None:
        details = self.query_one("#model-presets-details", Static)
        preset = self._preset_lookup.get(self._selected_preset)
        details.update(_render_preset_details(preset) if preset else _render_default_details())

    def _sync_status(self) -> None:
        status = self.query_one("#model-presets-status", Static)
        if not self._switch_allowed:
            status.update(Text.from_markup(f"[yellow]{self._reason or 'Preset switching is unavailable.'}[/yellow]"))
            return
        if self._current_override_label and not self._current_preset:
            status.update(
                Text.from_markup(
                    f"[dim]Current override:[/dim] {self._current_override_label}. "
                    "Apply [bold]Default LLM[/bold] to clear it."
                )
            )
            return
        if self._selected_preset == self._current_preset:
            status.update("[dim]Current preset selected.[/dim]")
            return
        status.update("")

    @property
    def current_result(self) -> ModelPresetsResult:
        return ModelPresetsResult(preset_name=self._selected_preset or None)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_apply(self) -> None:
        if not self._switch_allowed:
            return
        self.dismiss(self.current_result)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id == "model-presets-apply":
            self.action_apply()
        elif button_id == "model-presets-cancel":
            self.dismiss(None)

    def on_select_changed(self, event: Select.Changed) -> None:
        if self._suppress_events or event.select.id != "model-presets-select":
            return
        self._selected_preset = str(event.value)
        self._sync_details()
        self._sync_status()


__all__ = [
    "ModelPresetChoice",
    "ModelPresetsResult",
    "ModelPresetsScreen",
]
