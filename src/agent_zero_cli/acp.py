"""Agent Client Protocol adapter hosted by the A0 CLI connector."""
from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass, field
import getpass
import logging
import os
from pathlib import Path
import sys
import uuid
from typing import Any

import acp
from acp.schema import (
    AgentCapabilities,
    AvailableCommand,
    AvailableCommandsUpdate,
    CloseSessionResponse,
    CurrentModeUpdate,
    ForkSessionResponse,
    Implementation,
    InitializeResponse,
    ListSessionsResponse,
    LoadSessionResponse,
    NewSessionResponse,
    PromptCapabilities,
    PromptResponse,
    ResumeSessionResponse,
    SessionCapabilities,
    SessionCloseCapabilities,
    SessionForkCapabilities,
    SessionInfo,
    SessionListCapabilities,
    SessionMode,
    SessionModeState,
    SessionResumeCapabilities,
    SetSessionConfigOptionResponse,
    SetSessionModelResponse,
    SetSessionModeResponse,
    TerminalAuthMethod,
)

from agent_zero_cli import __version__
from agent_zero_cli.attachments import AttachmentUpload
from agent_zero_cli.client import A0Client, A0ProtocolError, DEFAULT_HOST
from agent_zero_cli.config import CLIConfig, load_config
from agent_zero_cli.instance_discovery import discover_local_instances
from agent_zero_cli.session import ConnectorSession, SessionError, SessionObserver


logger = logging.getLogger(__name__)
_MODE_PROMPTS = {
    "default": ("Default", "Use the normal Agent Zero behavior."),
    "plan": ("Plan", "Prefer planning and analysis before changing files."),
    "act": ("Act", "Complete actionable work with focused implementation and validation."),
}


@dataclass(frozen=True)
class AcpOptions:
    host: str = ""
    workspace: Path = Path(".")
    discover_instances: bool = True
    check: bool = False
    login: bool = False
    debug: bool = False
    transport: str = ""
    container_id: str = ""


@dataclass
class _SessionState:
    connector: ConnectorSession
    config: dict[str, Any]
    replay_history: bool = False
    final_text: str = ""
    failure: str = ""
    cancelled: bool = False
    completed: asyncio.Event = field(default_factory=asyncio.Event)

    def begin_turn(self) -> None:
        self.final_text = ""
        self.failure = ""
        self.cancelled = False
        self.completed.clear()


class _Observer(SessionObserver):
    def __init__(self, agent: "AgentZeroACPAgent") -> None:
        self.agent = agent

    def on_stage(self, stage: str, message: str, detail: str = "") -> None:
        logger.debug("ACP connector %s: %s %s", stage, message, detail)

    def on_event(self, event: dict[str, Any]) -> None:
        asyncio.create_task(self.agent._handle_event(event))

    def on_snapshot(self, events: list[dict[str, Any]], queue: list[dict[str, Any]]) -> None:
        del queue
        asyncio.create_task(self.agent._handle_snapshot(events))

    def on_complete(self, context_id: str) -> None:
        asyncio.create_task(self.agent._complete(context_id))

    def on_error(self, code: str, message: str) -> None:
        asyncio.create_task(self.agent._fail_active_session(f"{code}: {message}"))

    def on_disconnect(self) -> None:
        asyncio.create_task(self.agent._fail_active_session("Agent Zero connector disconnected."))


