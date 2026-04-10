import re
from typing import Any

from rich.align import Align
from rich import box
from rich.markdown import Markdown
from rich.panel import Panel
from rich.padding import Padding

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


def _normalize_code_heading(heading: str) -> str:
    normalized = " ".join(str(heading or "").split())
    if not normalized:
        return ""

    normalized = _CODE_HEADING_PREFIX_RE.sub("", normalized, count=1)
    normalized = _CODE_HEADING_SESSION_RE.sub("", normalized, count=1).strip()
    if _GENERIC_CODE_DETAIL_RE.match(normalized):
        return ""
    return normalized


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
        return heading[:40] if heading else ""

    if event_type in ("code_start", "code_output"):
        clean_heading = _normalize_code_heading(heading)
        return clean_heading[:40] if clean_heading else ""

    if event_type == "context_updated":
        return "memory"

    if event_type == "status":
        step = str(meta.get("step") or "").strip()
        if step:
            return step[:50]

        headline = str(meta.get("headline") or "").strip()
        if headline:
            return headline[:50]

        tool_name = str(meta.get("tool_name") or "").strip()
        if tool_name:
            return f"Using {tool_name}"[:50]

        # text may be a JSON blob or a sentence — take first sentence only
        raw = heading or text
        # strip obvious JSON artifacts
        if raw.startswith("{") or raw.startswith("["):
            return ""
        first = raw.split(".")[0].split("\n")[0].strip()
        return first[:50] if first else ""

    return ""


def render_connector_event(log: ChatLog, event: dict[str, Any]) -> bool:
    """Render a connector event to the chat log.
    
    Returns:
        bool: True if a static block was rendered, False otherwise.
    """
    event_type = event.get("event", "")
    data = event.get("data", {})
    text = data.get("text", "")
    heading = data.get("heading", "")
    meta = data.get("meta", {})
    seq = event.get("sequence", -1)

    category = _EVENT_CATEGORY.get(event_type, "info")

    if category == "user":
        if text:
            panel = Panel(text, border_style="#555555", padding=(0, 1))
            log.append_or_update(seq, Align.right(panel))
            return True
        return False

    if category == "response":
        if text:
            # Add markdown render inside Left aligned or normal layout
            panel = Panel(Markdown(text), border_style="#233e54", padding=(0, 1))
            log.append_or_update(seq, panel)
            return True
        return False

    if category == "warning":
        msg = f"{heading}: {text}" if heading else text
        log.append_or_update(seq, f"[yellow]{msg}[/yellow]")
        return True

    if category == "error":
        msg = f"{heading}: {text}" if heading else text
        log.append_or_update(seq, f"[red]{msg}[/red]")
        return True

    if category == "info":
        msg = f"{heading}: {text}" if heading else text
        if msg:
            log.append_or_update(seq, Padding(f"[dim]{msg}[/dim]", (0, 0, 0, 2)))
            return True
        return False

    if category == "util":
        msg = f"{heading}: {text}" if heading and text else heading or text
        if msg:
            log.append_or_update(seq, Padding(f"[dim]{msg}[/dim]", (0, 0, 0, 2)))
            return True
        return False

    if category == "code":
        code = str(meta.get("code") or "").rstrip()
        display_text = _sanitize_code_output(text, code_present=bool(code))
        if code or display_text:
            markdown_parts: list[str] = []
            if code:
                markdown_parts.append(f"```python\n{code}\n```")
            if display_text:
                markdown_parts.append(f"```text\n{display_text}\n```")
            md_content = "\n\n".join(markdown_parts)

            log.append_or_update_code(
                seq,
                _STATUS_LABEL[event_type],
                extract_detail(event_type, data),
                Panel(
                    Markdown(md_content),
                    box=box.SIMPLE,
                    padding=(1, 1),
                    style="on #202124",
                ),
            )
            return True
        return False

    return False
