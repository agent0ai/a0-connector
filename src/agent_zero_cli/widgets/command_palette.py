from __future__ import annotations

import posixpath
from typing import Any, Mapping

from rich.text import Text
from textual import on
from textual.app import ScreenStackError
from textual.command import CommandPalette, DiscoveryHit, Hit, Provider
from textual.widgets import Button, Input

from agent_zero_cli import project_commands
from agent_zero_cli.project_utils import display_project_title, normalize_project_list, project_name


def reference_query_at_cursor(value: str, cursor_index: int) -> tuple[str, int, int] | None:
    text = str(value or "")
    cursor = max(0, min(len(text), cursor_index))
    before = text[:cursor]
    token_start = cursor
    while token_start > 0 and not before[token_start - 1].isspace():
        token_start -= 1
    token = before[token_start:]
    if not token.startswith("@") or "@" in token[1:]:
        return None
    if token.startswith("@[") and token.endswith("]"):
        return None
    return token, token_start, cursor


def _reference_file_directory(query: str) -> str | None:
    value = str(query or "").lstrip("@").removeprefix("./")
    if value.startswith(("/", "agent/", "skill/", "mcp/")) or ".." in value.split("/"):
        return None
    return value.rpartition("/")[0]


def _container_reference_directory(query: str, root: str) -> str | None:
    value = str(query or "").lstrip("@")
    if value == "/":
        return ""
    if not value.startswith("/"):
        return None

    normalized_root = posixpath.normpath(str(root or "").replace("\\", "/"))
    requested = posixpath.normpath(value.replace("\\", "/"))
    if not normalized_root.startswith("/") or ".." in value.split("/"):
        return None
    try:
        if posixpath.commonpath((normalized_root, requested)) != normalized_root:
            return None
    except ValueError:
        return None
    return "" if requested == normalized_root else requested.removeprefix(normalized_root + "/")


def _mcp_policy_allows(policy: Mapping[str, Any] | None, tool_id: str) -> bool:
    if not isinstance(policy, Mapping) or policy.get("mode") != "custom":
        return True
    if tool_id in policy.get("blocked", []):
        return False
    if tool_id in policy.get("allowed", []):
        return True
    return policy.get("mcp_default") == "allow"


def _scoped_reference_catalog(
    profiles: list[Mapping[str, Any]],
    state: Mapping[str, Any] | None,
) -> list[tuple[str, str, str]]:
    items: list[tuple[str, str, str]] = []
    for profile in profiles:
        key = str(profile.get("id") or "").strip()
        label = str(profile.get("title") or key).strip()
        if key and key != "default" and profile.get("enabled") and profile.get("available"):
            items.append((f"@[agent/{key}]", f"Agent profile · {label}", f"agent/{key} {label}".casefold()))

    skills_state = state.get("skills") if isinstance(state, Mapping) else None
    skills = skills_state.get("catalog", []) if isinstance(skills_state, Mapping) else []
    for skill in skills if isinstance(skills, list) else []:
        if not isinstance(skill, Mapping) or skill.get("available") is False or skill.get("hidden"):
            continue
        name = str(skill.get("name") or "").strip()
        if not name:
            continue
        description = str(skill.get("description") or "").strip()
        path = str(skill.get("path") or "").strip()
        items.append((f"@[skill/{name}]", description or "Skill", f"skill/{name} {description} {path}".casefold()))

    tools_state = state.get("tools") if isinstance(state, Mapping) else None
    tools = tools_state.get("catalog", []) if isinstance(tools_state, Mapping) else []
    policy = tools_state.get("effective_policy") if isinstance(tools_state, Mapping) else None
    servers: dict[str, int] = {}
    for tool in tools if isinstance(tools, list) else []:
        if not isinstance(tool, Mapping) or tool.get("available") is False:
            continue
        tool_id = str(tool.get("id") or "")
        if not tool_id.startswith("mcp:") or not _mcp_policy_allows(policy, tool_id):
            continue
        parts = tool_id.split(":", maxsplit=2)
        if len(parts) != 3 or not parts[1]:
            continue
        server = parts[1]
        servers[server] = servers.get(server, 0) + 1
    for server, count in servers.items():
        description = f"{count} available MCP tool{'s' if count != 1 else ''}"
        items.append((f"@[mcp/{server}]", description, f"mcp/{server} {server} {description}".casefold()))
    return items


