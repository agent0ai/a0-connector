from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import Button, Static

from agent_zero_cli.rendering import format_duration


class GoalBar(Vertical):
    """Compact goal state row above the composer."""

    class UpdateRequested(Message):
        def __init__(self, bar: GoalBar) -> None:
            super().__init__()
            self.bar = bar

    class PauseResumeRequested(Message):
        def __init__(self, bar: GoalBar) -> None:
            super().__init__()
            self.bar = bar

    class DeleteRequested(Message):
        def __init__(self, bar: GoalBar) -> None:
            super().__init__()
            self.bar = bar

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.goal: dict[str, Any] | None = None
        self._header = Horizontal(id="goal-bar-header")
        self._summary = Static("", id="goal-bar-summary")
        self._objective = Static("", id="goal-bar-objective")
        self._update = Button("✎ Edit", id="goal-bar-update", classes="goal-bar-action")
        self._pause_resume = Button("Ⅱ Pause", id="goal-bar-pause-resume", classes="goal-bar-action")
        self._delete = Button("× Delete", id="goal-bar-delete", classes="goal-bar-action")
        self.display = False

    def compose(self) -> ComposeResult:
        with self._header:
            yield self._summary
            yield self._update
            yield self._pause_resume
            yield self._delete
        yield self._objective

    def clear(self) -> None:
        self.goal = None
        self.display = False
        self._summary.update("")
        self._objective.update("")

    def set_goal(self, goal: Mapping[str, Any] | None) -> None:
        normalized = dict(goal) if isinstance(goal, Mapping) else None
        objective = str((normalized or {}).get("objective") or "").strip()
        status = str((normalized or {}).get("status") or "active").strip().lower()
        self.goal = normalized if objective else None
        self.display = bool(self.goal and status != "complete")
        if not self.display:
            self._summary.update("")
            self._objective.update("")
            return

        self._pause_resume.label = "Ⅱ Pause" if status == "active" else "▶ Resume"
        self._summary.update(self._render_summary())
        self._objective.update(self._render_objective())

    def _render_summary(self) -> Text:
        goal = self.goal or {}
        status = str(goal.get("status") or "active").strip().lower()
        label = {
            "paused": "Goal paused",
            "blocked": "Goal blocked",
        }.get(status, "Goal")
        marker_color = {
            "paused": "#f5c35a",
            "blocked": "#ef767a",
        }.get(status, "#00b4ff")

        text = Text("●", style=marker_color)
        text.append(f" {label}", style="bold #d9e2ec")
        text.append(f" · {format_duration(_elapsed_seconds(goal))}", style="#7f8c98")
        return text

    def _render_objective(self) -> Text:
        objective = str((self.goal or {}).get("objective") or "").strip()
        return Text(objective, style="#929fac")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id == "goal-bar-update":
            event.stop()
            self.post_message(self.UpdateRequested(self))
            return
        if button_id == "goal-bar-pause-resume":
            event.stop()
            self.post_message(self.PauseResumeRequested(self))
            return
        if button_id == "goal-bar-delete":
            event.stop()
            self.post_message(self.DeleteRequested(self))


def _elapsed_seconds(goal: Mapping[str, Any]) -> int:
    try:
        seconds = int(goal.get("elapsed_seconds", 0) or 0)
    except (TypeError, ValueError):
        seconds = 0
    if str(goal.get("status") or "active") == "active":
        seconds += _seconds_since(str(goal.get("active_since") or goal.get("created_at") or ""))
    return max(0, seconds)


def _seconds_since(value: str) -> int:
    text = str(value or "").strip()
    if not text:
        return 0
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        start = datetime.fromisoformat(text)
    except ValueError:
        return 0
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    return max(0, int((datetime.now(timezone.utc) - start.astimezone(timezone.utc)).total_seconds()))
