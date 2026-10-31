from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
import re
from typing import Any, Mapping

from rich.console import Console, ConsoleOptions, Group, RenderResult
from rich.text import Text
from textual.app import ComposeResult
from textual import events
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.timer import Timer
from textual.worker import Worker
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from agent_zero_cli.text_utils import collapse_whitespace

_TOKEN_NAME_RE = re.compile(r"^[A-Za-z0-9]{6,16}$")
_ISO_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")


@dataclass(frozen=True)
class ChatListEntry:
    context_id: str
    title: str
    meta: str
    preview: str = ""


@dataclass(frozen=True)
class _SearchExcerpt:
    text: Text

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        lines = self.text.wrap(console, options.max_width)
        if len(lines) > 2:
            lines[1].truncate(max(0, options.max_width - 1))
            lines[1].append("…", style="not bold dim")
        yield from lines[:2]


def _parse_timestamp(value: object) -> datetime | None:
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value))
        except (OverflowError, OSError, ValueError):
            return None

    raw = collapse_whitespace(value)
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
    preview = collapse_whitespace(value)
    if not preview:
        return ""
    if preview == collapse_whitespace(created_at):
        return ""
    if _ISO_TIMESTAMP_RE.match(preview):
        return ""
    if len(preview) > 96:
        preview = f"{preview[:95].rstrip()}..."
    return preview


def _format_webui_title(context: Mapping[str, object], index: int) -> str:
    raw_name = collapse_whitespace(context.get("name"))
    if raw_name:
        return raw_name

    try:
        chat_no = int(context.get("no", 0) or 0)
    except (TypeError, ValueError):
        chat_no = 0
    if chat_no > 0:
        return f"Chat #{chat_no}"

    return f"Chat {index}"


def _build_entry(context: Mapping[str, object], index: int, *, now: datetime | None = None) -> ChatListEntry:
    context_id = collapse_whitespace(context.get("id"))
    raw_name = collapse_whitespace(context.get("name"))
    created_at = context.get("created_at")
    updated_at = context.get("updated_at")
    preview = _normalize_preview(context.get("last_message"), created_at=created_at)
    title = _format_webui_title(context, index)
    generated_name = _looks_generated_name(raw_name)

    parsed_created_at = _parse_timestamp(created_at)
    parsed_updated_at = _parse_timestamp(updated_at)

    if generated_name:
        title = _format_webui_title({**context, "name": ""}, index)

    meta_bits = [f"Started {_format_timestamp(parsed_created_at, now=now)}"]
    if parsed_updated_at and parsed_updated_at != parsed_created_at:
        meta_bits.append(f"Updated {_format_timestamp(parsed_updated_at, now=now)}")

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