class OrderedSystemCommandsProvider(Provider):
    """Expose app system commands without Textual's default discovery sorting."""

    async def discover(self):
        await self.app._load_server_commands(force=True)
        for title, help_text, callback, discover in self.app.get_system_commands(self.screen):
            if discover:
                yield DiscoveryHit(title, callback, help=help_text)

    async def search(self, query: str):
        normalized = str(query or "").strip()
        if normalized.startswith("/"):
            await self.app._load_server_commands(force=normalized == "/")

        async for hit in self._search_reference_targets(query):
            yield hit

        async for hit in self._search_skill_targets(query):
            yield hit

        async for hit in self._search_project_targets(query):
            yield hit

        async for hit in self._search_browser_targets(query):
            yield hit

        if normalized == "/":
            score = 1_000_000
            for title, help_text, callback, *_ in self.app.get_system_commands(self.screen):
                if title.startswith("/"):
                    yield Hit(score, title, callback, help=help_text)
                    score -= 1
            return

        matcher = self.matcher(query)
        for title, help_text, callback, *_ in self.app.get_system_commands(self.screen):
            if (match := matcher.match(title)) > 0:
                yield Hit(match, matcher.highlight(title), callback, help=help_text)

    async def _search_reference_targets(self, query: str):
        normalized = str(query or "").strip()
        if not normalized.startswith("@"):
            return

        body = normalized[1:].casefold()
        items: list[tuple[str, str, str]] = []
        directory = _reference_file_directory(normalized)
        if directory is not None:
            try:
                entries = self.app._remote_files.list_reference_entries(directory)
            except (OSError, PermissionError):
                entries = []
            for entry in entries:
                path = str(entry.get("path") or "").replace("\\", "/").strip("/")
                if not path:
                    continue
                is_dir = bool(entry.get("is_dir"))
                display = f"./{path}{'/' if is_dir else ''}"
                items.append((f"@[{display}]", "Folder in local workspace" if is_dir else "File in local workspace", display.casefold()))

        if normalized == "@" or normalized.startswith("@/"):
            try:
                root = await self.app.client.get_chat_files_path(self.app.current_context or "")
                container_directory = "" if normalized == "@" else _container_reference_directory(normalized, root)
                entries = await self.app.client.list_container_reference_entries(root, container_directory) if container_directory is not None else []
            except Exception:
                entries = []
            for entry in entries:
                path = str(entry.get("path") or "").replace("\\", "/")
                if not path:
                    continue
                is_dir = bool(entry.get("is_dir"))
                display = f"{path}{'/' if is_dir else ''}"
                items.append((f"@[{display}]", "Folder in active container workspace" if is_dir else "File in active container workspace", display.casefold()))

        if not body.startswith(("./", "/")) and "agent_editor" in self.app.connector_features:
            try:
                profiles_payload = await self.app.client.agent_editor(
                    "list", context_id=self.app.current_context or ""
                )
                profiles = profiles_payload.get("profiles", []) if profiles_payload.get("ok") else []
                chat = await self.app.client.get_chat(self.app.current_context or "")
                active_profile = str(chat.get("agent_profile") or "").strip()
                active = next(
                    (
                        profile for profile in profiles
                        if isinstance(profile, Mapping)
                        and profile.get("id") == active_profile
                        and profile.get("enabled")
                        and profile.get("available")
                    ),
                    None,
                )
                state_payload = await self.app.client.agent_editor(
                    "load", context_id=self.app.current_context or "", profile_id=active_profile
                ) if active else {}
                state = state_payload.get("state") if state_payload.get("ok") else None
                items.extend(_scoped_reference_catalog(
                    [profile for profile in profiles if isinstance(profile, Mapping)], state
                ))
            except Exception:
                pass

        matcher = self.matcher(normalized)
        score = 1_000_000
        trigger_range = getattr(self.app, "_reference_palette_range", None)
        for title, help_text, search_text in items:
            if body:
                match = matcher.match(title)
                if match <= 0 and body not in search_text:
                    continue
                if match <= 0:
                    match = len(body)
                display_title = Text(title)
            else:
                match = score
                display_title = Text(title)
                score -= 1
            yield Hit(
                match,
                display_title,
                lambda title=title, trigger_range=trigger_range: self.app._insert_reference(
                    title,
                    trigger_range,
                ),
                text=title,
                help=help_text,
            )

    async def _search_browser_targets(self, query: str):
        normalized = str(query or "").strip().lower()
        if normalized != "/browser":
            return

        for title, help_text, callback, *_ in self.app.get_system_commands(self.screen):
            if title.startswith("Browser: "):
                yield Hit(1_000_000, title, callback, help=help_text)

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

    async def _search_skill_targets(self, query: str):
        normalized = str(query or "").strip()
        if not normalized.startswith("$"):
            return
        if not getattr(self.app, "_skills_available", lambda: False)():
            return

        try:
            skills = await self.app._load_skill_palette_skills()
        except Exception:
            return
        if not skills:
            return

        skill_query = normalized[1:].strip().casefold()
        matcher = self.matcher(normalized)
        score = 1_000_000

        for skill in skills:
            name = self.app._skill_display_name(skill)
            if not name:
                continue

            title = f"${name}"
            help_text = self.app._skill_help_text(skill)
            if not skill_query:
                match = score
                display_title = title
                score -= 1
            else:
                match = matcher.match(title)
                display_title = matcher.highlight(title) if match > 0 else title
                if match <= 0 and skill_query not in self.app._skill_search_text(skill):
                    continue
                if match <= 0:
                    match = len(skill_query)

            worker_name = f"palette-skill-{self.app._command_worker_slug(name)}"
            yield Hit(
                match,
                display_title,
                lambda skill=skill, worker_name=worker_name: self.app.run_worker(
                    self.app._activate_skill(skill),
                    exclusive=True,
                    name=worker_name,
                ),
                help=help_text,
            )


