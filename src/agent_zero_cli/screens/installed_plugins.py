from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping, Sequence

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Center, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, ListItem, ListView, Static

from agent_zero_cli.text_utils import clip_text, coerce_bool, collapse_whitespace, safe_id_fragment


PluginToggleCallback = Callable[["InstalledPluginEntry", bool], Awaitable[Sequence[Mapping[str, Any]]]]


@dataclass(frozen=True)
class InstalledPluginEntry:
    name: str
    display_name: str
    description: str = ""
    version: str = ""
    source: str = "builtin"
    enabled: bool = True
    toggleable: bool = True
    protected_reason: str = ""
    always_enabled: bool = False

    @property
    def title(self) -> str:
        return self.display_name or self.name

    @property
    def search_text(self) -> str:
        return " ".join(
            (
                self.name,
                self.display_name,
                self.description,
                self.version,
                self.source,
            )
        ).casefold()


def coerce_installed_plugin(value: Mapping[str, Any]) -> InstalledPluginEntry:
    name = collapse_whitespace(value.get("name"))
    display_name = collapse_whitespace(value.get("display_name") or value.get("title")) or name
    toggle_state = collapse_whitespace(value.get("toggle_state")).lower()
    always_enabled = coerce_bool(value.get("always_enabled"))
    enabled = coerce_bool(
        value.get("enabled"),
        default=always_enabled or toggle_state == "enabled",
    )
    protected_reason = collapse_whitespace(value.get("protected_reason"))
    toggleable = coerce_bool(value.get("toggleable"), default=not always_enabled)
    if protected_reason:
        toggleable = False
    source = collapse_whitespace(value.get("source"))
    if not source:
        source = "custom" if coerce_bool(value.get("is_custom")) else "builtin"

    return InstalledPluginEntry(
        name=name,
        display_name=display_name,
        description=collapse_whitespace(value.get("description")),
        version=collapse_whitespace(value.get("version")),
        source=source.title(),
        enabled=enabled,
        toggleable=toggleable,
        protected_reason=protected_reason,
        always_enabled=always_enabled,
    )


def coerce_installed_plugins(values: Sequence[Mapping[str, Any]]) -> tuple[InstalledPluginEntry, ...]:
    entries = [coerce_installed_plugin(value) for value in values if collapse_whitespace(value.get("name"))]
    return tuple(
        sorted(
            entries,
            key=lambda entry: (
                not entry.enabled,
                entry.title.casefold(),
                entry.name.casefold(),
            ),
        )
    )


def _item_id(entry: InstalledPluginEntry, index: int) -> str:
    safe_name = safe_id_fragment(entry.name) or str(index)
    return f"installed-plugin-{index}-{safe_name}"


class InstalledPluginRow(ListItem):
    def __init__(self, entry: InstalledPluginEntry, *, item_id: str) -> None:
        super().__init__(id=item_id, classes="installed-plugin-row")
        self.entry = entry

    def compose(self) -> ComposeResult:
        marker = "[*]" if self.entry.enabled else "[ ]"
        if not self.entry.toggleable:
            marker = "[!]" if self.entry.enabled else "[-]"
        state = "Enabled" if self.entry.enabled else "Disabled"
        source = self.entry.source or "Builtin"
        version = f" v{self.entry.version}" if self.entry.version else ""
        description = clip_text(self.entry.description or self.entry.protected_reason, 76)
        action = "Space to disable" if self.entry.enabled else "Space to enable"
        if not self.entry.toggleable:
            action = "Protected"

        with Horizontal(classes="installed-plugin-row-line"):
            yield Static(marker, classes="installed-plugin-marker")
            yield Static(self.entry.title, classes="installed-plugin-name")
            yield Static(state, classes="installed-plugin-state")
            yield Static(f"{source}{version}", classes="installed-plugin-meta")
            yield Static(action, classes="installed-plugin-action")
        if description:
            yield Static(description, classes="installed-plugin-description")