class AgentZeroACPAgent:
    def __init__(self, options: AcpOptions, config: CLIConfig) -> None:
        self.options = options
        self.config = config
        self.host = ""
        self._conn: acp.Client | None = None
        self._sessions: dict[str, _SessionState] = {}

    def on_connect(self, conn: acp.Client) -> None:
        self._conn = conn

    async def initialize(self, client_capabilities: Any = None, **_: Any) -> InitializeResponse:
        self.host = await _resolve_host(self.options, self.config)
        return InitializeResponse(
            protocol_version=acp.PROTOCOL_VERSION,
            agent_info=Implementation(name="agent-zero", title="Agent Zero", version=__version__),
            agent_capabilities=AgentCapabilities(
                auth=None,
                load_session=True,
                prompt_capabilities=PromptCapabilities(embedded_context=True),
                session_capabilities=SessionCapabilities(
                    close=SessionCloseCapabilities(),
                    fork=SessionForkCapabilities(),
                    list=SessionListCapabilities(),
                    resume=SessionResumeCapabilities(),
                ),
            ),
            auth_methods=_auth_methods(client_capabilities),
        )

    async def new_session(
        self,
        cwd: str,
        additional_directories: list[str] | None = None,
        **_: Any,
    ) -> NewSessionResponse:
        state = await self._connect_state(cwd)
        client = _require_client(state)
        profile = str(state.config.get("agent_profile") or "").strip() or None
        context_id = await client.create_chat(agent_profile=profile)
        await self._activate(state, context_id, cwd, additional_directories)
        return NewSessionResponse(session_id=context_id, modes=_modes("default"))

    async def load_session(
        self,
        cwd: str,
        session_id: str,
        additional_directories: list[str] | None = None,
        **_: Any,
    ) -> LoadSessionResponse | None:
        state = await self._connect_state(cwd)
        try:
            mode = await self._activate(state, session_id, cwd, additional_directories, replay=True)
        except Exception:
            await state.connector.close()
            return None
        return LoadSessionResponse(modes=_modes(mode))

    async def resume_session(
        self,
        cwd: str,
        session_id: str,
        additional_directories: list[str] | None = None,
        **_: Any,
    ) -> ResumeSessionResponse:
        state = await self._connect_state(cwd)
        mode = await self._activate(state, session_id, cwd, additional_directories, replay=True)
        return ResumeSessionResponse(modes=_modes(mode))

    async def fork_session(
        self,
        cwd: str,
        session_id: str,
        additional_directories: list[str] | None = None,
        **_: Any,
    ) -> ForkSessionResponse:
        source = self._sessions.get(session_id)
        if source is None:
            return ForkSessionResponse(session_id="")
        response = await _require_client(source).acp_session(
            "fork",
            context_id=session_id,
            cwd=cwd,
            additional_directories=list(additional_directories or []),
        )
        session = response.get("session") if isinstance(response, dict) else {}
        fork_id = str(session.get("session_id") or "") if isinstance(session, dict) else ""
        if not fork_id:
            return ForkSessionResponse(session_id="")
        state = await self._connect_state(cwd)
        mode = await self._activate(state, fork_id, cwd, additional_directories, replay=True)
        return ForkSessionResponse(session_id=fork_id, modes=_modes(mode))

    async def list_sessions(self, cwd: str | None = None, **_: Any) -> ListSessionsResponse:
        try:
            state = await self._connect_state(cwd or str(self.options.workspace))
        except Exception:
            return ListSessionsResponse(sessions=[])
        try:
            if not bool(state.config.get("session_history", True)):
                return ListSessionsResponse(sessions=[])
            response = await _require_client(state).acp_session("list", cwd=cwd or "")
            records = response.get("sessions") if isinstance(response, dict) else []
            sessions = [_session_info(record) for record in records if isinstance(record, dict)]
            return ListSessionsResponse(sessions=sessions)
        except A0ProtocolError:
            return ListSessionsResponse(sessions=[])
        finally:
            await state.connector.close()

    async def prompt(
        self,
        prompt: list[Any],
        session_id: str,
        message_id: str | None = None,
        **_: Any,
    ) -> PromptResponse:
        state = self._sessions.get(session_id)
        if state is None:
            return PromptResponse(stop_reason="refusal", user_message_id=message_id)
        if state.connector.agent_active:
            return PromptResponse(stop_reason="refusal", user_message_id=message_id)

        text, uploads = _prompt_parts(prompt)
        if not text and not uploads:
            return PromptResponse(stop_reason="end_turn", user_message_id=message_id)
        client = _require_client(state)
        attachments = await client.upload_attachments(uploads) if uploads else []
        state.begin_turn()
        await state.connector.send_message(text, [attachment.path for attachment in attachments])
        await state.completed.wait()
        if state.cancelled:
            return PromptResponse(stop_reason="cancelled", user_message_id=message_id)
        if state.failure:
            await self._send_text(session_id, f"Error: {state.failure}")
        return PromptResponse(stop_reason="end_turn", user_message_id=message_id)

    async def cancel(self, session_id: str, **_: Any) -> None:
        state = self._sessions.get(session_id)
        if state is None:
            return
        state.cancelled = True
        await state.connector.pause()
        state.completed.set()

    async def close_session(self, session_id: str, **_: Any) -> CloseSessionResponse:
        state = self._sessions.pop(session_id, None)
        if state is None:
            return CloseSessionResponse()
        try:
            await _require_client(state).acp_session("close", context_id=session_id)
        finally:
            await state.connector.close()
        return CloseSessionResponse()

    async def set_session_mode(self, mode_id: str, session_id: str, **_: Any) -> SetSessionModeResponse | None:
        state = self._sessions.get(session_id)
        if state is None:
            return None
        mode = mode_id if mode_id in _MODE_PROMPTS else "default"
        try:
            await _require_client(state).acp_session("set_mode", context_id=session_id, mode=mode)
        except A0ProtocolError:
            return None
        await self._send_update(session_id, CurrentModeUpdate(session_update="current_mode_update", current_mode_id=mode))
        return SetSessionModeResponse()

    async def set_session_model(self, model_id: str, session_id: str, **_: Any) -> SetSessionModelResponse | None:
        state = self._sessions.get(session_id)
        if state is None:
            return None
        try:
            await _require_client(state).acp_session("set_model", context_id=session_id, model_id=model_id)
        except A0ProtocolError:
            return None
        return SetSessionModelResponse()

    async def set_config_option(
        self,
        config_id: str,
        session_id: str,
        value: str | bool,
        **_: Any,
    ) -> SetSessionConfigOptionResponse | None:
        state = self._sessions.get(session_id)
        if state is None:
            return None
        try:
            await _require_client(state).acp_session(
                "set_config_option", context_id=session_id, config_id=config_id, value=value
            )
        except A0ProtocolError:
            return None
        return SetSessionConfigOptionResponse(config_options=[])

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        del params
        return {"ok": True} if method in {"ping", "health", "healthcheck"} else {}

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        del method, params

    async def _connect_state(self, cwd: str) -> _SessionState:
        workspace = Path(cwd or self.options.workspace).expanduser().resolve()
        observer = _Observer(self)
        session = ConnectorSession(
            self.config,
            observer,
            workspace=workspace,
            defer_context=True,
            remember_context=False,
        )
        try:
            await session.connect(
                self.host or await _resolve_host(self.options, self.config),
                username=os.environ.get("A0_USERNAME", "").strip(),
                password=os.environ.get("A0_PASSWORD", ""),
                restore_session=True,
            )
            config = await _acp_config(_require_client(session))
            if not bool(config.get("enabled", True)):
                raise RuntimeError("ACP is disabled in Agent Zero settings.")
            writable = str(config.get("host_file_access") or "read_write") == "read_write"
            session.remote_file_write_enabled = writable
            session.remote_exec_enabled = writable and bool(config.get("host_code_execution", True))
            session.remote_files.set_write_enabled(writable)
            session.remote_exec.set_write_enabled(writable)
            session.remote_exec.set_enabled(session.remote_exec_enabled)
            await session.refresh_remote_tool_metadata()
            return _SessionState(connector=session, config=config)
        except Exception:
            await session.close()
            raise

    async def _activate(
        self,
        state: _SessionState,
        session_id: str,
        cwd: str,
        additional_directories: list[str] | None,
        *,
        replay: bool = False,
    ) -> str:
        client = _require_client(state)
        mode = "default"
        try:
            response = await client.acp_session(
                "configure",
                context_id=session_id,
                cwd=cwd,
                additional_directories=list(additional_directories or []),
                mode=mode,
            )
            record = response.get("session") if isinstance(response, dict) else {}
            if isinstance(record, dict):
                mode = str(record.get("mode") or "default")
        except A0ProtocolError:
            logger.info("Using compatibility ACP session without the built-in _a0_acp endpoint")
        state.replay_history = replay
        self._sessions[session_id] = state
        try:
            await state.connector.switch_context(session_id, has_messages_hint=replay)
        except Exception:
            self._sessions.pop(session_id, None)
            raise
        await self._send_session_start_updates(session_id, mode)
        return mode

    async def _send_session_start_updates(self, session_id: str, mode: str) -> None:
        commands = [
            AvailableCommand(name="help", description="Show Agent Zero ACP commands"),
            AvailableCommand(name="reset", description="Clear the active Agent Zero chat"),
            AvailableCommand(name="version", description="Show the A0 CLI version"),
        ]
        await self._send_update(
            session_id,
            AvailableCommandsUpdate(session_update="available_commands_update", available_commands=commands),
        )
        await self._send_update(
            session_id,
            CurrentModeUpdate(session_update="current_mode_update", current_mode_id=mode),
        )

    async def _handle_snapshot(self, events: list[dict[str, Any]]) -> None:
        if not events:
            return
        context_id = str(events[0].get("context_id") or "")
        state = self._sessions.get(context_id)
        if state is None or not state.replay_history:
            return
        for event in events:
            await self._handle_event(event, historical=True)
        state.replay_history = False

    async def _handle_event(self, event: dict[str, Any], *, historical: bool = False) -> None:
        context_id = str(event.get("context_id") or "")
        state = self._sessions.get(context_id)
        if state is None:
            return
        event_type = str(event.get("event") or "")
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        text = str(data.get("text") or "")
        if event_type == "assistant_message" and text:
            state.final_text = text
            await self._send_text(context_id, text)
            return
        if historical or event_type not in {"tool_start", "tool_output", "code_start", "code_output"}:
            return
        sequence = str(event.get("sequence") or uuid.uuid4().hex)
        title = str(data.get("heading") or event_type.replace("_", " ").title())
        tool_id = f"a0-{context_id}-{sequence}"
        update = acp.start_tool_call(tool_id, title, kind="other", status="completed", raw_output=text or data)
        await self._send_update(context_id, update)

    async def _complete(self, context_id: str) -> None:
        state = self._sessions.get(context_id)
        if state is not None:
            state.completed.set()

    async def _fail_active_session(self, message: str) -> None:
        for state in self._sessions.values():
            if state.connector.agent_active:
                state.failure = message
                state.completed.set()

    async def _send_text(self, session_id: str, text: str) -> None:
        if text:
            await self._send_update(session_id, acp.update_agent_message_text(text))

    async def _send_update(self, session_id: str, update: Any) -> None:
        if self._conn is None:
            return
        try:
            await self._conn.session_update(session_id, update)
        except Exception:
            logger.debug("Could not send ACP update", exc_info=True)


