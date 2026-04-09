from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Button, Input, Select, Static

_DEFAULT_PROVIDER_OPTIONS: tuple[tuple[str, str], ...] = (
    ("Anthropic", "anthropic"),
    ("Openai", "openai"),
)

from agent_zero_cli.model_config import coerce_model_config


@dataclass(frozen=True)
class ModelRuntimeResult:
    main_model: dict[str, str]
    utility_model: dict[str, str]


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _model_label(value: Mapping[str, Any] | None) -> str:
    payload = coerce_model_config(value)
    provider = payload.get("provider", "")
    name = payload.get("name", "")
    if provider and name:
        return f"{provider}/{name}"
    if name:
        return name
    if provider:
        return provider
    return "Connector default"


def _provider_label(provider: str) -> str:
    return provider.replace("_", " ").title()


class ModelRuntimeScreen(Screen[ModelRuntimeResult | None]):
    """Edit Main/Utility runtime model configuration for this chat."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("enter", "apply", "Apply", show=True, priority=True),
        Binding("ctrl+s", "apply", "Apply", show=False),
    ]

    def __init__(
        self,
        *,
        main_model: Mapping[str, Any] | None = None,
        utility_model: Mapping[str, Any] | None = None,
        focus_target: str = "main",
        provider_options: Sequence[tuple[str, str]] | None = None,
        provider_api_key_status: Mapping[str, object] | None = None,
        main_has_api_key: bool = False,
        utility_has_api_key: bool = False,
    ) -> None:
        super().__init__()
        self._main_model = coerce_model_config(main_model)
        self._utility_model = coerce_model_config(utility_model)
        self._main_has_api_key = bool(main_has_api_key)
        self._utility_has_api_key = bool(utility_has_api_key)
        self._focus_target = "utility" if focus_target == "utility" else "main"
        self._main_label = _model_label(main_model)
        self._utility_label = _model_label(utility_model)
        self._provider_options = self._normalize_provider_options(
            provider_options,
            main_model=self._main_model,
            utility_model=self._utility_model,
        )
        self._provider_api_key_status = {
            str(provider).strip().lower(): bool(has_key)
            for provider, has_key in dict(provider_api_key_status or {}).items()
            if str(provider).strip()
        }

        main_provider = _clean_text(self._main_model.get("provider")).lower()
        utility_provider = _clean_text(self._utility_model.get("provider")).lower()
        if main_provider and main_provider not in self._provider_api_key_status:
            self._provider_api_key_status[main_provider] = self._main_has_api_key
        if utility_provider and utility_provider not in self._provider_api_key_status:
            self._provider_api_key_status[utility_provider] = self._utility_has_api_key

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
            value = _clean_text(provider).lower()
            if not value:
                return
            if value in seen:
                return
            seen.add(value)
            label_text = _clean_text(label) or _provider_label(value)
            ordered.append((label_text, value))

        for entry in options or ():
            if not isinstance(entry, tuple) or len(entry) < 2:
                continue
            _add(entry[1], entry[0])
        _add(main_model.get("provider"))
        _add(utility_model.get("provider"))

        if not ordered:
            ordered.extend(_DEFAULT_PROVIDER_OPTIONS)

        return tuple(ordered)

    def _provider_field_value(self, values: Mapping[str, Any]) -> str | object:
        provider = _clean_text(values.get("provider"))
        return provider if provider else Select.NULL

    def _api_key_placeholder(self, *, provider: str, explicit_key: str = "") -> str:
        if explicit_key:
            return "Custom key override for this chat"
        if self._provider_api_key_status.get(provider.strip().lower(), False):
            return "Already set in Agent Zero (leave empty to keep it)"
        return "Set your API key for this provider"

    def compose(self) -> ComposeResult:
        with Vertical(id="model-runtime-box"):
            yield Static("Change LLMs", id="model-runtime-title")
            yield Static(
                "Pick provider and model for this chat. API key and Base URL are optional overrides.",
                id="model-runtime-description",
            )
            yield from self._compose_section(
                "main",
                "Main Model",
                self._main_label,
                self._main_model,
            )
            yield from self._compose_section(
                "utility",
                "Utility Model",
                self._utility_label,
                self._utility_model,
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
        with Vertical(classes="model-runtime-section"):
            yield Static(title, classes="model-runtime-section-title")
            yield Static(f"Current: {current_label}", classes="model-runtime-current")
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
                value=_clean_text(values.get("name")),
                placeholder="Example: claude-sonnet-4 or gpt-4o",
                id=f"model-runtime-{key}-name",
            )
            yield Static("API Key", classes="model-runtime-label")
            yield Input(
                value=_clean_text(values.get("api_key")),
                placeholder=self._api_key_placeholder(
                    provider=_clean_text(values.get("provider")),
                    explicit_key=_clean_text(values.get("api_key")),
                ),
                password=True,
                id=f"model-runtime-{key}-api-key",
            )
            yield Static("Base URL", classes="model-runtime-label")
            yield Input(
                value=_clean_text(values.get("api_base")),
                placeholder="Optional: custom provider base URL",
                id=f"model-runtime-{key}-base-url",
            )

    def on_mount(self) -> None:
        target = "#model-runtime-main-provider"
        if self._focus_target == "utility":
            target = "#model-runtime-utility-provider"
        self.query_one(target, Select).focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_apply(self) -> None:
        self._apply()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id == "model-runtime-apply":
            self._apply()
            return
        if button_id == "model-runtime-cancel":
            self.dismiss(None)

    def on_select_changed(self, event: Select.Changed) -> None:
        select_id = event.select.id or ""
        if select_id == "model-runtime-main-provider":
            self._sync_api_key_placeholder("main")
        elif select_id == "model-runtime-utility-provider":
            self._sync_api_key_placeholder("utility")

    def on_input_changed(self, event: Input.Changed) -> None:
        input_id = event.input.id or ""
        if input_id == "model-runtime-main-api-key":
            self._sync_api_key_placeholder("main")
        elif input_id == "model-runtime-utility-api-key":
            self._sync_api_key_placeholder("utility")

    def _selected_provider(self, key: str) -> str:
        provider_value = self.query_one(f"#model-runtime-{key}-provider", Select).value
        if isinstance(provider_value, str):
            return _clean_text(provider_value).lower()
        fallback = self._main_model if key == "main" else self._utility_model
        return _clean_text(fallback.get("provider")).lower()

    def _sync_api_key_placeholder(self, key: str) -> None:
        input_widget = self.query_one(f"#model-runtime-{key}-api-key", Input)
        explicit_key = _clean_text(input_widget.value)
        input_widget.placeholder = self._api_key_placeholder(
            provider=self._selected_provider(key),
            explicit_key=explicit_key,
        )

    def _collect_model(self, key: str) -> dict[str, str]:
        provider_value = self.query_one(f"#model-runtime-{key}-provider", Select).value
        provider = _clean_text(provider_value) if isinstance(provider_value, str) else ""
        name = _clean_text(self.query_one(f"#model-runtime-{key}-name", Input).value)
        api_key = _clean_text(self.query_one(f"#model-runtime-{key}-api-key", Input).value)
        base_url = _clean_text(self.query_one(f"#model-runtime-{key}-base-url", Input).value)
        payload: dict[str, str] = {}
        if provider:
            payload["provider"] = provider
        if name:
            payload["name"] = name
        if api_key:
            payload["api_key"] = api_key
        if base_url:
            payload["api_base"] = base_url
        return payload

    def _apply(self) -> None:
        status = self.query_one("#model-runtime-status", Static)
        main_model = self._collect_model("main")
        utility_model = self._collect_model("utility")

        if not main_model and not utility_model:
            status.update(Text("Set at least one model target before applying.", style="#ff8b6b"))
            return

        if main_model:
            if not main_model.get("provider"):
                provider = _clean_text(self._main_model.get("provider"))
                if provider:
                    main_model["provider"] = provider
            if not main_model.get("name"):
                name = _clean_text(self._main_model.get("name"))
                if name:
                    main_model["name"] = name

        if utility_model:
            if not utility_model.get("provider"):
                provider = _clean_text(self._utility_model.get("provider"))
                if provider:
                    utility_model["provider"] = provider
            if not utility_model.get("name"):
                name = _clean_text(self._utility_model.get("name"))
                if name:
                    utility_model["name"] = name

        if main_model and not main_model.get("name"):
            status.update(Text("Main model name is required.", style="#ff8b6b"))
            return
        if utility_model and not utility_model.get("name"):
            status.update(Text("Utility model name is required.", style="#ff8b6b"))
            return

        self.dismiss(
            ModelRuntimeResult(
                main_model=main_model,
                utility_model=utility_model,
            )
        )


__all__ = [
    "ModelRuntimeResult",
    "ModelRuntimeScreen",
]
