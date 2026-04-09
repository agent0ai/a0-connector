from __future__ import annotations

from rich.text import Text
from textual.app import RenderResult
from textual.reactive import reactive
from textual.widgets import Static

from agent_zero_cli.model_config import coerce_positive_int


def _format_token_count(value: int) -> str:
    if value >= 1_000_000:
        formatted = f"{value / 1_000_000:.1f}".rstrip("0").rstrip(".")
        return f"{formatted}M"
    if value >= 1_000:
        formatted = f"{value / 1_000:.1f}".rstrip("0").rstrip(".")
        return f"{formatted}k"
    return str(value)


class ConnectionStatus(Static):
    """A subtle connection status indicator at the top right."""

    status = reactive("connecting")
    url = reactive("")
    token_count = reactive(None)
    token_limit = reactive(None)
    _tick_count = reactive(0)

    def on_mount(self) -> None:
        self.set_interval(0.1, self._tick)

    def _tick(self) -> None:
        if self.status == "connecting":
            self._tick_count += 1

    def set_token_usage(self, token_count: object, token_limit: object = None) -> None:
        self.token_count = coerce_positive_int(token_count)
        self.token_limit = coerce_positive_int(token_limit)

    def clear_token_usage(self) -> None:
        self.token_count = None
        self.token_limit = None

    def _render_token_budget(self) -> Text:
        count = self.token_count
        if not isinstance(count, int):
            return Text()

        limit = self.token_limit if isinstance(self.token_limit, int) else None
        usage = _format_token_count(count)
        ratio = 0.0
        if limit and limit > 0:
            ratio = min(max(count / limit, 0.0), 1.0)
            usage = f"{usage}/{_format_token_count(limit)}"
        else:
            limit = None

        gauge_slots = 8
        gauge_filled = min(gauge_slots, max(0, int(round(ratio * gauge_slots)))) if limit else 0
        gauge = ""
        if limit:
            gauge = ("■" * gauge_filled) + ("·" * (gauge_slots - gauge_filled))

        if ratio >= 0.9:
            gauge_color = "#ff8b6b"
        elif ratio >= 0.75:
            gauge_color = "#f5c35a"
        else:
            gauge_color = "#79d18a"

        budget = Text.assemble(
            ("Tokens ", "dim"),
            (usage, "#d9e2ec"),
        )
        if gauge:
            budget.append(" ")
            budget.append(gauge, style=gauge_color)
        return budget

    def render(self) -> RenderResult:
        token_budget = self._render_token_budget()
        has_budget = bool(token_budget.plain.strip())

        label = self.url.strip()
        prefix = Text()
        if has_budget:
            prefix.append_text(token_budget)
            if label:
                prefix.append("  ", style="dim")
        if label:
            prefix.append(label, style="dim")
            prefix.append(" ", style="dim")

        if self.status == "connected":
            return Text.assemble(
                prefix,
                ("•", "green")
            )
        elif self.status == "connecting":
            return Text.assemble(
                prefix,
                ("•", "yellow")
            )
        else:
            disconnected = Text("Disconnected ", style="dim")
            if has_budget:
                disconnected.append_text(token_budget)
                disconnected.append("  ", style="dim")
            if label:
                disconnected.append(f"({label}) ", style="dim")
            return Text.assemble(
                disconnected,
                ("•", "red")
            )
