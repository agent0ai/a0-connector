from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
import getpass
import json
import os
import re
import signal
import sys
from pathlib import Path, PurePosixPath
from typing import TextIO

from agent_zero_cli.client import DEFAULT_HOST
from agent_zero_cli.config import CLIConfig, load_config
from agent_zero_cli.headless.commands import (
    HeadlessCommandResult,
    command_may_start_agent,
    dispatch_headless_command,
    is_headless_command,
)
from agent_zero_cli.headless.renderer import HeadlessRenderer, make_renderer
from agent_zero_cli.instance_discovery import discover_local_instances
from agent_zero_cli.rendering import completion_notification_sequence
from agent_zero_cli.session import ConnectorSession, SessionError

EXIT_SUCCESS = 0
EXIT_ERROR = 1
EXIT_CONNECT_OR_AUTH = 2
_COMPLETION_SETTLE_SECONDS = 0.2
_COMPLETION_SNAPSHOT_TIMEOUT_SECONDS = 1.0
_TAG_PROFILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_TAG_RESULT_PREFIX = "<!--a0-tag:v1;mode="
_TAG_RESULT_MARKERS = {
    "replace": "<!--a0-tag:v1;mode=replace-->",
    "action": "<!--a0-tag:v1;mode=action-->",
}
_TAG_UPLOAD_ROOT = PurePosixPath("/a0/usr/uploads")


def parse_launcher_tag_result(value: str) -> tuple[str, str, bool, str]:
    text = str(value or "")
    lines = text.splitlines(keepends=True)
    first = lines[0].rstrip("\r\n") if lines else ""
    matches = [mode for mode, marker in _TAG_RESULT_MARKERS.items() if first == marker]
    if len(matches) != 1 or text.count(_TAG_RESULT_PREFIX) != 1:
        return "overlay", text.strip(), False, "INVALID_TAG_RESULT"
    body = "".join(lines[1:])
    if not body.strip():
        return "overlay", "", False, "EMPTY_TAG_RESULT"
    return matches[0], body, True, ""


def normalize_launcher_tag_attachment_ref(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    path = PurePosixPath(raw)
    name = path.name
    if (
        path.parent != _TAG_UPLOAD_ROOT
        or not name
        or name in {".", ".."}
        or len(name) > 255
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name)
    ):
        raise SessionError(
            "INVALID_ATTACHMENT_REF",
            "Launcher-tag attachment must be one Agent Zero upload reference.",
            stage="options",
            exit_code=1,
        )
    return str(path)


def normalize_launcher_tag_attachment_refs(values: object) -> list[str]:
    if values is None:
        return []
    source = values if isinstance(values, (list, tuple)) else [values]
    if len(source) > 129:
        raise SessionError(
            "INVALID_ATTACHMENT_REF",
            "Launcher-tag accepts at most 129 Agent Zero upload references.",
            stage="options",
            exit_code=1,
        )
    refs: list[str] = []
    seen: set[str] = set()
    for value in source:
        ref = normalize_launcher_tag_attachment_ref(str(value or ""))
        if ref and ref not in seen:
            refs.append(ref)
            seen.add(ref)
    return refs


@dataclass
class HeadlessOptions:
    host: str = ""
    chat: str = ""
    chat_last: bool = False
    new_chat: bool = False
    output: str = "text"
    print_prompt: str | None = None
    workspace: Path = field(default_factory=Path.cwd)
    discover_instances: bool = True
    launcher_tag: bool = False
    agent_profile: str = ""
    attachment_refs: list[str] = field(default_factory=list)
    config: CLIConfig | None = None
    stdin: TextIO = field(default_factory=lambda: sys.stdin)
    stdout: TextIO = field(default_factory=lambda: sys.stdout)
    stderr: TextIO = field(default_factory=lambda: sys.stderr)