class InstalledPluginsScreen(ModalScreen[None]):
    """Installed-only plugin manager. Space toggles the selected plugin."""

    BINDINGS = [
        Binding("escape", "cancel", "Close", priority=True),
        Binding("space", "toggle_selected", "Enable/Disable", show=True, priority=True),
        Binding("enter", "show_details", "Details", show=True, priority=True),
        Binding("ctrl+f", "focus_search", "Search", show=False, priority=True),
    ]

    def __init__(
        self,
        plugins: Sequence[Mapping[str, Any]],
        *,
        toggle_callback: PluginToggleCallback | None = None,
    ) -> None:
        super().__init__()
        self._entries = coerce_installed_plugins(plugins)
        self._toggle_callback = toggle_callback
        self._item_names: dict[str, str] = {}
        self._busy = False
        self._filter = ""

    def compose(self) -> ComposeResult:
        with Center():
            with Vertical(id="installed-plugins-box"):
                yield Static("Plugins", id="installed-plugins-title")
                yield Static("", id="installed-plugins-summary")
                yield Input(
                    placeholder="Type to search installed plugins",
                    id="installed-plugins-search",
                )
                yield ListView(*self._rows_for_entries(self._filtered_entries()), id="installed-plugins-list")
                yield Static("", id="installed-plugins-empty")
                yield Static("", id="installed-plugins-status")
                yield Static(
                    "space toggle | arrows | enter details | esc close",
                    id="installed-plugins-help",
                )

    def on_mount(self) -> None:
        self._sync_summary()
        self._sync_empty()
        self.query_one("#installed-plugins-list", ListView).focus()

    def _rows_for_entries(self, entries: Sequence[InstalledPluginEntry]) -> list[InstalledPluginRow]:
        rows: list[InstalledPluginRow] = []
        self._item_names = {}
        for index, entry in enumerate(entries, start=1):
            item_id = _item_id(entry, index)
            self._item_names[item_id] = entry.name
            rows.append(InstalledPluginRow(entry, item_id=item_id))
        return rows

    def _filtered_entries(self) -> tuple[InstalledPluginEntry, ...]:
        query = self._filter.casefold().strip()
        if not query:
            return self._entries
        return tuple(entry for entry in self._entries if query in entry.search_text)

    async def _rebuild_rows(self, *, preserve_name: str = "") -> None:
        entries = self._filtered_entries()
        list_view = self.query_one("#installed-plugins-list", ListView)
        if not list_view.is_attached:
            return

        await list_view.clear()
        if not list_view.is_attached:
            return

        rows = self._rows_for_entries(entries)
        if rows:
            await list_view.extend(rows)
        self._sync_empty()
        if not entries:
            return

        selected_index = 0
        if preserve_name:
            for index, entry in enumerate(entries):
                if entry.name == preserve_name:
                    selected_index = index
                    break
        list_view.index = selected_index

    def _sync_summary(self) -> None:
        enabled_count = sum(1 for entry in self._entries if entry.enabled)
        total = len(self._entries)
        self.query_one("#installed-plugins-summary", Static).update(
            f"{enabled_count}/{total} installed plugins enabled. Installed-only toggles."
        )

    def _sync_empty(self) -> None:
        empty = self.query_one("#installed-plugins-empty", Static)
        empty.display = not bool(self._filtered_entries())
        if not self._entries:
            empty.update("No installed plugins were reported by Agent Zero Core.")
        elif self._filter:
            empty.update("No installed plugins match the current search.")
        else:
            empty.update("")

    def _set_status(self, message: str, *, error: bool = False) -> None:
        status = self.query_one("#installed-plugins-status", Static)
        if not message:
            status.update("")
            return
        status.update(Text(message, style="#ff8b6b" if error else "#9aa7b4"))

    def _selected_entry(self) -> InstalledPluginEntry | None:
        list_view = self.query_one("#installed-plugins-list", ListView)
        child = list_view.highlighted_child
        item_id = child.id if child is not None else ""
        name = self._item_names.get(item_id or "")
        if not name:
            entries = self._filtered_entries()
            index = list_view.index or 0
            if 0 <= index < len(entries):
                return entries[index]
            return None
        return next((entry for entry in self._entries if entry.name == name), None)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "installed-plugins-search":
            return
        sanitized = "".join(character for character in event.value if character.isprintable())
        if sanitized != event.value:
            event.input.value = sanitized
            return
        self._filter = sanitized
        self.run_worker(
            self._rebuild_rows(),
            exclusive=True,
            name="installed-plugins-filter",
        )

    def on_key(self, event: events.Key) -> None:
        if self._busy or isinstance(self.app.focused, Input):
            return
        character = event.character or ""
        if not character or character.isspace():
            return
        search = self.query_one("#installed-plugins-search", Input)
        search.focus()
        search.value = f"{search.value}{character}"
        event.stop()

    def action_focus_search(self) -> None:
        self.query_one("#installed-plugins-search", Input).focus()

    def action_show_details(self) -> None:
        entry = self._selected_entry()
        if entry is None:
            self._set_status("No installed plugin is selected.", error=True)
            return

        bits = [entry.name]
        if entry.version:
            bits.append(f"v{entry.version}")
        bits.append(entry.source)
        if entry.description:
            bits.append(entry.description)
        if entry.protected_reason:
            bits.append(entry.protected_reason)
        self._set_status(" | ".join(bits))

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        del event
        self.action_show_details()

    def action_toggle_selected(self) -> None:
        if self._busy:
            return
        entry = self._selected_entry()
        if entry is None:
            self._set_status("No installed plugin is selected.", error=True)
            return
        if not entry.toggleable:
            reason = entry.protected_reason or "Agent Zero marks this plugin as protected."
            self._set_status(f"{entry.title} cannot be toggled: {reason}", error=True)
            return

        self.run_worker(
            self._toggle_entry(entry),
            exclusive=True,
            name=f"installed-plugin-toggle-{entry.name}",
        )

    async def _toggle_entry(self, entry: InstalledPluginEntry) -> None:
        target_enabled = not entry.enabled
        action = "Enabling" if target_enabled else "Disabling"
        self._busy = True
        self._set_status(f"{action} {entry.title}...")
        try:
            if self._toggle_callback is None:
                raise RuntimeError("No plugin toggle handler is configured.")
            plugins = await self._toggle_callback(entry, target_enabled)
            self._entries = coerce_installed_plugins(plugins)
            self._sync_summary()
            await self._rebuild_rows(preserve_name=entry.name)
        except Exception as exc:
            self._set_status(f"Failed to update {entry.title}: {exc}", error=True)
            return
        finally:
            self._busy = False

        state = "enabled" if target_enabled else "disabled"
        self._set_status(f"{entry.title} {state}. Changes may affect new Agent Zero runs.")

    def action_cancel(self) -> None:
        self.dismiss(None)


__all__ = [
    "InstalledPluginEntry",
    "InstalledPluginsScreen",
    "coerce_installed_plugin",
    "coerce_installed_plugins",
]
