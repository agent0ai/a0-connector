from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import re
from typing import Any, Mapping

from textual.app import ComposeResult
from textual.containers import Center, Vertical
from textual.screen import ModalScreen
from textual.widgets import ListItem, ListView, Static

_WHITESPACE_RE = re.compile(r"\s+")
_TOKEN_NAME_RE = re.compile(r"^[A-Za-z0-9]{6,16}$")
_ISO_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")


@dataclass(frozen=True)
class ChatListEntry:
    context_id: str
    title: str
    meta: str
    preview: str = ""


def _normalize_text(value: object) -> str:
    return _WHITESPACE_RE.sub(" ", str(value or "")).strip()


def _parse_timestamp(value: object) -> datetime | None:
    raw = _normalize_text(value)
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone() if parsed.tzinfo else parsed


def _format_timestamp(value: datetime | None, *, now: datetime | None = None) -> str:
    if value is None:
        return "Unknown time"

    if now is None:
        current = datetime.now(value.tzinfo) if value.tzinfo else datetime.now()
    elif value.tzinfo and now.tzinfo:
        current = now.astimezone(value.tzinfo)
    else:
        current = now

    if value.date() == current.date():
        return value.strftime("Today %H:%M")
    if value.date() == (current.date() - timedelta(days=1)):
        return value.strftime("Yesterday %H:%M")
    if value.year == current.year:
        return value.strftime("%b %d %H:%M")
    return value.strftime("%Y-%m-%d %H:%M")


def _looks_generated_name(value: str) -> bool:
    if not _TOKEN_NAME_RE.fullmatch(value):
        return False
    if value.islower() or value.isupper():
        return False
    if len(value) > 1 and value[:1].isupper() and value[1:].islower():
        return False
    return True


def _normalize_preview(value: object, *, created_at: object = "") -> str:
    preview = _normalize_text(value)
    if not preview:
        return ""
    if preview == _normalize_text(created_at):
        return ""
    if _ISO_TIMESTAMP_RE.match(preview):
        return ""
    if len(preview) > 96:
        preview = f"{preview[:95].rstrip()}..."
    return preview


def _build_entry(context: Mapping[str, object], index: int, *, now: datetime | None = None) -> ChatListEntry:
    context_id = _normalize_text(context.get("id"))
    raw_name = _normalize_text(context.get("name"))
    created_at = context.get("created_at")
    preview = _normalize_preview(context.get("last_message"), created_at=created_at)
    title = raw_name or f"Chat {index}"
    generated_name = _looks_generated_name(raw_name)

    if generated_name:
        title = f"Chat {index}"

    meta_bits = [f"Started {_format_timestamp(_parse_timestamp(created_at), now=now)}"]
    if context.get("running"):
        meta_bits.append("Running now")
    if generated_name:
        meta_bits.append(f"ID {raw_name}")

    if preview and preview.casefold() == title.casefold():
        preview = ""

    return ChatListEntry(
        context_id=context_id,
        title=title,
        meta=" | ".join(meta_bits),
        preview=preview,
    )


class ChatListRow(ListItem):
    def __init__(self, entry: ChatListEntry, *, item_id: str) -> None:
        super().__init__(id=item_id, classes="chat-list-item")
        self._entry = entry

    def compose(self) -> ComposeResult:
        with Vertical(classes="chat-list-item-body"):
            yield Static(self._entry.title, classes="chat-list-item-title")
            yield Static(self._entry.meta, classes="chat-list-item-meta")
            if self._entry.preview:
                yield Static(self._entry.preview, classes="chat-list-item-preview")


class ChatListScreen(ModalScreen[str | None]):
    """Modal showing previous chats. Returns selected context ID or None."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, contexts: list[dict[str, Any]]) -> None:
        super().__init__()
        self.contexts = contexts
        self._item_contexts: dict[str, str] = {}

    def compose(self) -> ComposeResult:
        with Center():
            with Vertical(id="chat-list-box"):
                yield Static("Chats", id="chat-list-title")
                yield Static(
                    f"{len(self.contexts)} conversation{'s' if len(self.contexts) != 1 else ''}. "
                    "Use arrows and Enter to resume, Esc to cancel.",
                    id="chat-list-description",
                )
                if not self.contexts:
                    yield Static("No previous chats found.", id="chat-list-empty")
                    return

                items: list[ListItem] = []
                for index, context in enumerate(self.contexts, start=1):
                    entry = _build_entry(context, index)
                    context_id = entry.context_id
                    item_id = f"ctx-{context_id}" if context_id else f"ctx-missing-{index}"
                    self._item_contexts[item_id] = context_id
                    items.append(ChatListRow(entry, item_id=item_id))
                yield ListView(*items, id="chat-list")

    def on_mount(self) -> None:
        if self.contexts:
            self.query_one("#chat-list", ListView).focus()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        item_id = event.item.id or ""
        context_id = self._item_contexts.get(item_id, "")
        self.dismiss(context_id if context_id else None)

    def action_cancel(self) -> None:
        self.dismiss(None)
