from __future__ import annotations

from textual.command import CommandPalette, DiscoveryHit, Hit, Provider


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
    """Command palette with a tighter header and no leading search icon."""

    DEFAULT_CSS = CommandPalette.DEFAULT_CSS + """
    AgentCommandPalette > Vertical {
        margin-top: 1;
    }

    AgentCommandPalette SearchIcon {
        display: none;
        width: 0;
        margin: 0;
    }

    AgentCommandPalette #--input {
        min-height: 3;
    }
    """