class HeadlessRunner:
    def __init__(self, options: HeadlessOptions) -> None:
        self.options = options
        self.config = options.config or load_config()
        self.renderer: HeadlessRenderer = make_renderer(
            options.output,
            color=self._stdout_isatty() and options.output == "text",
        )
        self.session: ConnectorSession | None = None
        self._complete_event = asyncio.Event()
        self._stop_event = asyncio.Event()
        self._exit_code = EXIT_SUCCESS
        self._prompt_visible = False
        self._waiting_for_line = False
        self._sigint_count = 0
        self._signal_installed = False
        self._defer_complete_render = False
        self._deferred_complete_context_id = ""
        self._snapshot_event = asyncio.Event()
        self._event_fingerprints: dict[tuple[str, int, str], str] = {}
        self._tag_response_text = ""
        self._tag_response_sequence = -1
        self._tag_attachment_sent = False

    async def run(self) -> int:
        self._install_signal_handlers()
        try:
            self._validate_options()
            host = await self._resolve_host()
            session_options: dict[str, object] = {
                "workspace": self.options.workspace,
                "remote_file_write_enabled": not self.options.launcher_tag,
                "remote_exec_enabled": not self.options.launcher_tag,
            }
            if self.options.launcher_tag:
                session_options.update(
                    remote_files_enabled=False,
                    remember_context=False,
                )
            self.session = ConnectorSession(
                self.config,
                self,
                **session_options,
            )
            connected = await self._connect_with_auth_retry(host)
            if not connected:
                return self._exit_code

            if self.options.print_prompt is not None:
                return await self._run_print_mode()
            if not self._stdin_isatty():
                return await self._run_pipe_mode()
            return await self._run_repl_mode()
        except SessionError as exc:
            self._write_error(exc.code, exc.message)
            return exc.exit_code
        except Exception as exc:
            self._write_error("ERROR", str(exc))
            return EXIT_ERROR
        finally:
            self._remove_signal_handlers()
            if self.session is not None:
                await self.session.close()

    def on_stage(self, stage: str, message: str, detail: str = "") -> None:
        self._write_lines(self.renderer.render_stage(stage, message, detail))

    def on_event(self, event: dict) -> None:
        self._sigint_count = 0
        self._remember_event(event)
        self._remember_tag_response(event)
        if self.options.launcher_tag:
            return
        self._write_lines(self.renderer.render_event(event))

    def on_snapshot(self, events: list[dict], queue: list[dict]) -> None:
        changed_events = [event for event in events if self._snapshot_event_changed(event)]
        for event in changed_events:
            self._remember_tag_response(event)
        if not self.options.launcher_tag:
            self._write_lines(self.renderer.render_snapshot(changed_events, queue))
        self._snapshot_event.set()

    def on_complete(self, context_id: str) -> None:
        self._sigint_count = 0
        if self._complete_event.is_set():
            return
        if self._defer_complete_render:
            self._deferred_complete_context_id = context_id
            self._complete_event.set()
            return
        self._complete_event.set()
        self._render_tag_result(context_id)
        self._write_lines(self.renderer.render_complete(context_id))
        self._notify_terminal_completion()
        if self._stdin_isatty() and self._waiting_for_line and not self._stop_event.is_set():
            self._prompt()

    def on_error(self, code: str, message: str) -> None:
        self._write_error(code, message)
        self._exit_code = EXIT_ERROR

    def on_disconnect(self) -> None:
        if not self._stop_event.is_set():
            self._write_error("DISCONNECTED", "Connection lost.")
            self._exit_code = EXIT_ERROR
            self._stop_event.set()

    async def _connect_with_auth_retry(self, host: str) -> bool:
        username = os.environ.get("A0_USERNAME", "").strip()
        password = os.environ.get("A0_PASSWORD", "")
        prompted = False

        while True:
            try:
                await self._require_session().connect(
                    host,
                    username=username,
                    password=password,
                    context_id=self.options.chat,
                    chat_last=self.options.chat_last,
                    new_chat=self.options.new_chat,
                    new_chat_agent_profile=(
                        self.options.agent_profile if self.options.launcher_tag else ""
                    ),
                    restore_session=True,
                )
                return True
            except SessionError as exc:
                if exc.code != "AUTH_REQUIRED" or prompted or not self._stdin_isatty():
                    self._write_error(exc.code, exc.message)
                    self._exit_code = exc.exit_code
                    return False
                username, password = await self._prompt_for_credentials()
                prompted = True

    async def _run_print_mode(self) -> int:
        text = self.options.print_prompt or ""
        if not text:
            text = await self._read_print_stdin()
        text = text.strip()
        if not text:
            return EXIT_SUCCESS

        sent_agent_message = await self._handle_input_line(text, defer_completion=True)
        if sent_agent_message:
            return await self._wait_for_completion()
        return self._exit_code

    async def _run_pipe_mode(self) -> int:
        while not self._stop_event.is_set():
            line = await self._readline()
            if line == "":
                break
            sent_agent_message = await self._handle_input_line(line, defer_completion=True)
            if sent_agent_message:
                self._sigint_count = 0

        session = self._require_session()
        if session.agent_active or self._deferred_complete_context_id:
            await self._wait_for_completion()
        return self._exit_code

    async def _run_repl_mode(self) -> int:
        while not self._stop_event.is_set():
            line = await self._readline()
            if line == "":
                self._stop_event.set()
                break
            await self._handle_input_line(line)
        return self._exit_code

    async def _handle_input_line(self, line: str, *, defer_completion: bool = False) -> bool:
        text = str(line or "").strip()
        if not text:
            return False

        if is_headless_command(text):
            may_start_agent = command_may_start_agent(text)
            if may_start_agent:
                self._complete_event.clear()
                self._deferred_complete_context_id = ""
                self._defer_complete_render = defer_completion
            result = await dispatch_headless_command(self._require_session(), text)
            if may_start_agent and not result.await_completion:
                self._defer_complete_render = False
            self._write_command_result(result)
            if result.exit_requested:
                self._stop_event.set()
            if result.error:
                self._exit_code = EXIT_ERROR
            return result.await_completion

        self._complete_event.clear()
        self._deferred_complete_context_id = ""
        self._defer_complete_render = defer_completion
        try:
            attachments: list[str] | None = None
            if (
                self.options.launcher_tag
                and self.options.attachment_refs
                and not self._tag_attachment_sent
            ):
                attachments = list(self.options.attachment_refs)
                self._tag_attachment_sent = True
            await self._require_session().send_message(text, attachments=attachments)
        except Exception as exc:
            self._defer_complete_render = False
            self._write_error("SEND_FAILED", str(exc))
            self._exit_code = EXIT_ERROR
            return False
        return True

    async def _wait_for_completion(self) -> int:
        session = self._require_session()
        if not session.agent_active and not self._deferred_complete_context_id:
            return self._exit_code

        complete_task = asyncio.create_task(self._complete_event.wait())
        stop_task = asyncio.create_task(self._stop_event.wait())
        done, pending = await asyncio.wait(
            {complete_task, stop_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        for task in pending:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if complete_task in done and self._deferred_complete_context_id:
            await self._drain_completion_events()
            self._render_deferred_complete()
        return self._exit_code

    async def _drain_completion_events(self) -> None:
        await asyncio.sleep(_COMPLETION_SETTLE_SECONDS)
        session = self._require_session()
        self._snapshot_event.clear()
        try:
            await session.refresh_context_snapshot()
        except Exception:
            return
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(
                self._snapshot_event.wait(),
                timeout=_COMPLETION_SNAPSHOT_TIMEOUT_SECONDS,
            )

    def _render_deferred_complete(self) -> None:
        context_id = self._deferred_complete_context_id
        self._deferred_complete_context_id = ""
        self._defer_complete_render = False
        if not context_id:
            return
        self._render_tag_result(context_id)
        self._write_lines(self.renderer.render_complete(context_id))
        self._notify_terminal_completion()
        if self._stdin_isatty() and self._waiting_for_line and not self._stop_event.is_set():
            self._prompt()

    def _notify_terminal_completion(self) -> None:
        if self.options.launcher_tag:
            return
        sequence = completion_notification_sequence(is_tty=self._stderr_isatty())
        if not sequence:
            return
        try:
            self.options.stderr.write(sequence)
            self.options.stderr.flush()
        except Exception:
            pass

    def _snapshot_event_changed(self, event: dict) -> bool:
        key = self._event_key(event)
        fingerprint = self._event_fingerprint(event)
        if key is None:
            return True
        if self._event_fingerprints.get(key) == fingerprint:
            return False
        self._event_fingerprints[key] = fingerprint
        return True

    def _remember_event(self, event: dict) -> None:
        key = self._event_key(event)
        if key is None:
            return
        self._event_fingerprints[key] = self._event_fingerprint(event)

    def _remember_tag_response(self, event: dict) -> None:
        if (
            not self.options.launcher_tag
            or str(event.get("event") or "") != "assistant_message"
        ):
            return
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        text = data.get("text")
        if not isinstance(text, str) or not text:
            return
        try:
            sequence = int(event.get("sequence"))
        except (TypeError, ValueError):
            sequence = self._tag_response_sequence
        if sequence < self._tag_response_sequence:
            return
        self._tag_response_sequence = sequence
        self._tag_response_text = text

    def _render_tag_result(self, context_id: str) -> None:
        if not self.options.launcher_tag:
            return
        mode, text, valid, error = parse_launcher_tag_result(self._tag_response_text)
        self._write_lines(
            self.renderer.render_tag_result(
                context_id,
                mode=mode,
                text=text,
                valid=valid,
                error=error,
            )
        )

    def _validate_options(self) -> None:
        if not self.options.launcher_tag:
            return
        profile = str(self.options.agent_profile or "").strip()
        if (
            not self.options.new_chat
            or self.options.output != "jsonl"
            or self.options.print_prompt is None
        ):
            raise SessionError(
                "INVALID_TAG_MODE",
                "--launcher-tag requires --new-chat --output jsonl --print.",
                stage="options",
                exit_code=1,
            )
        if not _TAG_PROFILE_RE.fullmatch(profile):
            raise SessionError(
                "INVALID_AGENT_PROFILE",
                "--agent-profile must be a non-empty Agent Zero profile key.",
                stage="options",
                exit_code=1,
            )
        self.options.agent_profile = profile
        self.options.attachment_refs = normalize_launcher_tag_attachment_refs(
            self.options.attachment_refs
        )

    def _event_key(self, event: dict) -> tuple[str, int, str] | None:
        try:
            sequence = int(event.get("sequence"))
        except (TypeError, ValueError):
            return None
        return (
            str(event.get("context_id") or ""),
            sequence,
            str(event.get("event") or ""),
        )

    def _event_fingerprint(self, event: dict) -> str:
        if str(event.get("event") or "") == "assistant_message":
            data = event.get("data") if isinstance(event.get("data"), dict) else {}
            return json.dumps(
                {
                    "context_id": event.get("context_id"),
                    "sequence": event.get("sequence"),
                    "event": event.get("event"),
                    "text": data.get("text"),
                    "heading": data.get("heading"),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        return json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    async def _resolve_host(self) -> str:
        explicit = self.options.host.strip()
        if explicit:
            return explicit
        configured = self.config.instance_url.strip()
        if configured:
            return configured
        if not self.options.discover_instances:
            return DEFAULT_HOST

        result = await discover_local_instances()
        if result.status == "ready" and len(result.instances) == 1:
            return result.instances[0].url
        if result.status == "ready" and len(result.instances) > 1:
            raise SessionError(
                "HOST_REQUIRED",
                "Multiple local Agent Zero instances were found; pass --host URL.",
                stage="discovery",
                exit_code=EXIT_CONNECT_OR_AUTH,
            )
        detail = result.detail or "No local Agent Zero Docker instance was discovered."
        raise SessionError(
            "HOST_REQUIRED",
            f"{detail} Pass --host URL.",
            stage="discovery",
            exit_code=EXIT_CONNECT_OR_AUTH,
        )

    async def _read_print_stdin(self) -> str:
        if self._stdin_isatty():
            self._prompt()
            line = await self._readline(prompt=False)
            return line
        return await asyncio.to_thread(self.options.stdin.read)

    async def _readline(self, *, prompt: bool = True) -> str:
        session = self.session
        if prompt and self._stdin_isatty() and (session is None or not session.agent_active):
            self._prompt()

        self._waiting_for_line = True
        try:
            line = await asyncio.to_thread(self.options.stdin.readline)
        finally:
            self._waiting_for_line = False
            self._prompt_visible = False
        return line

    async def _prompt_for_credentials(self) -> tuple[str, str]:
        def read_username() -> str:
            self.options.stderr.write("Username: ")
            self.options.stderr.flush()
            return self.options.stdin.readline().rstrip("\n")

        username = await asyncio.to_thread(read_username)
        password = await asyncio.to_thread(
            getpass.getpass,
            "Password: ",
            self.options.stderr,
        )
        return username.strip(), password

    async def _handle_sigint(self) -> None:
        session = self.session
        if session is not None and session.agent_active and self._sigint_count == 0:
            self._sigint_count += 1
            try:
                await session.pause()
                self._write_lines(self.renderer.render_notice("pause requested; press Ctrl+C again to exit."))
            except Exception as exc:
                self._write_error("PAUSE_FAILED", str(exc))
            return
        self._stop_event.set()

    def _install_signal_handlers(self) -> None:
        try:
            loop = asyncio.get_running_loop()
            loop.add_signal_handler(signal.SIGINT, lambda: asyncio.create_task(self._handle_sigint()))
            self._signal_installed = True
        except (NotImplementedError, RuntimeError):
            self._signal_installed = False

    def _remove_signal_handlers(self) -> None:
        if not self._signal_installed:
            return
        with contextlib.suppress(Exception):
            asyncio.get_running_loop().remove_signal_handler(signal.SIGINT)

    def _write_command_result(self, result: HeadlessCommandResult) -> None:
        for line in result.lines:
            self._write_lines(self.renderer.render_notice(line, error=result.error))

    def _write_error(self, code: str, message: str) -> None:
        lines = self.renderer.render_error(code, message)
        if self.options.output == "jsonl":
            self._write_lines(lines)
            return
        for line in lines:
            self.options.stderr.write(line + "\n")
        self.options.stderr.flush()

    def _write_lines(self, lines: list[str]) -> None:
        if not lines:
            return
        stream = self.options.stdout
        if self._prompt_visible and self.options.output == "text":
            stream.write("\n")
            self._prompt_visible = False
        for line in lines:
            if line == "":
                continue
            stream.write(line + "\n")
        stream.flush()

    def _prompt(self) -> None:
        if self._prompt_visible:
            return
        stream = self.options.stderr if self.options.output == "jsonl" else self.options.stdout
        stream.write("> ")
        stream.flush()
        self._prompt_visible = True

    def _require_session(self) -> ConnectorSession:
        if self.session is None:
            raise SessionError("NOT_CONNECTED", "Not connected to Agent Zero.", exit_code=EXIT_ERROR)
        return self.session

    def _stdin_isatty(self) -> bool:
        isatty = getattr(self.options.stdin, "isatty", None)
        return bool(isatty and isatty())

    def _stdout_isatty(self) -> bool:
        isatty = getattr(self.options.stdout, "isatty", None)
        return bool(isatty and isatty())

    def _stderr_isatty(self) -> bool:
        isatty = getattr(self.options.stderr, "isatty", None)
        return bool(isatty and isatty())


def run_headless(options: HeadlessOptions) -> int:
    return asyncio.run(HeadlessRunner(options).run())
