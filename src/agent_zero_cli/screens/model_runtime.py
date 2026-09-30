from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Checkbox, Input, Select, Static

_DEFAULT_PROVIDER_OPTIONS: tuple[tuple[str, str], ...] = (
    ("Anthropic", "anthropic"),
    ("Openai", "openai"),
)

from agent_zero_cli.model_config import coerce_model_config, format_model_label, format_provider_label
from agent_zero_cli.text_utils import coerce_bool, strip_text


@dataclass(frozen=True)
class ModelRuntimeResult:
    main_model: dict[str, str]
    utility_model: dict[str, str]
    main_changed: bool = True
    utility_changed: bool = True
    main_vision: bool | None = None
    vision_model: dict[str, Any] | None = None


class ModelRuntimeScreen(Screen[ModelRuntimeResult | None]):
    """Edit Main, optional Vision sidecar, and Utility models in the active preset."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("enter", "apply", "Apply", show=True, priority=True),
        Binding("ctrl+s", "apply", "Apply", show=False),
    ]

    def __init__(
        self,
        *,
        preset_name: str = "Default",
        main_model: Mapping[str, Any] | None = None,
        utility_model: Mapping[str, Any] | None = None,
        supports_vision: bool = True,
        vision_model: Mapping[str, Any] | None = None,
        focus_target: str = "main",
        provider_options: Sequence[tuple[str, str]] | None = None,
    ) -> None:
        super().__init__()
        self._preset_name = preset_name
        self._main_model = coerce_model_config(main_model)
        self._utility_model = coerce_model_config(utility_model)
        self._main_model.pop("api_key", None)
        self._utility_model.pop("api_key", None)
        self._supports_vision = supports_vision
        self._vision_model = coerce_model_config(vision_model)
        self._vision_model.pop("api_key", None)
        self._override_main = coerce_bool((vision_model or {}).get("override_main"))
        self._focus_target = "utility" if focus_target == "utility" else "main"
        self._main_label = format_model_label(main_model)
        self._utility_label = format_model_label(utility_model)
        self._provider_options = self._normalize_provider_options(
            provider_options,
            main_model=self._main_model,
            utility_model=self._utility_model,
        )
    def _normalize_provider_options(
        self,
        options: Sequence[tuple[str, str]] | None,
        *,
        main_model: Mapping[str, Any],
        utility_model: Mapping[str, Any],
    ) -> tuple[tuple[str, str], ...]:
        ordered: list[tuple[str, str]] = []
        seen: set[str] = set()

        def _add(provider: Any, label: Any = "") -> None:
            value = strip_text(provider).lower()
            if not value:
                return
            if value in seen:
                return
            seen.add(value)
            label_text = strip_text(label) or format_provider_label(value)
            ordered.append((label_text, value))

        for entry in options or ():
            if not isinstance(entry, tuple) or len(entry) < 2:
                continue
            _add(entry[1], entry[0])
        _add(main_model.get("provider"))
        _add(utility_model.get("provider"))
        _add(self._vision_model.get("provider"))

        if not ordered:
            ordered.extend(_DEFAULT_PROVIDER_OPTIONS)

        return tuple(ordered)

    def _provider_field_value(self, values: Mapping[str, Any]) -> str | object:
        provider = strip_text(values.get("provider"))
        return provider if provider else Select.NULL

    def compose(self) -> ComposeResult:
        with Vertical(id="model-runtime-box"):
            yield Static(f"Edit preset: {self._preset_name}", id="model-runtime-title", markup=False)
            yield Static(
                "Apply saves this preset for all chats using it. Configure API keys in Agent Zero.",
                id="model-runtime-description",
            )
            with VerticalScroll(id="model-runtime-fields"):
                yield from self._compose_section(
                    "main", "Main Model", self._main_label, self._main_model,
                )
                yield Checkbox("Supports Vision", self._supports_vision, id="model-runtime-supports-vision")
                yield Checkbox("Use separate Vision Model", self._override_main, id="model-runtime-separate-vision")
                yield from self._compose_section(
                    "vision", "Vision sidecar",
                    format_model_label(self._vision_model, default="Not configured"),
                    self._vision_model,
                )
                yield from self._compose_section(
                    "utility", "Utility Model", self._utility_label, self._utility_model,
                )
            yield Static("", id="model-runtime-status")
            with Horizontal(id="model-runtime-actions"):
                yield Button("Cancel", id="model-runtime-cancel")
                yield Button("Apply", id="model-runtime-apply", variant="primary")

    def _compose_section(
        self,
        key: str,
        title: str,
        current_label: str,
        values: Mapping[str, Any],
    ) -> ComposeResult:
        with Vertical(id=f"model-runtime-{key}-section", classes="model-runtime-section"):
            yield Static(title, classes="model-runtime-section-title")
            yield Static(f"Current: {current_label}", classes="model-runtime-current")
            if key == "vision":
                yield Static("", id="model-runtime-vision-description")
            yield Static("Provider", classes="model-runtime-label")
            yield Select(
                list(self._provider_options),
                prompt="Select provider",
                allow_blank=True,
                value=self._provider_field_value(values),
                id=f"model-runtime-{key}-provider",
            )
            yield Static("Model", classes="model-runtime-label")
            yield Input(
                value=strip_text(values.get("name")),
                placeholder="Example: claude-sonnet-4 or gpt-4o",
                id=f"model-runtime-{key}-name",
            )
            yield Static("Base URL", classes="model-runtime-label")
            yield Input(
                value=strip_text(values.get("api_base")),
                placeholder="Optional: custom provider base URL",
                id=f"model-runtime-{key}-base-url",
            )

    def on_mount(self) -> None:
        self._sync_vision_fields()
        target = "#model-runtime-main-provider"
        if self._focus_target == "utility":
            target = "#model-runtime-utility-provider"
        self.query_one(target, Select).focus()

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        if event.checkbox.id in {"model-runtime-supports-vision", "model-runtime-separate-vision"}:
            self._sync_vision_fields()

    def _sync_vision_fields(self) -> None:
        supports_vision = self.query_one("#model-runtime-supports-vision", Checkbox).value
        separate = self.query_one("#model-runtime-separate-vision", Checkbox)
        separate.display = supports_vision
        self.query_one("#model-runtime-vision-section").display = not supports_vision or separate.value
        self.query_one("#model-runtime-vision-description", Static).update(
            "Overrides Main model vision for vision_load."
            if supports_vision
            else "Used by vision_load while Main vision is disabled. Leave empty to disable image analysis."
        )

    def action_cancel(self) -> None:
        self.dismiss(None)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "apply" and any(select.expanded for select in self.query(Select)):
            return False
        return super().check_action(action, parameters)

    def action_apply(self) -> None:
        self._apply()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id == "model-runtime-apply":
            self._apply()
            return
        if button_id == "model-runtime-cancel":
            self.dismiss(None)

    def _collect_model(self, key: str) -> dict[str, str]:
        provider_value = self.query_one(f"#model-runtime-{key}-provider", Select).value
        provider = strip_text(provider_value) if isinstance(provider_value, str) else ""
        name = strip_text(self.query_one(f"#model-runtime-{key}-name", Input).value)
        base_url = strip_text(self.query_one(f"#model-runtime-{key}-base-url", Input).value)
        payload: dict[str, str] = {}
        if provider:
            payload["provider"] = provider
        if name:
            payload["name"] = name
        if base_url:
            payload["api_base"] = base_url
        return payload

    def _apply(self) -> None:
        status = self.query_one("#model-runtime-status", Static)
        main_model = self._collect_model("main")
        utility_model = self._collect_model("utility")
        supports_vision = self.query_one("#model-runtime-supports-vision", Checkbox).value
        override_main = self.query_one("#model-runtime-separate-vision", Checkbox).value
        vision_model = self._collect_model("vision")
        vision_changed = vision_model != self._vision_model or override_main != self._override_main

        if vision_changed and vision_model and not (vision_model.get("provider") and vision_model.get("name")):
            status.update(Text("Set both the Vision sidecar provider and model, or clear all its fields.", style="#ff8b6b"))
            return
        if supports_vision and override_main and not vision_model:
            status.update(Text("Set a Vision sidecar or turn off Use separate Vision Model.", style="#ff8b6b"))
            return

        if not main_model and not utility_model:
            status.update(Text("Set at least one model target before applying.", style="#ff8b6b"))
            return

        if main_model:
            if not main_model.get("provider"):
                provider = strip_text(self._main_model.get("provider"))
                if provider:
                    main_model["provider"] = provider
            if not main_model.get("name"):
                name = strip_text(self._main_model.get("name"))
                if name:
                    main_model["name"] = name

        if utility_model:
            if not utility_model.get("provider"):
                provider = strip_text(self._utility_model.get("provider"))
                if provider:
                    utility_model["provider"] = provider
            if not utility_model.get("name"):
                name = strip_text(self._utility_model.get("name"))
                if name:
                    utility_model["name"] = name

        if main_model and not main_model.get("name"):
            status.update(Text("Main model name is required.", style="#ff8b6b"))
            return
        if utility_model and not utility_model.get("name"):
            status.update(Text("Utility model name is required.", style="#ff8b6b"))
            return

        main_changed = main_model != self._main_model
        utility_changed = utility_model != self._utility_model
        main_vision = supports_vision if supports_vision != self._supports_vision else None
        if not main_changed and not utility_changed and main_vision is None and not vision_changed:
            status.update(Text("No model changes to apply.", style="dim"))
            return

        self.dismiss(
            ModelRuntimeResult(
                main_model=main_model,
                utility_model=utility_model,
                main_changed=main_changed,
                utility_changed=utility_changed,
                main_vision=main_vision,
                vision_model=(
                    {**vision_model, "vision": True, "override_main": override_main}
                    if vision_model else {}
                ) if vision_changed else None,
            )
        )


__all__ = [
    "ModelRuntimeResult",
    "ModelRuntimeScreen",
]