def _require_client(session: ConnectorSession | _SessionState) -> A0Client:
    connector = session.connector if isinstance(session, _SessionState) else session
    if connector.client is None:
        raise RuntimeError("Agent Zero connector is not connected.")
    return connector.client


async def _acp_config(client: A0Client) -> dict[str, Any]:
    try:
        response = await client.acp_session("config")
    except A0ProtocolError:
        return {}
    config = response.get("config") if isinstance(response, dict) else {}
    return dict(config) if isinstance(config, dict) else {}


def _modes(current: str) -> SessionModeState:
    normalized = current if current in _MODE_PROMPTS else "default"
    return SessionModeState(
        current_mode_id=normalized,
        available_modes=[
            SessionMode(id=mode, name=label, description=description)
            for mode, (label, description) in _MODE_PROMPTS.items()
        ],
    )


def _session_info(record: dict[str, Any]) -> SessionInfo:
    return SessionInfo(
        session_id=str(record.get("session_id") or ""),
        cwd=str(record.get("cwd") or ""),
        additional_directories=[str(path) for path in record.get("additional_directories", [])],
        title=str(record.get("title") or "") or None,
        updated_at=str(record.get("updated_at") or "") or None,
    )


def _prompt_parts(prompt: list[Any]) -> tuple[str, list[AttachmentUpload]]:
    text_parts: list[str] = []
    uploads: list[AttachmentUpload] = []
    for index, block in enumerate(prompt):
        block_type = str(getattr(block, "type", "") or "")
        if block_type == "text":
            text_parts.append(str(getattr(block, "text", "") or ""))
            continue
        data = str(getattr(block, "data", "") or "")
        if data:
            try:
                content = base64.b64decode(data.split(",", 1)[-1], validate=False)
            except Exception:
                content = b""
            if content:
                mime_type = str(getattr(block, "mime_type", "") or "application/octet-stream")
                uploads.append(AttachmentUpload(f"acp-{index}", content, mime_type))
                continue
        resource = getattr(block, "resource", None)
        resource_text = str(getattr(resource, "text", "") or "") if resource else ""
        if resource_text:
            text_parts.append(resource_text)
            continue
        uri = str(getattr(block, "uri", "") or "")
        if uri:
            text_parts.append(f"[Attached resource: {uri}]")
    return "\n\n".join(part.strip() for part in text_parts if part.strip()), uploads