def is_raw_slash_command(value: str) -> bool:
    raw = str(value or "").strip()
    return raw.startswith("/") and bool(raw.split(maxsplit=1)[0].strip()) and " " in raw


def is_raw_skill_command(value: str) -> bool:
    raw = str(value or "").strip()
    if not raw.startswith("$") or raw == "$":
        return False
    token = raw[1:].split(maxsplit=1)[0].strip()
    return bool(token) and token[0].isalpha()


class AgentCommandPalette(CommandPalette):
    """Command palette with slash-first styling and optional seeded query."""

    def __init__(
        self,
        *args,
        initial_query: str = "",
        from_slash: bool = False,
        from_skill: bool = False,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._initial_query = initial_query
        self._from_slash = from_slash
        self._from_skill = from_skill

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

    @on(Input.Submitted)
    @on(Button.Pressed)
    def _select_or_command(self, event: Input.Submitted | Button.Pressed | None = None) -> None:
        if event is not None:
            event.stop()

        input_widget = self.query_one(Input)
        raw_command = input_widget.value.strip()
        if self._from_slash and is_raw_slash_command(raw_command):
            self._cancel_gather_commands()

            token = raw_command.split(maxsplit=1)[0].strip().lower().lstrip("/") or "command"
            worker_name = f"slash-{token.replace('/', '-')}"
            self._close_and_call_later(
                lambda: self.app._run_dispatch_command(raw_command, worker_name=worker_name)
            )
            return

        if self._from_skill and is_raw_skill_command(raw_command):
            self._cancel_gather_commands()

            token = raw_command[1:].split(maxsplit=1)[0].strip().lower() or "skill"
            worker_name = f"skill-{self.app._command_worker_slug(token)}"
            self._close_and_call_later(
                lambda: self.app._run_skill_command(raw_command, worker_name=worker_name)
            )
            return

        if event is None and self._selected_command is not None:
            self._cancel_gather_commands()
            self._close_and_call_later(self._selected_command.command)
            return

        super()._select_or_command(event)

    def _close_and_call_later(self, callback) -> None:
        self.app.post_message(CommandPalette.Closed(option_selected=True))
        self.app.delay_update()
        try:
            self.dismiss()
        except ScreenStackError:
            pass
        self.app.call_later(callback)
