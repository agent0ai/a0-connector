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
    "status": "info",
    "message_complete": "info",
    "context_updated": "info",
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


def extract_detail(event_type: str, data: dict[str, Any]) -> str:
    """Extract a short human-readable detail string from event data."""
    heading = (data.get("heading") or "").strip()
    text = (data.get("text") or "").strip()

    if event_type in ("tool_start", "tool_output", "tool_end"):
        # heading is the tool name
        return heading[:40] if heading else ""

    if event_type in ("code_start", "code_output"):
        return heading[:40] if heading else ""

    if event_type == "context_updated":
        return "memory"

    if event_type == "status":
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

    if category == "code":
        code = meta.get("code") or ""
        if code or text:
            # Code block with a slightly lighter background and subtle border.
            # If both source code and output (text) are present, show both.
            md_content = f"```python\n{code}\n```" if code else ""
            if text:
                if md_content:
                    md_content += "\n\n---\n\n"
                md_content += text

            panel = Panel(
                Markdown(md_content),
                box=box.SIMPLE,
                padding=(1, 1),
                style="on #202124"
            )
            log.append_or_update(seq, Padding(panel, (1, 0, 1, 0)))
            return True
        return False

    return False
