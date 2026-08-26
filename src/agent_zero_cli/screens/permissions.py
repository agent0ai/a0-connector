from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import re
from typing import Any, Mapping, Sequence

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Center, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, ListItem, ListView, Static


_ID_SAFE_RE = re.compile(r"[^A-Za-z0-9_-]+")


@dataclass(frozen=True)
class PermissionEntry:
    kind: str
    key: str
    label: str
    description: str = ""
    origin: str = ""
    available: bool = True

    @property
    def search_text(self) -> str:
        return " ".join(
            (self.key, self.label, self.description, self.origin)
        ).casefold()


@dataclass(frozen=True)
class PermissionsResult:
    tool_policy: dict[str, Any]
    skill_policy: dict[str, Any]
    tool_changed: bool
    skill_changed: bool


def _ids(values: object) -> list[str]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        return []
    return sorted({str(value).strip() for value in values if str(value).strip()})


def _normalize_policy(value: object, *, include_mcp: bool) -> dict[str, Any]:
    policy = value if isinstance(value, Mapping) else {}
    normalized: dict[str, Any] = {
        "mode": "custom" if policy.get("mode") == "custom" else "inherit",
        "default": "block" if policy.get("default") == "block" else "allow",
        "allowed": _ids(policy.get("allowed")),
        "blocked": _ids(policy.get("blocked")),
    }
    if include_mcp:
        normalized["mcp_default"] = (
            "block" if policy.get("mcp_default") == "block" else "allow"
        )
    return normalized


def _policy_behavior(policy: Mapping[str, Any], *, include_mcp: bool) -> dict[str, Any]:
    if policy.get("mode") != "custom":
        return {
            "default": "allow",
            **({"mcp_default": "allow"} if include_mcp else {}),
            "allowed": [],
            "blocked": [],
        }
    return {
        "default": "block" if policy.get("default") == "block" else "allow",
        **(
            {
                "mcp_default": (
                    "block" if policy.get("mcp_default") == "block" else "allow"
                )
            }
            if include_mcp
            else {}
        ),
        "allowed": _ids(policy.get("allowed")),
        "blocked": _ids(policy.get("blocked")),
    }


def _initial_policy(state: Mapping[str, Any], *, include_mcp: bool) -> dict[str, Any]:
    policy = _normalize_policy(state.get("policy"), include_mcp=include_mcp)
    if not state.get("has_override"):
        policy["mode"] = "inherit"
    return policy


def _entry_state(policy: Mapping[str, Any], key: str) -> str:
    if policy.get("mode") != "custom":
        return "default"
    if key in policy.get("blocked", ()):
        return "block"
    if key in policy.get("allowed", ()):
        return "allow"
    return "default"


def _clip(value: str, limit: int = 84) -> str:
    return value if len(value) <= limit else f"{value[: limit - 1].rstrip()}..."


def _profile_label(state: Mapping[str, Any]) -> str:
    profile = state.get("profile") if isinstance(state.get("profile"), Mapping) else {}
    metadata = profile.get("metadata") if isinstance(profile.get("metadata"), Mapping) else {}
    title = metadata.get("title") if isinstance(metadata.get("title"), Mapping) else {}
    return str(title.get("effective") or profile.get("id") or "Agent").strip()


def _entries(state: Mapping[str, Any]) -> tuple[PermissionEntry, ...]:
    result: list[PermissionEntry] = []
    tools = state.get("tools") if isinstance(state.get("tools"), Mapping) else {}
    for item in tools.get("catalog") or ():
        if not isinstance(item, Mapping):
            continue
        key = str(item.get("id") or "").strip()
        if not key:
            continue
        result.append(
            PermissionEntry(
                kind="mcps" if key.startswith("mcp:") else "tools",
                key=key,
                label=str(item.get("label") or item.get("name") or key).strip(),
                description=str(item.get("description") or "").strip(),
                origin=str(item.get("origin") or "").strip(),
                available=item.get("available") is not False,
            )
        )
    skills = state.get("skills") if isinstance(state.get("skills"), Mapping) else {}
    for item in skills.get("catalog") or ():
        if not isinstance(item, Mapping):
            continue
        key = str(item.get("name") or "").strip()
        if not key:
            continue
        result.append(
            PermissionEntry(
                kind="skills",
                key=key,
                label=key,
                description=str(item.get("description") or "").strip(),
                origin=str(item.get("origin") or "").strip(),
                available=item.get("available") is not False,
            )
        )
    return tuple(sorted(result, key=lambda entry: (entry.kind, entry.label.casefold(), entry.key)))


