from __future__ import annotations

from typing import Mapping

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.message import Message
from textual.reactive import reactive
from textual.widgets import Static

from agent_zero_cli.model_config import coerce_positive_int
from agent_zero_cli.project_utils import display_project_title, normalize_project_summary, project_color


def _format_token_count(value: int) -> str:
    if value >= 1_000_000:
        formatted = f"{value / 1_000_000:.1f}".rstrip("0").rstrip(".")
        return f"{formatted}M"
    if value >= 1_000:
        formatted = f"{value / 1_000:.1f}".rstrip("0").rstrip(".")
        return f"{formatted}k"
    return str(value)


class ConnectionStatus(Horizontal):
    """Top-right header with token usage, project state, and endpoint status."""

    class ProjectTrigger(Static):
        class Requested(Message):
            def __init__(self, trigger: "ConnectionStatus.ProjectTrigger") -> None:
                super().__init__()
                self.trigger = trigger

        enabled = reactive(False)

        def on_click(self) -> None:
            if self.enabled:
                self.post_message(self.Requested(self))

    class ProjectRequested(Message):
        def __init__(self, status_bar: "ConnectionStatus") -> None:
            super().__init__()
            self.status_bar = status_bar

    status = reactive("connecting")
    url = reactive("")
    token_count = reactive(None)
    token_limit = reactive(None)
    current_project = reactive(None)
    project_enabled = reactive(False)
    computer_use_status = reactive("")
    computer_use_detail = reactive("")

    def compose(self) -> ComposeResult:
        yield Static("", id="connection-status-spacer")
        yield Static("", id="connection-status-budget")
        yield self.ProjectTrigger("", id="connection-status-project")
        yield Static("", id="connection-status-endpoint")

    def on_mount(self) -> None:
        self._sync_segments()

    def watch_status(self, status: str) -> None:
        del status
        self._sync_segments()

    def watch_url(self, url: str) -> None:
        del url
        self._sync_segments()

    def watch_token_count(self, token_count: object) -> None:
        del token_count
        self._sync_segments()

    def watch_token_limit(self, token_limit: object) -> None:
        del token_limit
        self._sync_segments()

    def watch_current_project(self, project: object) -> None:
        del project
        self._sync_segments()

    def watch_project_enabled(self, enabled: bool) -> None:
        del enabled
        self._sync_segments()

    def watch_computer_use_status(self, value: str) -> None:
        del value
        self._sync_segments()

    def watch_computer_use_detail(self, value: str) -> None:
        del value
        self._sync_segments()

    def on_project_trigger_requested(self, event: ProjectTrigger.Requested) -> None:
        if event.trigger.id == "connection-status-project" and event.trigger.enabled:
            self.post_message(self.ProjectRequested(self))

    def set_token_usage(self, token_count: object, token_limit: object = None) -> None:
        self.token_count = coerce_positive_int(token_count)
        self.token_limit = coerce_positive_int(token_limit)

    def clear_token_usage(self) -> None:
        self.token_count = None
        self.token_limit = None

    def set_project_state(
        self,
        project: Mapping[str, object] | None,
        *,
        enabled: bool = False,
    ) -> None:
        self.current_project = normalize_project_summary(project)
        self.project_enabled = enabled

    def set_project_enabled(self, enabled: bool) -> None:
        self.project_enabled = enabled

    def clear_project_state(self) -> None:
        self.current_project = None
        self.project_enabled = False

    def set_computer_use_state(self, status: str, detail: str = "") -> None:
        self.computer_use_status = str(status or "").strip()
        self.computer_use_detail = str(detail or "").strip()

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

    def _render_project_trigger(self) -> Text:
        project = normalize_project_summary(self.current_project)
        color = project_color(project)
        label = display_project_title(project, default="No project")
        dot = "●" if color else "○"

        trigger = Text()
        trigger.append(dot, style=color or "#7f8c98")
        trigger.append(f" {label}", style="#d9e2ec" if project is not None else "#9aa7b4")
        return trigger

    def _render_endpoint_indicator(self) -> Text:
        label = self.url.strip()
        if not label:
            label = {
                "connected": "Connected",
                "connecting": "Connecting",
            }.get(self.status, "Disconnected")

        dot_color = {
            "connected": "green",
            "connecting": "yellow",
        }.get(self.status, "red")

        endpoint = Text.assemble(
            (label, "dim"),
            (" ", "dim"),
            ("•", dot_color),
        )
        return endpoint

    def _sync_segments(self) -> None:
        try:
            budget = self.query_one("#connection-status-budget", Static)
            project = self.query_one("#connection-status-project", self.ProjectTrigger)
            endpoint = self.query_one("#connection-status-endpoint", Static)
        except Exception:
            return

        budget_text = self._render_token_budget()
        budget.update(budget_text)
        budget.display = bool(budget_text.plain.strip())

        project.display = self.project_enabled
        project.enabled = self.project_enabled
        project.update(self._render_project_trigger())

        endpoint.update(self._render_endpoint_indicator())
