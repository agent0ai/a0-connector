from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Any

from rich.align import Align
from rich import box
from rich.console import Group
from rich.markdown import Markdown
from rich.panel import Panel
from rich.padding import Padding
from rich.text import Text

from agent_zero_cli.icon_text import normalize_icon_text, strip_icon_markers

if TYPE_CHECKING:
    from agent_zero_cli.widgets.chat_log import ChatLog


_EVENT_CATEGORY: dict[str, str] = {
    "user_message": "user",
    "assistant_message": "response",
    "assistant_delta": "response",
    "tool_start": "tool",
    "tool_output": "tool",
    "tool_end": "tool",
    "code_start": "code",
    "code_output": "code",
    "warning": "warning",
    "error": "error",
    "info": "info",
    "util_message": "util",
    "status": "status",
    "message_complete": "status",
    "context_updated": "status",
}

# Human-readable activity labels per event type.
# "user_message" intentionally omitted — no indicator shown when sending.
# "Responding" intentionally omitted — the chat log response covers that.
_STATUS_LABEL: dict[str, str] = {
    "assistant_message": "Responding",
    "assistant_delta": "Responding",
    "tool_start": "Using tool",
    "tool_output": "Using tool",
    "tool_end": "Using tool",
    "code_start": "Running code",
    "code_output": "Running code",
    "status": "Thinking",
    "context_updated": "Updating memory",
}

_CODE_HEADING_PREFIX_RE = re.compile(r"^\s*icon://terminal\s*")
_CODE_HEADING_SESSION_RE = re.compile(r"^\[\d+\]\s*")
_GENERIC_CODE_DETAIL_RE = re.compile(
    r"^(?:code_execution(?:_remote)?(?:_tool)?\s*-\s*)?"
    r"(?:python|ipython|nodejs|node|terminal|output|input|reset)\s*$",
    re.IGNORECASE,
)
_SHELL_ECHO_LINE_RE = re.compile(
    r"^\s*(?:python|node|bash|sh|zsh|fish|pwsh|powershell|ps)>\s+.*$",
    re.IGNORECASE,
)
_SESSION_COMPLETE_LINE_RE = re.compile(r"^\s*Session \d+ completed\.\s*$")
_OSC_TITLE_PREFIX_RE = re.compile(r"^\s*\d+;[A-Za-z0-9_.+-]+:\s*")
_OSC_CWD_PREFIX_RE = re.compile(
    r"^(?:/?(?:[a-z0-9._-]+/)+[a-z0-9._-]*)(?=[A-Z0-9\"'(\[{])"
)
_PROMPT_LINE_RE = re.compile(
    r"^\s*(?:\([^)\n]+\)\s*)?(?:[\w.-]+@[\w.-]+:)?[/~.\w-]*[#$]\s*$"
)


def format_duration(seconds: float | int) -> str:
    total_seconds = max(0, int(float(seconds) + 0.5))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, remaining = divmod(remainder, 60)
    return (
        f"{hours}h{minutes}m{remaining}s"
        if hours
        else f"{minutes}m{remaining}s" if minutes else f"{remaining}s"
    )


@dataclass(frozen=True)
class ConnectorEventParts:
    event_type: str
    category: str
    sequence: int
    heading: str
    text: str
    meta: dict[str, Any]
    label: str
    detail: str
    message: str
    code: str
    code_output: str


def _plain_text(value: object, *, style: str = "") -> Text:
    return Text(str(value or ""), style=style)


def _event_message(heading: object, text: object) -> str:
    heading_text = normalize_icon_text(heading)
    body_text = strip_icon_markers(text).strip()
    return f"{heading_text}: {body_text}" if heading_text else body_text


def _normalize_code_heading(heading: str, *, suppress_generic: bool = True) -> str:
    normalized = " ".join(str(heading or "").split())
    if not normalized:
        return ""

    normalized = _CODE_HEADING_PREFIX_RE.sub("", normalized, count=1)
    normalized = normalize_icon_text(normalized)
    normalized = _CODE_HEADING_SESSION_RE.sub("", normalized, count=1).strip()
    if suppress_generic and _GENERIC_CODE_DETAIL_RE.match(normalized):
        return ""
    return normalized


def _normalize_heading(heading: object) -> str:
    raw = str(heading or "")
    if _CODE_HEADING_PREFIX_RE.match(raw):
        return _normalize_code_heading(raw, suppress_generic=False)
    return normalize_icon_text(raw)


def _strip_terminal_title_noise(line: str) -> str:
    stripped = _OSC_TITLE_PREFIX_RE.sub("", line, count=1)
    if stripped != line:
        stripped = _OSC_CWD_PREFIX_RE.sub("", stripped, count=1).lstrip()
    return stripped