async def _resolve_host(options: AcpOptions, config: CLIConfig) -> str:
    if options.host.strip():
        return options.host.strip().rstrip("/")
    if config.instance_url.strip():
        return config.instance_url.strip().rstrip("/")
    if options.discover_instances:
        result = await discover_local_instances()
        if result.status == "ready" and len(result.instances) == 1:
            return result.instances[0].url.rstrip("/")
        if result.status == "ready" and len(result.instances) > 1:
            raise RuntimeError("Multiple local Agent Zero instances found; pass --host.")
    return DEFAULT_HOST


def _auth_methods(client_capabilities: Any) -> list[TerminalAuthMethod]:
    auth = getattr(client_capabilities, "auth", None)
    if not bool(getattr(auth, "terminal", False)):
        return []
    return [
        TerminalAuthMethod(
            id="a0-web-login",
            name="Sign in to Agent Zero",
            description="Sign in with the configured Agent Zero web account.",
            args=["--login"],
            type="terminal",
        )
    ]


async def _login_for_acp(options: AcpOptions, config: CLIConfig) -> int:
    host = await _resolve_host(options, config)
    client = A0Client(host)
    try:
        capabilities = await client.fetch_capabilities()
        if not bool(capabilities.get("auth_required")):
            print("Agent Zero does not require login.")
            return 0

        if client.restore_session(host) and await client.verify_session():
            print("A0 ACP session is already authenticated.")
            return 0
        client.clear_persisted_session(host)

        username = os.environ.get("A0_USERNAME", "").strip()
        password = os.environ.get("A0_PASSWORD", "")
        if not username:
            username = input("Agent Zero username: ").strip()
        if not password:
            password = getpass.getpass("Agent Zero password: ")
        if not username or not password:
            print("Agent Zero username and password are required.", file=sys.stderr)
            return 1
        if not await client.login(username, password):
            print("Agent Zero login failed.", file=sys.stderr)
            return 1

        client.persist_session(host)
        print("A0 ACP login succeeded.")
        return 0
    except Exception as exc:
        logger.error("A0 ACP login failed: %s", exc)
        return 1
    finally:
        await client.disconnect(close_http=True, notify=False)


