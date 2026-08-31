from __future__ import annotations

import json
import os
from typing import Any, Protocol

from agent_zero_cli.rendering import describe_connector_event


class HeadlessRenderer(Protocol):
    def render_stage(self, stage: str, message: str, detail: str = "") -> list[str]: ...
    def render_event(self, event: dict[str, Any]) -> list[str]: ...
    def render_snapshot(self, events: list[dict[str, Any]], queue: list[dict[str, Any]]) -> list[str]: ...
    def render_complete(self, context_id: str) -> list[str]: ...
    def render_tag_result(
        self,
        context_id: str,
        *,
        mode: str,
        text: str,
        valid: bool,
        error: str = "",
    ) -> list[str]: ...
    def render_error(self, code: str, message: str) -> list[str]: ...
    def render_notice(self, message: str, *, error: bool = False) -> list[str]: ...


def _json_line(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


class TextRenderer:
    def __init__(self, *, color: bool = False) -> None:
        self.color = color and not os.environ.get("NO_COLOR")
        self._last_status: tuple[str, str] | None = None

    def render_stage(self, stage: str, message: str, detail: str = "") -> list[str]:
        if stage == "ready":
            text = "ready"
            if detail:
                text = f"{text}: {detail}"
            return [self._style(text, "green")]
        if stage == "warning":
            return [self._style(message, "yellow")] if message else []
        if stage in {"connecting", "login"}:
            text = message or stage
            if detail:
                text = f"{text} ({detail})"
            return [self._style(text, "dim")]
        if stage == "error":
            text = message or "error"
            if detail:
                text = f"{text}: {detail}"
            return [self._style(text, "red")]
        return [message] if message else []

    def render_event(self, event: dict[str, Any]) -> list[str]:
        parts = describe_connector_event(event)
        category = parts.category

        if category == "user":
            return [f"You: {parts.text.strip()}"] if parts.text.strip() else []
        if category == "response":
            return [parts.text.rstrip()] if parts.text.strip() else []
        if category == "warning":
            return [self._style(parts.message, "yellow")] if parts.message else []
        if category == "error":
            return [self._style(parts.message, "red")] if parts.message else []
        if category in {"info", "util"}:
            return [self._style(parts.message, "dim")] if parts.message else []
        if category == "code":
            if parts.code or parts.code_output:
                blocks: list[str] = []
                if parts.code:
                    blocks.append(f"```python\n{parts.code}\n```")
                if parts.code_output:
                    blocks.append(f"```text\n{parts.code_output}\n```")
                return ["\n\n".join(blocks)]
            return self._status_line(parts.label, parts.detail)
        if parts.label:
            return self._status_line(parts.label, parts.detail)
        return [parts.message] if parts.message else []

    def render_snapshot(self, events: list[dict[str, Any]], queue: list[dict[str, Any]]) -> list[str]:
        lines: list[str] = []
        self._last_status = None
        for event in events:
            lines.extend(self.render_event(event))
        if queue:
            item_label = "message" if len(queue) == 1 else "messages"
            lines.append(self._style(f"{len(queue)} queued {item_label}", "dim"))
        return lines

    def render_complete(self, context_id: str) -> list[str]:
        del context_id
        self._last_status = None
        return []

    def render_tag_result(
        self,
        context_id: str,
        *,
        mode: str,
        text: str,
        valid: bool,
        error: str = "",
    ) -> list[str]:
        del context_id, mode, valid, error
        return [text] if text else []

    def render_error(self, code: str, message: str) -> list[str]:
        return [self._style(f"{code}: {message}", "red")]

    def render_notice(self, message: str, *, error: bool = False) -> list[str]:
        return [self._style(message, "red" if error else "dim")]

    def _status_line(self, label: str, detail: str) -> list[str]:
        normalized = (label.strip(), detail.strip())
        if not normalized[0] or normalized == self._last_status:
            return []
        self._last_status = normalized
        suffix = f" [{normalized[1]}]" if normalized[1] else ""
        return [self._style(f"- {normalized[0]}{suffix}", "dim")]

    def _style(self, text: str, style: str) -> str:
        if not self.color or not text:
            return text
        codes = {
            "dim": "2",
            "green": "32",
            "yellow": "33",
            "red": "31",
        }
        code = codes.get(style)
        if not code:
            return text
        return f"\033[{code}m{text}\033[0m"


class JsonlRenderer:
    def render_stage(self, stage: str, message: str, detail: str = "") -> list[str]:
        if stage == "ready":
            return [_json_line({"type": "ready", "message": message, "detail": detail})]
        if stage in {"connecting", "login", "warning"}:
            return [_json_line({"type": "stage", "stage": stage, "message": message, "detail": detail})]
        if stage == "error":
            return [_json_line({"type": "error", "code": "STAGE_ERROR", "message": message, "detail": detail})]
        return []

    def render_event(self, event: dict[str, Any]) -> list[str]:
        payload = {"type": "event"}
        payload.update(event)
        return [_json_line(payload)]

    def render_snapshot(self, events: list[dict[str, Any]], queue: list[dict[str, Any]]) -> list[str]:
        lines = [self.render_event(event)[0] for event in events]
        if queue:
            lines.append(_json_line({"type": "message_queue", "items": queue}))
        return lines

    def render_complete(self, context_id: str) -> list[str]:
        return [_json_line({"type": "complete", "context_id": context_id})]

    def render_tag_result(
        self,
        context_id: str,
        *,
        mode: str,
        text: str,
        valid: bool,
        error: str = "",
    ) -> list[str]:
        payload: dict[str, Any] = {
            "type": "tag_result",
            "context_id": context_id,
            "mode": mode,
            "text": text,
            "valid": valid,
        }
        if error:
            payload["error"] = error
        return [_json_line(payload)]

    def render_error(self, code: str, message: str) -> list[str]:
        return [_json_line({"type": "error", "code": code, "message": message})]

    def render_notice(self, message: str, *, error: bool = False) -> list[str]:
        return [_json_line({"type": "error" if error else "notice", "message": message})]


def make_renderer(output: str, *, color: bool) -> HeadlessRenderer:
    if output == "jsonl":
        return JsonlRenderer()
    return TextRenderer(color=color)
