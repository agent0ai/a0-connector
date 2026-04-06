from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.message import Message
from textual.widgets import Select, Static

_CUSTOM_OVERRIDE_VALUE = "__custom_override__"


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
    if identity.provider and identity.name:
        return f"{identity.provider}/{identity.name}"
    return identity.name or identity.provider or "—"


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

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self._summary = Static("", id="model-switcher-summary")
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
        self.display = False

    def compose(self) -> ComposeResult:
        yield self._summary
        yield self._preset

    def clear(self) -> None:
        self.display = False
        self._busy = False
        self._switch_allowed = False
        self._option_count = 1
        self._suppress_events = True
        try:
            self._summary.update("")
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
        self._summary.update(self._render_summary(main_model, utility_model))

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
        finally:
            self._suppress_events = False

        self._switch_allowed = allowed
        self._option_count = len(options)
        self._update_select_state()

    def _render_summary(self, main_model: object, utility_model: object) -> Text:
        return Text.assemble(
            ("Main ", "dim"),
            (_model_text(main_model), "#d9e2ec"),
            ("   Utility ", "dim"),
            (_model_text(utility_model), "#d9e2ec"),
        )

    def _update_select_state(self) -> None:
        self._preset.disabled = self._busy or not self._switch_allowed or self._option_count <= 1

    def on_select_changed(self, event: Select.Changed) -> None:
        if self._suppress_events or event.select.id != "model-switcher-preset":
            return
        value = str(event.value)
        if value == _CUSTOM_OVERRIDE_VALUE:
            return
        self.post_message(self.PresetChanged(value=value, bar=self))


__all__ = [
    "ModelIdentity",
    "ModelPreset",
    "ModelSwitcherBar",
]
