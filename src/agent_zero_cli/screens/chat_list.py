from __future__ import annotations

from typing import Any

from textual.app import ComposeResult
from textual.containers import Center, Vertical
from textual.screen import ModalScreen
from textual.widgets import ListItem, ListView, Static


class ChatListScreen(ModalScreen[str | None]):
    """Modal showing previous chats. Returns selected context ID or None."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, contexts: list[dict[str, Any]]) -> None:
        super().__init__()
        self.contexts = contexts
        self._item_contexts: dict[str, str] = {}

    def compose(self) -> ComposeResult:
        with Center():
            with Vertical(id="chat-list-panel"):
                yield Static("Select a chat (Esc to cancel):", id="chat-list-title")
                if not self.contexts:
                    yield Static("No previous chats found.", id="chat-list-empty")
                    return
                items: list[ListItem] = []
                for index, context in enumerate(self.contexts, start=1):
                    context_id = str(context.get("id", ""))
                    name = context.get("name") or f"Chat {index}"
                    created_at = context.get("created_at") or ""
                    last_message = context.get("last_message") or ""
                    preview = last_message.replace("\n", " ")[:60]
                    parts = [f"{index}. {name}"]
                    if created_at:
                        parts.append(f"created {created_at}")
                    if preview:
                        parts.append(f"last: {preview}")
                    label = " | ".join(parts)
                    item_id = f"ctx-{context_id}" if context_id else f"ctx-missing-{index}"
                    self._item_contexts[item_id] = context_id
                    items.append(ListItem(Static(label), id=item_id))
                yield ListView(*items, id="chat-list")

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        item_id = event.item.id or ""
        context_id = self._item_contexts.get(item_id, "")
        self.dismiss(context_id if context_id else None)

    def action_cancel(self) -> None:
        self.dismiss(None)
