from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.message import Message
from textual.widgets import Button, Select

from agent_zero_cli.model_config import format_model_label

_CUSTOM_OVERRIDE_VALUE = "__custom_override__"
_PRESET_MIN_VISIBLE_WIDTH = 82


def _show_preset_for_width(width: int) -> bool:
    return max(width, 0) >= _PRESET_MIN_VISIBLE_WIDTH


def _should_stack_summary_rows(width: int, *, main_model_text: str, utility_model_text: str) -> bool:
    """Return True when main + utility don't fit on one row."""
    available = max(width, 0)
    if available == 0:
        return True
    main_segment = len("Main ") + len(main_model_text)
    utility_segment = len("Utility ") + len(utility_model_text)
    return (main_segment + 3 + utility_segment) > available


@dataclass(frozen=True)
class ModelIdentity:
    provider: str = ""
    name: str = ""
    label: str = ""


@dataclass(frozen=True)
class ModelPreset:
    name: str
    label: str = ""
    description: str = ""


def _coerce_model_identity(value: object) -> ModelIdentity:
    if isinstance(value, ModelIdentity):
        return value
    if isinstance(value, Mapping):
        return ModelIdentity(
            provider=str(value.get("provider") or "").strip(),
            name=str(value.get("name") or "").strip(),
            label=str(value.get("label") or "").strip(),
        )
    return ModelIdentity()


def _coerce_model_preset(value: object) -> ModelPreset:
    if isinstance(value, ModelPreset):
        return value
    if isinstance(value, str):
        clean = value.strip()
        return ModelPreset(name=clean, label=clean or "Unnamed preset")
    if isinstance(value, Mapping):
        raw_name = str(value.get("name") or value.get("value") or "").strip()
        raw_label = str(value.get("label") or value.get("title") or raw_name).strip()
        raw_description = str(value.get("description") or value.get("summary") or "").strip()
        return ModelPreset(
            name=raw_name,
            label=raw_label or raw_name or "Unnamed preset",
            description=raw_description,
        )
    clean = str(value).strip()
    return ModelPreset(name=clean, label=clean or "Unnamed preset")


def _model_text(model: object) -> str:
    identity = _coerce_model_identity(model)
    if identity.label:
        return identity.label
    return format_model_label(
        {
            "provider": identity.provider,
            "name": identity.name,
        },
        default="—",
    )


def _preset_options(
    presets: Sequence[ModelPreset | Mapping[str, object] | str] | None,
    *,
    override_label: str = "",
) -> list[tuple[str, str]]:
    options: list[tuple[str, str]] = [("Default LLM", "")]
    if override_label:
        options.append((override_label, _CUSTOM_OVERRIDE_VALUE))
    for raw in presets or []:
        preset = _coerce_model_preset(raw)
        if not preset.name:
            continue
        label = preset.label or preset.name
        if preset.description:
            label = f"{label} - {preset.description}"
        options.append((label, preset.name))
    return options


class ModelSwitcherBar(Horizontal):
    """Compact model summary + preset selector above the composer."""

    DEFAULT_CSS = """
    ModelSwitcherBar {
        layout: horizontal;
        align: left middle;
    }
    """

    class PresetChanged(Message):
        def __init__(self, value: str, bar: ModelSwitcherBar) -> None:
            super().__init__()
            self.value = value
            self.bar = bar

    class ModelConfigRequested(Message):
        def __init__(self, target: str, bar: ModelSwitcherBar) -> None:
            super().__init__()
            self.target = target
            self.bar = bar

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self._summary = Horizontal(id="model-switcher-summary")
        self._main_button = Button("", id="model-switcher-main", classes="model-switcher-chip")
        self._utility_button = Button("", id="model-switcher-utility", classes="model-switcher-chip")
        self._preset = Select(
            [("Default LLM", "")],
            prompt="Preset",
            allow_blank=False,
            value="",
            compact=True,
            id="model-switcher-preset",
        )
        self._busy = False
        self._switch_allowed = False
        self._option_count = 1
        self._suppress_events = False
        self._selected_value = ""
        self._main_model_text = "—"
        self._utility_model_text = "—"
        self.display = False

    def compose(self) -> ComposeResult:
        with self._summary:
            yield self._main_button
            yield self._utility_button
        yield self._preset

    def on_mount(self) -> None:
        self.call_after_refresh(self._sync_layout)

    def on_resize(self, event: events.Resize) -> None:
        self._sync_layout()

    def clear(self) -> None:
        self.display = False
        self._busy = False
        self._switch_allowed = False
        self._option_count = 1
        self._selected_value = ""
        self._suppress_events = True
        try:
            self._main_model_text = "—"
            self._utility_model_text = "—"
            self._main_button.label = "Main —"
            self._utility_button.label = "Utility —"
            self._preset.set_options([("Default LLM", "")])
            self._preset.value = ""
        finally:
            self._suppress_events = False
        self._update_select_state()

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._update_select_state()

    def set_state(
        self,
        *,
        main_model: object,
        utility_model: object,
        presets: Sequence[ModelPreset | Mapping[str, object] | str] | None,
        allowed: bool,
        selected_preset: str = "",
        override_label: str = "",
    ) -> None:
        self.display = True
        self._main_model_text = _model_text(main_model)
        self._utility_model_text = _model_text(utility_model)
        self._main_button.label = f"Main {self._main_model_text}"
        self._utility_button.label = f"Utility {self._utility_model_text}"

        options = _preset_options(presets, override_label=override_label)
        option_values = {value for _, value in options}
        current_value = (
            selected_preset
            if selected_preset and selected_preset in option_values
            else (_CUSTOM_OVERRIDE_VALUE if override_label else "")
        )

        self._suppress_events = True
        try:
            self._preset.set_options(options)
            self._preset.value = current_value
            self._selected_value = current_value
        finally:
            self._suppress_events = False

        self._switch_allowed = allowed
        self._option_count = len(options)
        self._sync_layout()
        self._update_select_state()

    def _sync_layout(self) -> None:
        self._preset.display = _show_preset_for_width(self.size.width)
        available_width = max(self.size.width, 0)
        if self._preset.display:
            # Preserve space for the selector so model labels can adapt cleanly.
            available_width = max(available_width - 34, 0)

        stack_rows = _should_stack_summary_rows(
            available_width,
            main_model_text=self._main_model_text,
            utility_model_text=self._utility_model_text,
        )
        if stack_rows:
            self._summary.add_class("stacked")
        else:
            self._summary.remove_class("stacked")

    def _update_select_state(self) -> None:
        self._preset.disabled = self._busy or not self._switch_allowed or self._option_count <= 1

    def _request_model_config(self, target: str) -> None:
        if target not in {"main", "utility"}:
            return
        if self._busy:
            return
        self.post_message(self.ModelConfigRequested(target=target, bar=self))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id == "model-switcher-main":
            self._request_model_config("main")
            return
        if button_id == "model-switcher-utility":
            self._request_model_config("utility")
            return

    def on_select_changed(self, event: Select.Changed) -> None:
        if self._suppress_events or event.select.id != "model-switcher-preset":
            return
        if self._busy:
            return
        value = str(event.value)
        if value == _CUSTOM_OVERRIDE_VALUE:
            return
        if value == self._selected_value:
            return
        self._selected_value = value
        self.post_message(self.PresetChanged(value=value, bar=self))


__all__ = [
    "ModelIdentity",
    "ModelPreset",
    "ModelSwitcherBar",
]