class PermissionRow(ListItem):
    def __init__(self, entry: PermissionEntry, state: str, default: str, *, item_id: str) -> None:
        super().__init__(id=item_id, classes="permission-row")
        self.entry = entry
        self.state = state
        self.default = default

    def compose(self) -> ComposeResult:
        marker = {"allow": "[+]", "default": "[*]", "block": "[-]"}[self.state]
        label = {
            "allow": "On",
            "default": f"Default ({'on' if self.default == 'allow' else 'off'})",
            "block": "Off",
        }[self.state]
        with Horizontal(classes="permission-row-line"):
            yield Static(marker, classes="permission-marker")
            yield Static(self.entry.label, classes="permission-name")
            yield Static(label, classes=f"permission-state permission-state-{self.state}")
        description = self.entry.description
        if not self.entry.available:
            description = f"{description} · Unavailable" if description else "Unavailable · kept in settings"
        if description:
            yield Static(_clip(description), classes="permission-description")


class PermissionsScreen(ModalScreen[PermissionsResult | None]):
    BINDINGS = [
        Binding("escape", "cancel", "Cancel", priority=True),
        Binding("ctrl+s", "save", "Save", show=False, priority=True),
    ]

    def __init__(self, state: Mapping[str, Any]) -> None:
        super().__init__()
        self._profile_label = _profile_label(state)
        tools = state.get("tools") if isinstance(state.get("tools"), Mapping) else {}
        skills = state.get("skills") if isinstance(state.get("skills"), Mapping) else {}
        self._tool_effective = _normalize_policy(tools.get("effective_policy"), include_mcp=True)
        self._skill_effective = _normalize_policy(skills.get("effective_policy"), include_mcp=False)
        self._initial_tool = _initial_policy(tools, include_mcp=True)
        self._initial_skill = _initial_policy(skills, include_mcp=False)
        self._tool_policy = deepcopy(self._initial_tool)
        self._skill_policy = deepcopy(self._initial_skill)
        self._entries = _entries(state)
        self._category = "tools"
        self._filter = ""
        self._item_keys: dict[str, str] = {}

    def compose(self) -> ComposeResult:
        with Center():
            with Vertical(id="permissions-box"):
                yield Static("Agent permissions", id="permissions-title")
                yield Static(self._profile_label, id="permissions-profile")
                with Horizontal(id="permissions-categories"):
                    yield Button("Tools", id="permissions-category-tools")
                    yield Button("MCPs", id="permissions-category-mcps")
                    yield Button("Skills", id="permissions-category-skills")
                yield Button("", id="permissions-default")
                yield Input(placeholder="Search permissions", id="permissions-search")
                yield ListView(id="permissions-list")
                yield Static("", id="permissions-empty")
                yield Static("space/enter change | arrows navigate | ctrl+s save | esc cancel", id="permissions-help")
                with Horizontal(id="permissions-actions"):
                    yield Button("Cancel", id="permissions-cancel")
                    yield Button("Save", id="permissions-save", variant="primary")

    def on_mount(self) -> None:
        self._sync_category()
        self.run_worker(self._rebuild_rows(), exclusive=True, name="permissions-initial-rows")

    def _active_policy(self) -> dict[str, Any]:
        return self._skill_policy if self._category == "skills" else self._tool_policy

    def _effective_policy(self) -> dict[str, Any]:
        return self._skill_effective if self._category == "skills" else self._tool_effective

    def _default_field(self) -> str:
        return "mcp_default" if self._category == "mcps" else "default"

    def _default_value(self) -> str:
        policy = self._active_policy()
        if policy.get("mode") != "custom":
            policy = self._effective_policy()
        return "block" if policy.get(self._default_field()) == "block" else "allow"

    def _filtered_entries(self) -> tuple[PermissionEntry, ...]:
        query = self._filter.casefold().strip()
        return tuple(
            entry
            for entry in self._entries
            if entry.kind == self._category and (not query or query in entry.search_text)
        )

    def _row_id(self, entry: PermissionEntry, index: int) -> str:
        safe = _ID_SAFE_RE.sub("-", entry.key).strip("-") or str(index)
        return f"permission-{index}-{safe}"

    async def _rebuild_rows(self, *, preserve_key: str = "") -> None:
        entries = self._filtered_entries()
        list_view = self.query_one("#permissions-list", ListView)
        await list_view.clear()
        self._item_keys = {}
        policy = self._active_policy()
        default = self._default_value()
        rows: list[PermissionRow] = []
        for index, entry in enumerate(entries, start=1):
            item_id = self._row_id(entry, index)
            self._item_keys[item_id] = entry.key
            rows.append(
                PermissionRow(
                    entry,
                    _entry_state(policy if policy.get("mode") == "custom" else self._effective_policy(), entry.key),
                    default,
                    item_id=item_id,
                )
            )
        if rows:
            await list_view.extend(rows)
        empty = self.query_one("#permissions-empty", Static)
        empty.display = not bool(entries)
        empty.update("No permissions match this search." if self._filter else f"No {self._category} are available.")
        if entries:
            list_view.index = next(
                (index for index, entry in enumerate(entries) if entry.key == preserve_key),
                0,
            )
            list_view.focus()

    def _sync_category(self) -> None:
        names = {"tools": "tools", "mcps": "MCPs", "skills": "skills"}
        for category in names:
            self.query_one(f"#permissions-category-{category}", Button).set_class(
                category == self._category, "is-active"
            )
        state = "On" if self._default_value() == "allow" else "Off"
        self.query_one("#permissions-default", Button).label = (
            f"Allow {names[self._category]} by default: {state}"
        )

    def _customize(self) -> dict[str, Any]:
        policy = self._active_policy()
        if policy.get("mode") == "custom":
            return policy
        effective = _policy_behavior(
            self._effective_policy(), include_mcp=self._category != "skills"
        )
        policy.clear()
        policy.update({"mode": "custom", **effective})
        return policy

    def _collapse(self) -> None:
        initial = self._initial_skill if self._category == "skills" else self._initial_tool
        if initial.get("mode") == "custom":
            return
        policy = self._active_policy()
        include_mcp = self._category != "skills"
        if _policy_behavior(policy, include_mcp=include_mcp) == _policy_behavior(
            self._effective_policy(), include_mcp=include_mcp
        ):
            policy.clear()
            policy.update(deepcopy(initial))

    def _selected_key(self) -> str:
        child = self.query_one("#permissions-list", ListView).highlighted_child
        return self._item_keys.get((child.id if child else "") or "", "")

    def action_cycle_selected(self) -> None:
        if isinstance(self.app.focused, (Input, Button)):
            return
        key = self._selected_key()
        if not key:
            return
        policy = self._customize()
        current = _entry_state(policy, key)
        next_state = {"default": "allow", "allow": "block", "block": "default"}[current]
        policy["allowed"] = [value for value in policy["allowed"] if value != key]
        policy["blocked"] = [value for value in policy["blocked"] if value != key]
        if next_state == "allow":
            policy["allowed"].append(key)
        elif next_state == "block":
            policy["blocked"].append(key)
        policy["allowed"].sort()
        policy["blocked"].sort()
        self._collapse()
        self.run_worker(
            self._rebuild_rows(preserve_key=key),
            exclusive=True,
            name="permissions-change-state",
        )

    def action_save(self) -> None:
        self.dismiss(
            PermissionsResult(
                tool_policy=deepcopy(self._tool_policy),
                skill_policy=deepcopy(self._skill_policy),
                tool_changed=self._tool_policy != self._initial_tool,
                skill_changed=self._skill_policy != self._initial_skill,
            )
        )

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "permissions-search":
            return
        self._filter = "".join(character for character in event.value if character.isprintable())
        if self._filter != event.value:
            event.input.value = self._filter
            return
        self.run_worker(self._rebuild_rows(), exclusive=True, name="permissions-filter")

    def on_key(self, event: events.Key) -> None:
        if isinstance(self.app.focused, ListView):
            if event.key == "space":
                self.action_cycle_selected()
                event.stop()
                return
        if isinstance(self.app.focused, (Input, Button)):
            return
        character = event.character or ""
        if not character or character.isspace():
            return
        search = self.query_one("#permissions-search", Input)
        search.focus()
        search.value = f"{search.value}{character}"
        event.stop()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        del event
        self.action_cycle_selected()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id.startswith("permissions-category-"):
            self._category = button_id.removeprefix("permissions-category-")
            self._filter = ""
            self.query_one("#permissions-search", Input).value = ""
            self._sync_category()
            self.run_worker(self._rebuild_rows(), exclusive=True, name="permissions-category")
            return
        if button_id == "permissions-default":
            policy = self._customize()
            field = self._default_field()
            policy[field] = "block" if self._default_value() == "allow" else "allow"
            self._collapse()
            self._sync_category()
            self.run_worker(self._rebuild_rows(), exclusive=True, name="permissions-default")
            return
        if button_id == "permissions-cancel":
            self.action_cancel()
        elif button_id == "permissions-save":
            self.action_save()


__all__ = ["PermissionEntry", "PermissionsResult", "PermissionsScreen"]