async def _transport_config(options: AcpOptions, config: CLIConfig) -> tuple[str, dict[str, Any]]:
    host = await _resolve_host(options, config)
    client = A0Client(host)
    try:
        capabilities = await client.fetch_capabilities()
        if bool(capabilities.get("auth_required")):
            client.restore_session(host)
            if not await client.verify_session():
                username = os.environ.get("A0_USERNAME", "").strip()
                password = os.environ.get("A0_PASSWORD", "")
                if not username or not password or not await client.login(username, password):
                    return host, {}
        return host, await _acp_config(client)
    except Exception:
        return host, {}
    finally:
        await client.disconnect(close_http=True, notify=False)


def _container_command(options: AcpOptions, config: dict[str, Any]) -> list[str]:
    container_id = options.container_id or str(config.get("container_id") or "").strip()
    if not container_id:
        raise RuntimeError("Container ACP transport requires hidden _a0_acp.container_id configuration.")
    return [
        "docker",
        "exec",
        "-i",
        "-w",
        str(config.get("container_workdir") or "/a0"),
        container_id,
        str(config.get("container_python") or "/opt/venv-a0/bin/python"),
        "-m",
        "usr.plugins.a0_acp",
    ] + (["--debug"] if options.debug else [])


def _setup_logging(debug: bool) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logging.basicConfig(level=logging.DEBUG if debug else logging.INFO, handlers=[handler], force=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("socketio").setLevel(logging.WARNING)


def run_acp(options: AcpOptions) -> int:
    if options.check:
        print("A0 ACP check OK")
        return 0
    _setup_logging(options.debug)
    config = load_config()
    if options.login:
        return asyncio.run(_login_for_acp(options, config))
    host, remote_config = asyncio.run(_transport_config(options, config))
    transport = options.transport or str(remote_config.get("transport") or "connector")
    if transport == "container":
        os.execvp("docker", _container_command(options, remote_config))
    resolved_options = AcpOptions(
        host=host,
        workspace=options.workspace,
        discover_instances=options.discover_instances,
        debug=options.debug,
    )
    try:
        asyncio.run(acp.run_agent(AgentZeroACPAgent(resolved_options, config), use_unstable_protocol=True))
    except KeyboardInterrupt:
        return 0
    except (RuntimeError, SessionError) as exc:
        logger.error("ACP stopped: %s", exc)
        return 1
    return 0
