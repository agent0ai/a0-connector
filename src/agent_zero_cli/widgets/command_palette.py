from __future__ import annotations

from textual.command import CommandPalette, DiscoveryHit, Hit, Provider
from textual.widgets import Input

from agent_zero_cli import project_commands
from agent_zero_cli.project_utils import display_project_title, normalize_project_list, project_name


class OrderedSystemCommandsProvider(Provider):
    """Expose app system commands without Textual's default discovery sorting."""

    async def discover(self):
        for title, help_text, callback, discover in self.app.get_system_commands(self.screen):
            if discover:
                yield DiscoveryHit(title, callback, help=help_text)

    async def search(self, query: str):
        async for hit in self._search_project_targets(query):
            yield hit

        matcher = self.matcher(query)
        for title, help_text, callback, *_ in self.app.get_system_commands(self.screen):
            if (match := matcher.match(title)) > 0:
                yield Hit(match, matcher.highlight(title), callback, help=help_text)

    async def _search_project_targets(self, query: str):
        token, _, project_query = query.partition(" ")
        if token.lower() not in {"/project", "/projects"} or not project_query.strip():
            return

        availability = self.app._project_availability()
        if not availability.available:
            return

        matcher = self.matcher(query)
        projects = normalize_project_list(getattr(self.app, "project_list", []))
        current_name = project_name(getattr(self.app, "current_project", None))
        for project in projects:
            name = project_name(project)
            if not name or name == current_name:
                continue

            title = display_project_title(project, default=name)
            label = f"/project {title}"
            if name != title:
                label = f"/project {title} ({name})"

            if (match := matcher.match(label)) <= 0:
                continue

            worker_name = f"palette-project-{name.replace('/', '-').replace(' ', '-')}"
            yield Hit(
                match,
                matcher.highlight(label),
                lambda name=name, worker_name=worker_name: self.app.run_worker(
                    project_commands.cmd_project(self.app, query=name),
                    exclusive=True,
                    name=worker_name,
                ),
                help=f"Switch to {title}.",
            )


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
