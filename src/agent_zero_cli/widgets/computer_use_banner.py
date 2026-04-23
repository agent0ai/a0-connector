from __future__ import annotations

from textual.widgets import Static


def _normalize_status(value: str) -> str:
    return str(value or "").strip().lower()


def _message_for_status(status: str, *, enabled: bool) -> str:
    normalized = _normalize_status(status)
    if not enabled or normalized == "disabled":
        return ""
    if normalized == "active":
        return "Agent Zero CLI is controlling your computer. Leave your mouse free."
    if normalized == "approval required":
        return "Agent Zero CLI is requesting computer control. Leave your mouse free if you approve the step."
    if normalized == "rearm required":
        return "Computer use needs re-arming before Agent Zero can control your computer again."
    return "Agent Zero CLI can control your computer in this session. Leave your mouse free during computer-use steps."


class ComputerUseBanner(Static):
    """High-visibility warning banner above the composer controls."""

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__("", id=id)
        self.display = False

    def set_state(self, *, enabled: bool, status: str = "") -> None:
        message = _message_for_status(status, enabled=enabled)
        self.display = bool(message)
        self.update(message)


__all__ = ["ComputerUseBanner"]