class ChatListScreen(ModalScreen[str | None]):
    """Modal showing previous chats. Returns selected context ID or None."""

    BINDINGS = [("escape", "cancel", "Cancel"), ("ctrl+f", "focus_search", "Search")]

    def __init__(
        self, contexts: list[dict[str, Any]],
        *, search: Callable[[str], Awaitable[list[dict[str, Any]]]] | None = None,
    ) -> None:
        super().__init__()
        self._entries = [_build_entry(context, index) for index, context in enumerate(contexts, start=1)]
        self._search = search
        self._search_timer: Timer | None = None
        self._search_worker: Worker | None = None
        self._search_request = 0
        self._item_contexts: dict[str, str] = {}

    def compose(self) -> ComposeResult:
        with Vertical(id="chat-list-box"):
            yield Static("Chats", id="chat-list-title")
            yield Static(id="chat-list-description")
            yield Input(placeholder="Search", max_length=200, id="chat-list-search")
            yield OptionList(id="chat-list", markup=False)
            yield Static("No previous chats found.", id="chat-list-empty")

    def _render_results(self, query: str = "", matches: list[dict[str, Any]] | None = None) -> None:
        by_id = {match.get("id"): match for match in matches or [] if isinstance(match.get("id"), str)}
        options: list[Option] = []
        self._item_contexts.clear()
        for index, entry in enumerate(self._entries):
            title_match = query.casefold() in entry.title.casefold()
            match = by_id.get(entry.context_id)
            if query and not title_match and not match:
                continue
            item_id = f"ctx-{index}"
            self._item_contexts[item_id] = entry.context_id
            prompt = Text(entry.title, style="bold")
            prompt.append(f"\n{entry.meta}", style="not bold dim")
            if query:
                parts = match.get("parts", []) if match and not title_match else []
                if isinstance(parts, list) and parts:
                    preview = Text()
                    for part in parts:
                        if isinstance(part, dict) and isinstance(part.get("text"), str):
                            preview.append(part["text"], style="bold not dim" if part.get("match") else "not bold dim")
                    prompt = Group(prompt, _SearchExcerpt(preview))
            elif entry.preview:
                prompt.append(f"\n{entry.preview}", style="not bold")
            options.append(Option(prompt, id=item_id))
        chat_list = self.query_one("#chat-list", OptionList)
        chat_list.clear_options().add_options(options)
        chat_list.disabled = False
        chat_list.display = bool(options)
        chat_list.highlighted = 0 if options else None
        chat_list.scroll_home(animate=False)
        empty = self.query_one("#chat-list-empty", Static)
        empty.display = not options
        empty.update("No matching chats." if query else "No previous chats found.")
        count = f"{len(options)} of {len(self._entries)}" if query else str(len(self._entries))
        self.query_one("#chat-list-description", Static).update(
            f"{count} conversation{'s' if len(self._entries) != 1 else ''}. "
            f"Arrows and Enter to resume, Esc to {'clear' if query else 'cancel'}."
        )

    def on_mount(self) -> None:
        self._render_results()
        self.action_focus_search()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "chat-list-search":
            return
        event.stop()
        self._search_request += 1
        request = self._search_request
        if self._search_timer:
            self._search_timer.stop()
        if self._search_worker:
            self._search_worker.cancel()
        query = event.value.strip()
        if not query:
            self._render_results()
            return
        self.query_one("#chat-list", OptionList).clear_options()
        self._item_contexts.clear()
        self.query_one("#chat-list", OptionList).disabled = True
        self.query_one("#chat-list-empty", Static).display = False
        self.query_one("#chat-list-description", Static).update("Searching…")
        self._search_timer = self.set_timer(0.3, lambda: self._start_search(query, request))

    def _start_search(self, query: str, request: int) -> None:
        if not self.is_attached or request != self._search_request:
            return
        self._search_worker = self.run_worker(
            self._load_matches(query, request), group="chat-search", exclusive=True,
        )

    async def _load_matches(self, query: str, request: int) -> None:
        try:
            matches = await self._search(query) if self._search else []
        except Exception as exc:
            if self.is_attached and request == self._search_request:
                self.query_one("#chat-list-description", Static).update("Search failed. Clear to show all chats.")
                self.notify(str(exc), severity="error")
            return
        if self.is_attached and request == self._search_request:
            self._render_results(query, matches)

    def on_key(self, event: events.Key) -> None:
        search = self.query_one("#chat-list-search", Input)
        if search.has_focus and event.key in ("up", "down", "pageup", "pagedown"):
            event.stop()
            event.prevent_default()
            chat_list = self.query_one("#chat-list", OptionList)
            if not chat_list.disabled:
                action = {"up": "cursor_up", "down": "cursor_down", "pageup": "page_up", "pagedown": "page_down"}[event.key]
                getattr(chat_list, f"action_{action}")()
        elif not search.has_focus and event.character and event.character.isprintable():
            search.focus()
            search.value += event.character
            event.stop()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "chat-list-search":
            event.stop()
            chat_list = self.query_one("#chat-list", OptionList)
            if not chat_list.disabled:
                chat_list.action_select()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        context_id = self._item_contexts.get(event.option_id or "", "")
        if context_id and not self.query_one("#chat-list", OptionList).disabled:
            self.dismiss(context_id)

    def action_focus_search(self) -> None:
        self.query_one("#chat-list-search", Input).focus()

    def action_cancel(self) -> None:
        search = self.query_one("#chat-list-search", Input)
        if search.value:
            search.value = ""
            search.focus()
        else:
            self.dismiss(None)

    def on_unmount(self) -> None:
        self._search_request += 1
        if self._search_timer:
            self._search_timer.stop()
        if self._search_worker:
            self._search_worker.cancel()
