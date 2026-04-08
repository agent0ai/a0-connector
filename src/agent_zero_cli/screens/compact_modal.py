from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual import events
from textual.containers import Center, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static


@dataclass(frozen=True)
class CompactResult:
    use_chat_model: bool
    preset_name: str | None


def _format_metric(value: object, default: str = "0") -> str:
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        if value.is_integer():
            return f"{int(value):,}"
        return f"{value:,.2f}"
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


class CompactScreen(ModalScreen[CompactResult | None]):
    """Confirmation modal for connector-backed chat compaction."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+s", "compact", "Compact", show=True, priority=True),
    ]

    def __init__(
        self,
        stats: Mapping[str, Any] | None = None,
        *,
        use_chat_model: bool = True,
        available: bool = False,
        reason: str = "",
    ) -> None:
        super().__init__()
        self._stats = dict(stats or {})
        self._use_chat_model = use_chat_model
        self._available = available
        self._reason = reason
        self._status = ""

    def compose(self) -> ComposeResult:
        with Center():
            with Vertical(id="compact-box"):
                yield Static("Compact Chat History", id="compact-title")
                yield Static(
                    "This will summarize your entire conversation into a single optimized message.",
                    id="compact-description",
                )
                with Horizontal(id="compact-metrics"):
                    with Vertical(classes="compact-metric-card", id="compact-messages-card"):
                        yield Static("MESSAGES", classes="compact-metric-label")
                        yield Static("0", classes="compact-metric-value", id="compact-messages-value")
                    with Vertical(classes="compact-metric-card", id="compact-tokens-card"):
                        yield Static("TOKENS", classes="compact-metric-label")
                        yield Static("0", classes="compact-metric-value", id="compact-tokens-value")
                with Horizontal(id="compact-model-and-note"):
                    with Vertical(id="compact-model-field"):
                        yield Static("MODEL", id="compact-model-label")
                        with Horizontal(id="compact-mode-toggle"):
                            yield Button("Chat", id="compact-mode-chat")
                            yield Button("Utility", id="compact-mode-utility")
                        yield Static("", id="compact-model-preview")
                    with Vertical(id="compact-note-field"):
                        yield Static(
                            "The context will be replaced with a compacted summary. "
                            "The original conversation will be backed up.",
                            id="compact-note",
                        )
                yield Static("", id="compact-status")
                with Horizontal(id="compact-actions"):
                    yield Button("Cancel", id="compact-cancel")
                    yield Button("Compact", id="compact-submit", variant="primary")

    def on_mount(self) -> None:
        self._sync_controls()
        self._sync_responsive_layout(self.size.width)
        self._focus_primary_control()

    def on_resize(self, event: events.Resize) -> None:
        self._sync_responsive_layout(event.size.width)

    def _sync_responsive_layout(self, width: int) -> None:
        if width < 110:
            self.add_class("compact-narrow")
        else:
            self.remove_class("compact-narrow")

    def _focus_primary_control(self) -> None:
        button_id = "#compact-mode-chat" if self._use_chat_model else "#compact-mode-utility"
        self.query_one(button_id, Button).focus()

    def _sync_controls(self) -> None:
        self.query_one("#compact-messages-value", Static).update(
            _format_metric(self._stats.get("message_count"), default="0")
        )
        self.query_one("#compact-tokens-value", Static).update(
            _format_metric(self._stats.get("token_count"), default="0")
        )
        self._sync_mode_buttons()
        self._sync_mode_preview()
        self._update_status()
        self._update_submit_state()

    def _sync_mode_buttons(self) -> None:
        chat_button = self.query_one("#compact-mode-chat", Button)
        utility_button = self.query_one("#compact-mode-utility", Button)
        if self._use_chat_model:
            chat_button.add_class("is-active")
            utility_button.remove_class("is-active")
        else:
            utility_button.add_class("is-active")
            chat_button.remove_class("is-active")

    def _sync_mode_preview(self) -> None:
        mode_label = "Chat" if self._use_chat_model else "Utility"
        self.query_one("#compact-model-preview", Static).update(f"{mode_label} model selected")

    def _update_status(self, message: str = "") -> None:
        self._status = message
        status = self.query_one("#compact-status", Static)
        if message:
            status.update(Text(message))
        elif not self._available and self._reason:
            status.update(Text(self._reason, style="yellow"))
        else:
            status.update("")

    def _update_submit_state(self) -> None:
        self.query_one("#compact-submit", Button).disabled = not self._available

    def set_compaction_data(
        self,
        *,
        stats: Mapping[str, Any] | None = None,
        available: bool | None = None,
        reason: str | None = None,
    ) -> None:
        """Replace the compaction snapshot shown by the modal."""

        if stats is not None:
            self._stats = dict(stats)
        if available is not None:
            self._available = available
        if reason is not None:
            self._reason = reason

        self._sync_controls()

    @property
    def current_result(self) -> CompactResult:
        return CompactResult(
            use_chat_model=self._use_chat_model,
            preset_name=None,
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
        if button_id == "compact-mode-chat":
            self._use_chat_model = True
            self._sync_controls()
            return
        if button_id == "compact-mode-utility":
            self._use_chat_model = False
            self._sync_controls()
            return
        if button_id == "compact-submit":
            self.action_compact()
        elif button_id == "compact-cancel":
            self.dismiss(None)


__all__ = [
    "CompactResult",
    "CompactScreen",
]