def _sanitize_code_output(text: str, *, code_present: bool) -> str:
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return ""

    lines = normalized.splitlines()

    if code_present and lines and _SHELL_ECHO_LINE_RE.match(lines[0]):
        lines.pop(0)

    if len(lines) > 1 and lines and _SESSION_COMPLETE_LINE_RE.match(lines[0]):
        lines.pop(0)

    cleaned_lines = [_strip_terminal_title_noise(line).rstrip() for line in lines]

    while cleaned_lines and not cleaned_lines[0].strip():
        cleaned_lines.pop(0)

    while cleaned_lines and not cleaned_lines[-1].strip():
        cleaned_lines.pop()

    while cleaned_lines and _PROMPT_LINE_RE.match(cleaned_lines[-1]):
        cleaned_lines.pop()

    return "\n".join(cleaned_lines).strip()


def extract_detail(event_type: str, data: dict[str, Any]) -> str:
    """Extract a short human-readable detail string from event data."""
    heading = (data.get("heading") or "").strip()
    text = (data.get("text") or "").strip()
    meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}

    if event_type in ("tool_start", "tool_output", "tool_end"):
        # heading is the tool name
        clean_heading = _normalize_heading(heading)
        return clean_heading[:40] if clean_heading else ""

    if event_type in ("code_start", "code_output"):
        clean_heading = _normalize_code_heading(heading)
        return clean_heading[:40] if clean_heading else ""

    if event_type == "context_updated":
        return "memory"

    if event_type == "status":
        step = str(meta.get("step") or "").strip()
        if step:
            return _normalize_heading(step)[:50]

        headline = str(meta.get("headline") or "").strip()
        if headline:
            return _normalize_heading(headline)[:50]

        tool_name = str(meta.get("tool_name") or "").strip()
        if tool_name:
            return f"Using {normalize_icon_text(tool_name)}"[:50]

        # text may be a JSON blob or a sentence — take first sentence only
        raw = heading or text
        # strip obvious JSON artifacts
        if raw.startswith("{") or raw.startswith("["):
            return ""
        first = _normalize_heading(raw.split(".")[0].split("\n")[0])
        return first[:50] if first else ""

    return ""


def describe_connector_event(event: dict[str, Any]) -> ConnectorEventParts:
    """Return UI-neutral display parts for a connector context event."""
    event_type = str(event.get("event") or "")
    data = event.get("data", {})
    if not isinstance(data, dict):
        data = {}
    text = str(data.get("text") or "")
    heading = str(data.get("heading") or "")
    meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
    category = _EVENT_CATEGORY.get(event_type, "info")
    label = _STATUS_LABEL.get(event_type, "")
    detail = extract_detail(event_type, data)
    sequence_raw = event.get("sequence", -1)
    try:
        sequence = int(sequence_raw)
    except (TypeError, ValueError):
        sequence = -1

    code = ""
    code_output = ""
    if category == "code":
        code = str(meta.get("code") or "").rstrip()
        code_output = _sanitize_code_output(text, code_present=bool(code))

    return ConnectorEventParts(
        event_type=event_type,
        category=category,
        sequence=sequence,
        heading=heading,
        text=text,
        meta=meta,
        label=label,
        detail=detail,
        message=_event_message(heading, text),
        code=code,
        code_output=code_output,
    )


def render_connector_event(
    log: ChatLog,
    event: dict[str, Any],
    *,
    prepend: bool = False,
) -> bool:
    """Render a connector event to the chat log.
    
    Returns:
        bool: True if a static block was rendered, False otherwise.
    """
    parts = describe_connector_event(event)
    event_type = parts.event_type
    text = parts.text
    seq = parts.sequence
    category = parts.category

    if category == "user":
        if text:
            panel = Panel(_plain_text(text), border_style="#555555", padding=(0, 1))
            log.append_or_update(seq, Align.right(panel), prepend=prepend)
            return True
        return False

    if category == "response":
        if text:
            if seq == 1:
                log.mark_intro_message(seq)
            # Add markdown render inside Left aligned or normal layout
            panel = Panel(Markdown(str(text)), border_style="#233e54", padding=(0, 1))
            log.append_or_update(seq, panel, prepend=prepend)
            return True
        return False

    if category == "warning":
        msg = parts.message
        log.append_or_update(seq, _plain_text(msg, style="yellow"), prepend=prepend)
        return True

    if category == "error":
        msg = parts.message
        log.append_or_update(seq, _plain_text(msg, style="red"), prepend=prepend)
        return True

    if category == "info":
        msg = parts.message
        if msg:
            log.append_or_update(
                seq,
                Padding(_plain_text(msg, style="dim"), (0, 0, 0, 2)),
                prepend=prepend,
            )
            return True
        return False

    if category == "util":
        msg = parts.message
        if msg:
            log.append_or_update(
                seq,
                Padding(_plain_text(msg, style="dim"), (0, 0, 0, 2)),
                prepend=prepend,
            )
            return True
        return False

    if category == "code":
        code = parts.code
        display_text = parts.code_output
        if code or display_text:
            blocks: list[Text] = []
            if code:
                blocks.append(Text(code, style="#f2f5f7"))
            if display_text:
                blocks.append(Text(display_text, style="#b8c2cc"))

            log.append_or_update_code(
                seq,
                parts.label,
                parts.detail,
                Panel(
                    Group(*blocks),
                    box=box.SIMPLE,
                    padding=(0, 1),
                    style="on #202124",
                    expand=False,
                ),
                prepend=prepend,
            )
            return True
        return False

    return False
