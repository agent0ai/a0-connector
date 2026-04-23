from __future__ import annotations

from typing import Mapping, Sequence

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.message import Message
from textual.reactive import reactive
from textual.widgets import Static


def _profile_item_label(profile: Mapping[str, object], *, active: bool) -> Text:
    key = str(profile.get("key") or "").strip()
    label = str(profile.get("label") or key).strip() or key
    marker = "*" if active else "-"

    text = Text()
    text.append(marker, style="#00b4ff" if active else "#7f8c98")
    text.append(f" {label}", style="#9ecfff" if active else "#d9e2ec")
    if key and key != label:
        text.append(f"  /{key}", style="#7f8c98")
    return text


class ProfileMenuItem(Static):
    can_focus = True
    disabled = reactive(False)

    class Selected(Message):
        def __init__(self, profile_key: str) -> None:
            super().__init__()
            self.profile_key = profile_key

    def __init__(
        self,
        label: str | Text,
        *,
        profile_key: str,
        disabled: bool = False,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(label, id=id, classes=classes)
        self.profile_key = profile_key
        self.disabled = disabled
        self.can_focus = not disabled
        if disabled:
            self.add_class("-disabled")

    def watch_disabled(self, disabled: bool) -> None:
        self.can_focus = not disabled
        if disabled:
            self.add_class("-disabled")
        else:
            self.remove_class("-disabled")

    def on_click(self, event: events.Click) -> None:
        event.stop()
        self._select()

    def on_key(self, event) -> None:
        if event.key == "escape":
            event.prevent_default()
            event.stop()
            self.post_message(ProfileMenuPopover.DismissRequested())
            return
        if event.key in {"enter", "space"}:
            event.prevent_default()
            event.stop()
            self._select()

    def _select(self) -> None:
        if self.disabled:
            return
        self.post_message(self.Selected(self.profile_key))


class ProfileMenuPopover(Vertical):
    BINDINGS = [Binding("escape", "dismiss", "Cancel", show=False)]

    class DismissRequested(Message):
        pass

    def __init__(
        self,
        profiles: Sequence[Mapping[str, object]] | None = None,
        *,
        current_profile: str = "",
        id: str | None = None,
    ) -> None:
        super().__init__(id=id)
        self._profiles = [
            {
                "key": str(profile.get("key") or "").strip(),
                "label": str(profile.get("label") or profile.get("key") or "").strip(),
            }
            for profile in profiles or ()
            if str(profile.get("key") or "").strip()
        ]
        self._current_profile = current_profile.strip()

    def compose(self) -> ComposeResult:
        yield Static("Agent Profile", id="profile-menu-title")
        current_label = next(
            (
                str(profile.get("label") or profile.get("key") or "").strip()
                for profile in self._profiles
                if str(profile.get("key") or "").strip() == self._current_profile
            ),
            self._current_profile,
        )
        if current_label:
            yield Static(f"Current: {current_label}", id="profile-menu-current")
        with VerticalScroll(id="profile-menu-items"):
            if self._profiles:
                for profile in self._profiles:
                    key = str(profile.get("key") or "").strip()
                    active = key == self._current_profile
                    yield ProfileMenuItem(
                        _profile_item_label(profile, active=active),
                        profile_key=key,
                        disabled=active,
                        id=f"profile-menu-item-{key}",
                        classes="profile-menu-item",
                    )
            else:
                yield Static("No agent profiles available.", id="profile-menu-empty")

    def action_dismiss(self) -> None:
        self.post_message(self.DismissRequested())

    def focus_first_item(self) -> None:
        for item in self.query(ProfileMenuItem):
            if not item.disabled:
                item.focus()
                break
