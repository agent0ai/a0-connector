from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Center, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, ContentSwitcher, Input, SelectionList, Static, TextArea


@dataclass(frozen=True)
class ProfileEditorResult:
    title: str
    instructions: str
    allowed_tools: tuple[str, ...]


def _tool_prompt(tool: Mapping[str, object]) -> Text:
    label = str(tool.get("label") or tool.get("name") or tool.get("id") or "Tool")
    description = str(tool.get("description") or "").strip()
    text = Text(label, style="bold")
    if description:
        text.append(f" — {description}", style="dim")
    return text


class ProfileEditorScreen(ModalScreen[ProfileEditorResult | None]):
    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+s", "save", "Save", show=False),
    ]

    def __init__(
        self,
        *,
        creating: bool,
        title: str = "",
        instructions: str = "",
        tools: Sequence[Mapping[str, object]] = (),
        allowed_tools: Sequence[str] = (),
    ) -> None:
        super().__init__()
        self._creating = creating
        self._title = title
        self._instructions = instructions
        self._tools = tuple(tools)
        self._allowed_tools = set(allowed_tools)
        self._page = "details"

    def compose(self) -> ComposeResult:
        with Center():
            with Vertical(id="profile-editor-box"):
                yield Static(
                    "Create agent" if self._creating else "Edit current profile",
                    id="profile-editor-title",
                )
                yield Static("1 of 2 — Details", id="profile-editor-step")
                with ContentSwitcher(initial="profile-editor-details", id="profile-editor-pages"):
                    with Vertical(id="profile-editor-details"):
                        yield Static("Agent name", classes="profile-editor-label")
                        yield Input(value=self._title, id="profile-editor-name")
                        yield Static("Instructions", classes="profile-editor-label")
                        yield TextArea(
                            self._instructions,
                            language=None,
                            show_line_numbers=False,
                            soft_wrap=True,
                            id="profile-editor-instructions",
                        )
                    with Vertical(id="profile-editor-tools"):
                        yield Static(
                            "Checked tools are allowed; unchecked tools are blocked.",
                            id="profile-editor-tools-help",
                        )
                        yield SelectionList(
                            *[
                                (
                                    _tool_prompt(tool),
                                    str(tool.get("id") or ""),
                                    str(tool.get("id") or "") in self._allowed_tools,
                                )
                                for tool in self._tools
                                if str(tool.get("id") or "")
                            ],
                            id="profile-editor-tool-list",
                        )
                yield Static("", id="profile-editor-status")
                with Horizontal(id="profile-editor-actions"):
                    yield Button("Cancel", id="profile-editor-cancel")
                    yield Button("Back", id="profile-editor-back")
                    yield Button("Next", id="profile-editor-next", variant="primary")
                    yield Button("Save", id="profile-editor-save", variant="primary")

    def on_mount(self) -> None:
        self._sync_page()

    def _sync_page(self) -> None:
        details = self._page == "details"
        self.query_one("#profile-editor-pages", ContentSwitcher).current = (
            "profile-editor-details" if details else "profile-editor-tools"
        )
        self.query_one("#profile-editor-step", Static).update(
            "1 of 2 — Details" if details else "2 of 2 — Tools"
        )
        self.query_one("#profile-editor-back", Button).display = not details
        self.query_one("#profile-editor-next", Button).display = details
        self.query_one("#profile-editor-save", Button).display = not details
        self.query_one(
            "#profile-editor-name" if details else "#profile-editor-tool-list"
        ).focus()

    def _details(self) -> tuple[str, str] | None:
        title = self.query_one("#profile-editor-name", Input).value.strip()
        instructions = self.query_one("#profile-editor-instructions", TextArea).text
        status = self.query_one("#profile-editor-status", Static)
        if not title:
            status.update(Text("Agent name is required.", style="#ff8b6b"))
            return None
        if not instructions.strip():
            status.update(Text("Instructions are required.", style="#ff8b6b"))
            return None
        status.update("")
        return title, instructions

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_back(self) -> None:
        self._page = "details"
        self._sync_page()

    def action_next(self) -> None:
        if self._details() is None:
            return
        self._page = "tools"
        self._sync_page()

    def action_save(self) -> None:
        details = self._details()
        if details is None:
            self._page = "details"
            self._sync_page()
            return
        title, instructions = details
        selected = self.query_one("#profile-editor-tool-list", SelectionList).selected
        self.dismiss(
            ProfileEditorResult(
                title=title,
                instructions=instructions,
                allowed_tools=tuple(str(value) for value in selected),
            )
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        action = (event.button.id or "").removeprefix("profile-editor-")
        if action == "cancel":
            self.action_cancel()
        elif action == "back":
            self.action_back()
        elif action == "next":
            self.action_next()
        elif action == "save":
            self.action_save()


__all__ = ["ProfileEditorResult", "ProfileEditorScreen"]
