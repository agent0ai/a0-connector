from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from rich.console import Group
from rich.text import Text
from textual.app import ComposeResult
from textual.message import Message
from textual.widgets import OptionList
from textual.widgets._option_list import Option
from textual.containers import Vertical
from textual.widgets import Static


@dataclass(frozen=True)
class SlashCommand:
    canonical: str
    aliases: tuple[str, ...] = ()
    description: str = ""
    enabled: bool = True
    disabled_reason: str = ""


class SlashCommandMenu(Vertical):
    """Suggestion menu for slash commands beneath the chat input."""

    DEFAULT_CSS = """
    SlashCommandMenu {
        layout: vertical;
    }
    """

    class CommandHighlighted(Message):
        def __init__(self, command: SlashCommand | None) -> None:
            super().__init__()
            self.command = command

    class CommandSelected(Message):
        def __init__(self, command: SlashCommand | None) -> None:
            super().__init__()
            self.command = command

    def __init__(self, commands: Sequence[SlashCommand] | None = None) -> None:
        super().__init__(id="slash-menu")
        self._title = Static("Slash commands", id="slash-menu-title")
        self._help = Static(
            "Up/Down move  Tab inserts  Enter runs  Esc closes",
            id="slash-menu-help",
        )
        self._list = OptionList(id="slash-menu-list", compact=True)
        self._commands: list[SlashCommand] = []
        self._placeholder = SlashCommand(
            canonical="/help",
            description="Type a slash command to see suggestions.",
            enabled=False,
        )
        if commands is not None:
            self.set_commands(commands)
        else:
            self._apply_commands([])

    def compose(self) -> ComposeResult:
        yield self._title
        yield self._list
        yield self._help

    def focus(self) -> None:
        self._list.focus()

    def _build_prompt(self, command: SlashCommand) -> Text | Group:
        canonical = Text(command.canonical, style="bold")
        alias_line = ""
        if command.aliases:
            alias_line = "Aliases: " + ", ".join(command.aliases)
        description = command.description or ""
        extra_lines: list[Text] = []
        if alias_line:
            extra_lines.append(Text(alias_line, style="dim"))
        if description:
            extra_lines.append(Text(description, style="dim"))
        if not command.enabled and command.disabled_reason:
            extra_lines.append(Text(command.disabled_reason, style="yellow"))
        if extra_lines:
            return Group(canonical, *extra_lines)
        return canonical

    def _apply_commands(self, commands: Sequence[SlashCommand], *, highlighted: str | int | None = None) -> None:
        self._commands = list(commands)
        if not self._commands:
            self._list.set_options([Option(self._build_prompt(self._placeholder), id=self._placeholder.canonical, disabled=True)])
            self._list.highlighted = None
            return

        options = [
            Option(self._build_prompt(command), id=command.canonical, disabled=not command.enabled)
            for command in self._commands
        ]
        self._list.set_options(options)
        self.set_highlighted(highlighted)

    def set_commands(
        self,
        commands: Sequence[SlashCommand],
        *,
        highlighted: str | int | None = None,
    ) -> None:
        self._apply_commands(commands, highlighted=highlighted)

    def set_visible_commands(
        self,
        commands: Sequence[SlashCommand],
        *,
        highlighted: str | int | None = None,
    ) -> None:
        self._apply_commands(commands, highlighted=highlighted)

    def set_highlighted(self, highlighted: str | int | None) -> None:
        if not self._commands:
            self._list.highlighted = None
            return

        if highlighted is None:
            self._list.action_first()
            return

        if isinstance(highlighted, int):
            self._list.highlighted = max(0, min(highlighted, len(self._commands) - 1))
            return

        for index, command in enumerate(self._commands):
            aliases = {alias.lower() for alias in command.aliases}
            if highlighted == command.canonical or highlighted.lower() in aliases:
                self._list.highlighted = index
                return

        self._list.action_first()

    @property
    def visible_commands(self) -> tuple[SlashCommand, ...]:
        return tuple(self._commands)

    @property
    def highlighted_command(self) -> SlashCommand | None:
        highlighted = self._list.highlighted
        if highlighted is None:
            return None
        if highlighted < 0 or highlighted >= len(self._commands):
            return None
        return self._commands[highlighted]

    def action_cursor_up(self) -> None:
        self._list.action_cursor_up()

    def action_cursor_down(self) -> None:
        self._list.action_cursor_down()

    def action_first(self) -> None:
        self._list.action_first()

    def action_last(self) -> None:
        self._list.action_last()

    def action_select(self) -> None:
        self._list.action_select()

    def on_option_list_option_highlighted(self, _event: OptionList.OptionHighlighted) -> None:
        self.post_message(self.CommandHighlighted(self.highlighted_command))

    def on_option_list_option_selected(self, _event: OptionList.OptionSelected) -> None:
        self.post_message(self.CommandSelected(self.highlighted_command))

