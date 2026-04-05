from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from rich.markdown import Markdown
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Center, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Select, Static


@dataclass(frozen=True)
class ModelPreset:
    name: str
    label: str = ""
    description: str = ""


@dataclass(frozen=True)
class CompactResult:
    use_chat_model: bool
    preset_name: str | None


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


def _render_stats(stats: Mapping[str, Any] | None) -> Markdown:
    if not stats:
        return Markdown(
            "\n".join(
                [
                    "# Compaction stats",
                    "",
                    "No compaction stats are available yet.",
                ]
            )
        )

    lines = ["# Compaction stats", ""]
    seen: set[str] = set()
    preferred_fields = (
        ("message_count", "Messages"),
        ("token_count", "Tokens"),
        ("visible_count", "Visible messages"),
        ("minimum_tokens", "Minimum tokens"),
        ("max_tokens", "Maximum tokens"),
    )
    for key, label in preferred_fields:
        if key in stats:
            lines.append(f"- {label}: {stats[key]}")
            seen.add(key)

    extras = [key for key in stats if key not in seen and key != "ok"]
    if extras:
        lines.extend(["", "Additional details:"])
        for key in sorted(extras):
            lines.append(f"- {key.replace('_', ' ').title()}: {stats[key]}")

    return Markdown("\n".join(lines))


def _preset_options(
    presets: Sequence[ModelPreset | Mapping[str, Any] | str] | None,
) -> list[tuple[str, str]]:
    options: list[tuple[str, str]] = [("No preset", "")]
    for raw in presets or []:
        preset = _coerce_model_preset(raw)
        if not preset.name:
            continue
        label = preset.label or preset.name
        if preset.description:
            label = f"{label} - {preset.description}"
        options.append((label, preset.name))
    return options


def _preset_values(presets: Sequence[ModelPreset | Mapping[str, Any] | str] | None) -> set[str]:
    return {
        preset.name
        for preset in (_coerce_model_preset(raw) for raw in presets or [])
        if preset.name
    }


class CompactScreen(ModalScreen[CompactResult | None]):
    """Confirmation modal for connector-backed chat compaction."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+s", "compact", "Compact", show=True, priority=True),
    ]

    def __init__(
        self,
        stats: Mapping[str, Any] | None = None,
        presets: Sequence[ModelPreset | Mapping[str, Any] | str] | None = None,
        *,
        use_chat_model: bool = True,
        preset_name: str | None = None,
        available: bool = False,
        reason: str = "",
    ) -> None:
        super().__init__()
        self._stats = dict(stats or {})
        self._presets = list(presets or [])
        self._use_chat_model = use_chat_model
        self._preset_name = preset_name or ""
        self._available = available
        self._reason = reason
        self._status = ""
        self._suppress_events = False

    def compose(self) -> ComposeResult:
        with Center():
            with Vertical(id="compact-box"):
                yield Static("Compact Chat", id="compact-title")
                yield Static(
                    "Review the compaction stats, choose a model, and start compaction.",
                    id="compact-description",
                )
                yield Static(_render_stats(self._stats), id="compact-stats")
                with Horizontal(id="compact-controls"):
                    with Vertical(id="compact-model-field"):
                        yield Static("Model", id="compact-model-label")
                        yield Select(
                            [("Chat model", "chat"), ("Utility model", "utility")],
                            prompt="Model",
                            allow_blank=False,
                            value="chat" if self._use_chat_model else "utility",
                            id="compact-model",
                        )
                    with Vertical(id="compact-preset-field"):
                        yield Static("Preset", id="compact-preset-label")
                        yield Select(
                            _preset_options(self._presets),
                            prompt="Optional preset",
                            allow_blank=False,
                            value=self._preset_name or "",
                            id="compact-preset",
                        )
                yield Static("", id="compact-status")
                with Horizontal(id="compact-actions"):
                    yield Button("Cancel", id="compact-cancel")
                    yield Button("Compact", id="compact-submit", variant="primary")

    def on_mount(self) -> None:
        self._sync_controls()
        self._focus_model_select()

    def _focus_model_select(self) -> None:
        self.query_one("#compact-model", Select).focus()

    def _sync_controls(self) -> None:
        preset_values = _preset_values(self._presets)
        self._suppress_events = True
        try:
            self.query_one("#compact-stats", Static).update(_render_stats(self._stats))
            self.query_one("#compact-model", Select).value = "chat" if self._use_chat_model else "utility"
            self.query_one("#compact-preset", Select).value = (
                self._preset_name if self._preset_name in preset_values else ""
            )
        finally:
            self._suppress_events = False
        self._update_status()
        self._update_submit_state()

    def _update_status(self, message: str = "") -> None:
        self._status = message
        status = self.query_one("#compact-status", Static)
        if message:
            status.update(Text.from_markup(message))
        elif not self._available and self._reason:
            status.update(Text.from_markup(f"[yellow]{self._reason}[/yellow]"))
        else:
            status.update("")

    def _update_submit_state(self) -> None:
        self.query_one("#compact-submit", Button).disabled = not self._available

    def set_compaction_data(
        self,
        *,
        stats: Mapping[str, Any] | None = None,
        presets: Sequence[ModelPreset | Mapping[str, Any] | str] | None = None,
        available: bool | None = None,
        reason: str | None = None,
    ) -> None:
        """Replace the stats/presets snapshot shown by the modal."""

        if stats is not None:
            self._stats = dict(stats)
        if presets is not None:
            self._presets = list(presets)
        if available is not None:
            self._available = available
        if reason is not None:
            self._reason = reason

        self._suppress_events = True
        try:
            self.query_one("#compact-stats", Static).update(_render_stats(self._stats))
            self.query_one("#compact-preset", Select).set_options(_preset_options(self._presets))
        finally:
            self._suppress_events = False
        preset_values = _preset_values(self._presets)
        if self._preset_name not in preset_values:
            self._preset_name = ""
        self._sync_controls()

    @property
    def current_result(self) -> CompactResult:
        return CompactResult(
            use_chat_model=self._use_chat_model,
            preset_name=self._preset_name or None,
        )

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_compact(self) -> None:
        if not self._available:
            self._update_status(self._reason or "Compaction is unavailable.")
            return
        self.dismiss(self.current_result)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id == "compact-submit":
            self.action_compact()
        elif button_id == "compact-cancel":
            self.dismiss(None)

    def on_select_changed(self, event: Select.Changed) -> None:
        if self._suppress_events:
            return
        widget_id = event.select.id or ""
        if widget_id == "compact-model":
            self._use_chat_model = str(event.value) == "chat"
        elif widget_id == "compact-preset":
            self._preset_name = str(event.value)
        else:
            return
        self._update_status()


__all__ = [
    "CompactResult",
    "CompactScreen",
    "ModelPreset",
]
