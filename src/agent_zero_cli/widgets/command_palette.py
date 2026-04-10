from __future__ import annotations

from textual.command import CommandPalette, DiscoveryHit, Hit, Provider
from textual.widgets import Input


class OrderedSystemCommandsProvider(Provider):
    """Expose app system commands without Textual's default discovery sorting."""

    async def discover(self):
        for title, help_text, callback, discover in self.app.get_system_commands(self.screen):
            if discover:
                yield DiscoveryHit(title, callback, help=help_text)

    async def search(self, query: str):
        matcher = self.matcher(query)
        for title, help_text, callback, *_ in self.app.get_system_commands(self.screen):
            if (match := matcher.match(title)) > 0:
                yield Hit(match, matcher.highlight(title), callback, help=help_text)


class AgentCommandPalette(CommandPalette):
    """Command palette with slash-first styling and optional seeded query."""

    def __init__(self, *args, initial_query: str = "", **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._initial_query = initial_query

    DEFAULT_CSS = CommandPalette.DEFAULT_CSS + """
    AgentCommandPalette > Vertical {
        margin-top: 0;
        background: transparent;
    }

    AgentCommandPalette SearchIcon {
        display: none;
        width: 0;
        margin: 0;
    }

    AgentCommandPalette #--input {
        min-height: 1;
        border: none;
        padding: 0;
        margin: 0;
    }

    AgentCommandPalette #--results {
        margin-top: 0;
    }

    AgentCommandPalette CommandList {
        border: none;
        background: transparent;
        max-height: 12;
    }

    AgentCommandPalette CommandList > .option-list--option {
        padding: 0 1;
    }
    """

    def on_mount(self) -> None:
        if self._initial_query:
            self.call_after_refresh(self._apply_initial_query)

    def _apply_initial_query(self) -> None:
        input_widget = self.query_one(Input)
        input_widget.value = self._initial_query
        input_widget.action_end()
