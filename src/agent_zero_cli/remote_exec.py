from __future__ import annotations

import asyncio
import base64
import locale
import os
import re
import shlex
import shutil
import sys
import time
import uuid
from dataclasses import dataclass
from typing import Any

_ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_HEX_ESCAPE_RE = re.compile(r"(?<!\\)\\x[0-9A-Fa-f]{2}")
_TIMEOUT_KEYS = (
    "first_output_timeout",
    "between_output_timeout",
    "max_exec_timeout",
    "dialog_timeout",
)
_SUPPORTED_RUNTIMES = ("terminal", "python", "nodejs", "output", "reset", "input")

_DEFAULT_CODE_EXEC_TIMEOUTS = {
    "first_output_timeout": 30,
    "between_output_timeout": 15,
    "max_exec_timeout": 180,
    "dialog_timeout": 5,
}
_DEFAULT_OUTPUT_TIMEOUTS = {
    "first_output_timeout": 90,
    "between_output_timeout": 45,
    "max_exec_timeout": 300,
    "dialog_timeout": 5,
}
_DEFAULT_PROMPT_PATTERNS = (
    r"(\(venv\)).+[$#] ?$",
    r"root@[^:]+:[^#]+# ?$",
    r"[a-zA-Z0-9_.-]+@[^:]+:[^$#]+[$#] ?$",
    r"\(?.*\)?\s*PS\s+[^>]+> ?$",
)
_DEFAULT_DIALOG_PATTERNS = (
    r"Y/N",
    r"yes/no",
    r":\s*$",
    r"\?\s*$",
)

_EXEC_DISABLED_ERROR = (
    "Remote execution is disabled in this CLI session. Press F4 to switch exec on."
)
_EXEC_WRITE_DISABLED_ERROR = (
    "Remote execution that may modify local files is disabled in this CLI session while "
    "local access is Read only. Press F3 to switch to Read&Write. "
    "`runtime=output` and `runtime=reset` remain available for existing sessions."
)
_RUNNING_MESSAGE = (
    "Terminal session {session} might be still running. Check previous outputs and "
    "decide whether to reset and continue or wait for more output is needed."
)
_MAX_TIME_MESSAGE = (
    "Returning control to agent after {timeout} seconds of execution. Process might "
    "be still running. Check previous outputs and decide whether to reset and "
    "continue or wait for more output is needed."
)
_NO_OUTPUT_MESSAGE = (
    "Returning control to agent after {timeout} seconds with no output. Process might "
    "be still running. Check previous outputs and decide whether to reset and "
    "continue or wait for more output is needed."
)
_PAUSE_TIME_MESSAGE = (
    "Returning control to agent after {timeout} seconds since last output update. "
    "Process might be still running. Check previous outputs and decide whether to "
    "reset and continue or wait for more output is needed."
)
_PAUSE_DIALOG_MESSAGE = (
    "Potential dialog detected in output. Returning control to agent after {timeout} "
    "seconds since last output update. Decide whether dialog actually occurred and "
    "needs to be addressed, or if it was just a false positive and wait for more output."
)
_RESET_MESSAGE = "Terminal session has been reset."

_PYTHON_CODE_ENV = "A0_PY_CODE"
_NODE_CODE_ENV = "A0_NODE_CODE"
_MARKER_PREFIX = "__A0_DONE__"


@dataclass(frozen=True)
class _ExecConfig:
    version: int
    code_exec_timeouts: dict[str, int]
    output_timeouts: dict[str, int]
    prompt_patterns: tuple[re.Pattern[str], ...]
    dialog_patterns: tuple[re.Pattern[str], ...]


@dataclass
class _SessionState:
    shell: "LocalShellSession"
    running: bool = False


def _parse_patterns(raw: Any, flags: int = 0) -> tuple[re.Pattern[str], ...]:
    if isinstance(raw, (list, tuple)):
        lines = [str(value) for value in raw]
    else:
        lines = str(raw or "").splitlines()
    return tuple(re.compile(line.strip(), flags) for line in lines if line.strip())


def _coerce_timeout_group(raw: Any, defaults: dict[str, int]) -> dict[str, int]:
    group = raw if isinstance(raw, dict) else {}
    result: dict[str, int] = {}
    for key in _TIMEOUT_KEYS:
        value = group.get(key, defaults[key])
        try:
            result[key] = int(value)
        except (TypeError, ValueError):
            result[key] = defaults[key]
    return result


def _default_exec_config() -> _ExecConfig:
    return _ExecConfig(
        version=1,
        code_exec_timeouts=dict(_DEFAULT_CODE_EXEC_TIMEOUTS),
        output_timeouts=dict(_DEFAULT_OUTPUT_TIMEOUTS),
        prompt_patterns=_parse_patterns(_DEFAULT_PROMPT_PATTERNS),
        dialog_patterns=_parse_patterns(_DEFAULT_DIALOG_PATTERNS, re.IGNORECASE),
    )


def _normalize_exec_config(payload: dict[str, Any] | None) -> _ExecConfig:
    if not isinstance(payload, dict):
        return _default_exec_config()

    defaults = _default_exec_config()
    try:
        version = int(payload.get("version", defaults.version))
    except (TypeError, ValueError):
        version = defaults.version

    prompt_source = payload.get("prompt_patterns", list(_DEFAULT_PROMPT_PATTERNS))
    dialog_source = payload.get("dialog_patterns", list(_DEFAULT_DIALOG_PATTERNS))

    return _ExecConfig(
        version=version,
        code_exec_timeouts=_coerce_timeout_group(
            payload.get("code_exec_timeouts"),
            defaults.code_exec_timeouts,
        ),
        output_timeouts=_coerce_timeout_group(
            payload.get("output_timeouts"),
            defaults.output_timeouts,
        ),
        prompt_patterns=_parse_patterns(prompt_source),
        dialog_patterns=_parse_patterns(dialog_source, re.IGNORECASE),
    )


def _clean_terminal_output(output: str) -> str:
    cleaned = _ANSI_ESCAPE_RE.sub("", str(output or ""))
    cleaned = cleaned.replace("\x00", "")
    cleaned = cleaned.replace("\r\n", "\n")
    cleaned = cleaned.replace("\r", "\n")
    return cleaned.strip("\n")


def _ps_single_quote(value: str) -> str:
    return value.replace("'", "''")


def _build_python_command(code: str) -> str:
    encoded = base64.b64encode(code.encode("utf-8")).decode("ascii")
    decoder = (
        "import base64, os; exec(compile(base64.b64decode(os.environ['A0_PY_CODE'])."
        "decode('utf-8'), '<a0-remote>', 'exec'))"
    )
    python_executable = sys.executable or "python"

    if os.name == "nt":
        encoded_ps = _ps_single_quote(encoded)
        executable_ps = _ps_single_quote(python_executable)
        return (
            f"$env:{_PYTHON_CODE_ENV} = '{encoded_ps}'; "
            f"& '{executable_ps}' -c \"{decoder}\"; "
            "$__a0InnerExit = if ($null -ne $LASTEXITCODE) { [int]$LASTEXITCODE } else { 0 }; "
            f"Remove-Item Env:{_PYTHON_CODE_ENV} -ErrorAction SilentlyContinue; "
            "$global:LASTEXITCODE = $__a0InnerExit"
        )

    return (
        f"{_PYTHON_CODE_ENV}={shlex.quote(encoded)} "
        f"{shlex.quote(python_executable)} -c {shlex.quote(decoder)}"
    )


def _build_node_command(code: str) -> str:
    encoded = base64.b64encode(code.encode("utf-8")).decode("ascii")
    decoder = (
        "const src = Buffer.from(process.env.A0_NODE_CODE, 'base64').toString('utf8'); "
        "(0, eval)(src);"
    )

    if os.name == "nt":
        encoded_ps = _ps_single_quote(encoded)
        return (
            f"$env:{_NODE_CODE_ENV} = '{encoded_ps}'; "
            f"& node -e \"{decoder}\"; "
            "$__a0InnerExit = if ($null -ne $LASTEXITCODE) { [int]$LASTEXITCODE } else { 0 }; "
            f"Remove-Item Env:{_NODE_CODE_ENV} -ErrorAction SilentlyContinue; "
            "$global:LASTEXITCODE = $__a0InnerExit"
        )

    return f"{_NODE_CODE_ENV}={shlex.quote(encoded)} node -e {shlex.quote(decoder)}"


class LocalShellSession:
    def __init__(self, *, cwd: str | None = None) -> None:
        self.cwd = cwd
        self.process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._state_lock = asyncio.Lock()
        self._output_event = asyncio.Event()
        self._raw_output = ""
        self._display_output = ""
        self._full_output_start = 0
        self._last_reported_len = 0
        self._marker_pattern: re.Pattern[str] | None = None
        self._command_completed = False
        self._closed = False

    @property
    def is_alive(self) -> bool:
        return self.process is not None and self.process.returncode is None

    @property
    def command_completed(self) -> bool:
        return self._command_completed

    async def connect(self) -> None:
        if self.is_alive:
            return

        cmd = self._shell_command()
        self.process = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=self.cwd or None,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        self._closed = False
        self._reader_task = asyncio.create_task(self._reader_loop())

    async def close(self) -> None:
        self._closed = True
        process = self.process
        self.process = None

        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()

        reader = self._reader_task
        self._reader_task = None
        if reader is not None:
            try:
                await reader
            except Exception:
                pass

    async def send_command(self, command: str) -> None:
        if not self.is_alive or self.process is None or self.process.stdin is None:
            raise RuntimeError("Terminal session is not connected")

        marker_pattern, wrapped = self._wrap_command(command)
        async with self._state_lock:
            self._raw_output = ""
            self._display_output = ""
            self._full_output_start = 0
            self._last_reported_len = 0
            self._marker_pattern = marker_pattern
            self._command_completed = False
            self._output_event.clear()

        self.process.stdin.write(wrapped.encode("utf-8"))
        await self.process.stdin.drain()

    async def send_input(self, text: str) -> None:
        if not self.is_alive or self.process is None or self.process.stdin is None:
            raise RuntimeError("Terminal session is not connected")

        payload = text
        if not payload.endswith("\n"):
            payload += "\n"

        self.process.stdin.write(payload.encode("utf-8"))
        await self.process.stdin.drain()

    async def reset_output(self) -> None:
        async with self._state_lock:
            self._full_output_start = len(self._display_output)
            self._last_reported_len = len(self._display_output)
            self._output_event.clear()

    async def read_output(
        self,
        *,
        timeout: float = 0,
        reset_full_output: bool = False,
    ) -> tuple[str, str | None]:
        if reset_full_output:
            async with self._state_lock:
                self._full_output_start = len(self._display_output)
                self._last_reported_len = len(self._display_output)

        if timeout > 0:
            try:
                await asyncio.wait_for(self._output_event.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                pass
        elif timeout == 0:
            await asyncio.sleep(0)

        async with self._state_lock:
            full_output = self._display_output[self._full_output_start :]
            partial_output = self._display_output[self._last_reported_len :]
            self._last_reported_len = len(self._display_output)
            self._output_event.clear()

        normalized_full = _clean_terminal_output(full_output)
        normalized_partial = _clean_terminal_output(partial_output)
        return normalized_full, (normalized_partial or None)

    def _shell_command(self) -> list[str]:
        if os.name == "nt":
            executable = shutil.which("powershell.exe") or shutil.which("pwsh") or "powershell.exe"
            return [
                executable,
                "-NoLogo",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-NoExit",
                "-Command",
                "-",
            ]

        shell = os.environ.get("SHELL", "").strip()
        executable = ""
        if shell:
            executable = shutil.which(shell) or shell
        if not executable:
            executable = shutil.which("bash") or shutil.which("sh") or "/bin/sh"

        if os.path.basename(executable) == "bash":
            return [executable, "--noprofile", "--norc"]
        return [executable]

    def _wrap_command(self, command: str) -> tuple[re.Pattern[str], str]:
        marker = f"{_MARKER_PREFIX}{uuid.uuid4().hex}__"
        marker_pattern = re.compile(rf"(?:\r?\n|^){re.escape(marker)}:(-?\d+)\r?\n?")
        body = command.rstrip("\n")

        if os.name == "nt":
            encoded_body = base64.b64encode(body.encode("utf-8")).decode("ascii")
            wrapped = (
                f"$__a0Body = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded_body}')); "
                "$__a0Exit = 0; "
                "try { "
                "Invoke-Expression $__a0Body; "
                "$__a0Exit = if ($null -ne $LASTEXITCODE) { [int]$LASTEXITCODE } else { 0 } "
                "} catch { "
                "Write-Error $_; "
                "$__a0Exit = 1 "
                "}; "
                'Write-Output ""; '
                f'Write-Output "{marker}:$__a0Exit"\n'
            )
            return marker_pattern, wrapped

        wrapped = (
            f"{body}\n"
            "__a0_exit=$?\n"
            f"printf '\\n{marker}:%s\\n' \"$__a0_exit\"\n"
        )
        return marker_pattern, wrapped

    async def _reader_loop(self) -> None:
        if self.process is None or self.process.stdout is None:
            return

        encoding = locale.getpreferredencoding(False) or "utf-8"
        try:
            while True:
                chunk = await self.process.stdout.read(4096)
                if not chunk:
                    break
                text = chunk.decode(encoding, errors="replace")
                await self._append_output(text)
        finally:
            async with self._state_lock:
                if self._marker_pattern is not None:
                    self._command_completed = True
                self._output_event.set()

    async def _append_output(self, text: str) -> None:
        async with self._state_lock:
            self._raw_output += text
            marker_pattern = self._marker_pattern
            if marker_pattern is not None:
                match = marker_pattern.search(self._raw_output)
                if match:
                    self._display_output = self._raw_output[: match.start()]
                    self._raw_output = self._display_output
                    self._marker_pattern = None
                    self._command_completed = True
                else:
                    self._display_output = self._raw_output
            else:
                self._display_output = self._raw_output
            self._output_event.set()


class RemoteExecManager:
    def __init__(
        self,
        *,
        cwd: str,
        enabled: bool = True,
        allow_writes: bool = True,
        poll_interval: float = 0.1,
    ) -> None:
        self.cwd = cwd
        self.enabled = enabled
        self.allow_writes = allow_writes
        self.poll_interval = poll_interval
        self._exec_config = _default_exec_config()
        self._sessions: dict[int, _SessionState] = {}

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled

    def set_write_enabled(self, enabled: bool) -> None:
        self.allow_writes = enabled

    def _runtime_requires_write_access(self, runtime: str) -> bool:
        return runtime in {"terminal", "python", "nodejs", "input"}

    def set_exec_config(self, payload: dict[str, Any] | None) -> None:
        self._exec_config = _normalize_exec_config(payload)

    async def close(self) -> None:
        sessions = list(self._sessions.values())
        self._sessions.clear()
        for state in sessions:
            try:
                await state.shell.close()
            except Exception:
                continue

    async def handle_exec_op(self, data: dict[str, Any]) -> dict[str, Any]:
        op_id = str(data.get("op_id", "")).strip()

        if not self.enabled:
            return {"op_id": op_id, "ok": False, "error": _EXEC_DISABLED_ERROR}

        runtime = str(data.get("runtime", "")).strip().lower()
        if runtime not in _SUPPORTED_RUNTIMES:
            return {
                "op_id": op_id,
                "ok": False,
                "error": (
                    "runtime must be one of: terminal, python, nodejs, output, reset, "
                    "input (deprecated alias)"
                ),
            }

        if self._runtime_requires_write_access(runtime) and not self.allow_writes:
            return {"op_id": op_id, "ok": False, "error": _EXEC_WRITE_DISABLED_ERROR}

        try:
            session = int(data.get("session", 0) or 0)
        except (TypeError, ValueError):
            return {
                "op_id": op_id,
                "ok": False,
                "error": "session must be an integer",
            }

        try:
            if runtime == "terminal":
                code = data.get("code")
                if code is None or not str(code).strip():
                    raise ValueError("code is required for runtime=terminal")
                result = await self.execute_terminal(session=session, command=str(code))
            elif runtime == "python":
                code = data.get("code")
                if code is None or not str(code).strip():
                    raise ValueError("code is required for runtime=python")
                result = await self.execute_python(session=session, code=str(code))
            elif runtime == "nodejs":
                code = data.get("code")
                if code is None or not str(code).strip():
                    raise ValueError("code is required for runtime=nodejs")
                result = await self.execute_nodejs(session=session, code=str(code))
            elif runtime == "input":
                keyboard = data.get("keyboard")
                if keyboard is None:
                    keyboard = data.get("code")
                if keyboard is None:
                    raise ValueError("keyboard is required for runtime=input")
                result = await self.send_input(session=session, keyboard=str(keyboard))
            elif runtime == "output":
                result = await self.collect_output(session=session)
            else:
                result = await self.reset_session(
                    session=session,
                    reason=str(data.get("reason") or ""),
                )
        except Exception as exc:
            return {"op_id": op_id, "ok": False, "error": str(exc)}

        return {"op_id": op_id, "ok": True, "result": result}

    async def execute_terminal(self, *, session: int, command: str) -> dict[str, Any]:
        return await self._run_shell_command(
            session=session,
            command=command,
            timeouts=self._exec_config.code_exec_timeouts,
        )

    async def execute_python(self, *, session: int, code: str) -> dict[str, Any]:
        return await self._run_shell_command(
            session=session,
            command=_build_python_command(code),
            timeouts=self._exec_config.code_exec_timeouts,
        )

    async def execute_nodejs(self, *, session: int, code: str) -> dict[str, Any]:
        return await self._run_shell_command(
            session=session,
            command=_build_node_command(code),
            timeouts=self._exec_config.code_exec_timeouts,
        )

    async def send_input(self, *, session: int, keyboard: str) -> dict[str, Any]:
        if session not in self._sessions or not self._sessions[session].running:
            raise ValueError(
                f"Session {session} is not awaiting input. runtime=input is a deprecated "
                "compatibility alias for sending a line into a running shell session."
            )

        state = self._sessions[session]
        await state.shell.reset_output()
        await state.shell.send_input(keyboard.rstrip("\n"))
        state.running = True
        return await self._get_terminal_output(
            session=session,
            timeouts=self._exec_config.code_exec_timeouts,
            reset_full_output=False,
        )

    async def collect_output(self, *, session: int) -> dict[str, Any]:
        return await self._get_terminal_output(
            session=session,
            timeouts=self._exec_config.output_timeouts,
            reset_full_output=False,
        )

    async def reset_session(self, *, session: int, reason: str = "") -> dict[str, Any]:
        await self._ensure_session(session, reset=True)
        message = _RESET_MESSAGE
        if reason.strip():
            message = f"{message} Reason: {reason.strip()}."
        return {
            "message": message,
            "output": "",
            "running": False,
        }

    def _create_shell_session(self) -> LocalShellSession:
        return LocalShellSession(cwd=self.cwd)

    async def _run_shell_command(
        self,
        *,
        session: int,
        command: str,
        timeouts: dict[str, int],
    ) -> dict[str, Any]:
        state = await self._ensure_session(session)
        if response := await self._handle_running_session(session=session):
            return response

        await state.shell.send_command(command)
        state.running = True
        return await self._get_terminal_output(
            session=session,
            timeouts=timeouts,
            reset_full_output=False,
        )

    async def _ensure_session(self, session: int, *, reset: bool = False) -> _SessionState:
        if reset:
            existing = self._sessions.pop(session, None)
            if existing is not None:
                await existing.shell.close()

        existing = self._sessions.get(session)
        if existing is not None and existing.shell.is_alive:
            return existing
        if existing is not None:
            await existing.shell.close()

        shell = self._create_shell_session()
        await shell.connect()
        state = _SessionState(shell=shell, running=False)
        self._sessions[session] = state
        return state

    async def _handle_running_session(self, *, session: int) -> dict[str, Any] | None:
        state = self._sessions.get(session)
        if state is None or not state.running:
            return None

        full_output, _ = await state.shell.read_output(timeout=1, reset_full_output=False)
        output = self._normalize_output(full_output)

        if state.shell.command_completed or self._detect_prompt(output, self._exec_config.prompt_patterns):
            state.running = False
            return None

        message = _RUNNING_MESSAGE.format(session=session)
        if self._detect_dialog(output, self._exec_config.dialog_patterns):
            message = _PAUSE_DIALOG_MESSAGE.format(timeout=1)

        return {
            "message": message,
            "output": output,
            "running": True,
        }

    async def _get_terminal_output(
        self,
        *,
        session: int,
        timeouts: dict[str, int],
        reset_full_output: bool,
    ) -> dict[str, Any]:
        state = await self._ensure_session(session)

        start_time = time.monotonic()
        last_output_time = start_time
        got_output = False
        output = ""

        while True:
            await asyncio.sleep(self.poll_interval)
            full_output, partial_output = await state.shell.read_output(
                timeout=1,
                reset_full_output=reset_full_output,
            )
            reset_full_output = False
            now = time.monotonic()

            output = self._normalize_output(full_output)
            partial = self._normalize_output(partial_output or "")

            if partial:
                got_output = True
                last_output_time = now

            if state.shell.command_completed or self._detect_prompt(output, self._exec_config.prompt_patterns):
                state.running = False
                completed_output = self._trim_prompt(output, self._exec_config.prompt_patterns)
                return {
                    "message": f"Session {session} completed.",
                    "output": completed_output,
                    "running": False,
                }

            if now - start_time > timeouts["max_exec_timeout"]:
                state.running = True
                return {
                    "message": _MAX_TIME_MESSAGE.format(timeout=timeouts["max_exec_timeout"]),
                    "output": output,
                    "running": True,
                }

            if not got_output:
                if now - start_time > timeouts["first_output_timeout"]:
                    state.running = True
                    return {
                        "message": _NO_OUTPUT_MESSAGE.format(timeout=timeouts["first_output_timeout"]),
                        "output": output,
                        "running": True,
                    }
                continue

            if now - last_output_time > timeouts["between_output_timeout"]:
                state.running = True
                return {
                    "message": _PAUSE_TIME_MESSAGE.format(timeout=timeouts["between_output_timeout"]),
                    "output": output,
                    "running": True,
                }

            if (
                now - last_output_time > timeouts["dialog_timeout"]
                and self._detect_dialog(output, self._exec_config.dialog_patterns)
            ):
                state.running = True
                return {
                    "message": _PAUSE_DIALOG_MESSAGE.format(timeout=timeouts["dialog_timeout"]),
                    "output": output,
                    "running": True,
                }

    def _normalize_output(self, output: str) -> str:
        if not output:
            return ""
        cleaned = _clean_terminal_output(output)
        cleaned = _HEX_ESCAPE_RE.sub("", cleaned)
        return cleaned.strip()

    def _detect_prompt(
        self,
        output: str,
        patterns: tuple[re.Pattern[str], ...],
    ) -> bool:
        if not output:
            return False

        last_lines = output.splitlines()[-3:]
        for line in reversed(last_lines):
            candidate = line.strip()
            if len(candidate) > 500:
                candidate = candidate[:250] + candidate[-250:]
            for pattern in patterns:
                if pattern.search(candidate):
                    return True
        return False

    def _detect_dialog(
        self,
        output: str,
        patterns: tuple[re.Pattern[str], ...],
    ) -> bool:
        if not output:
            return False

        for line in output.splitlines()[-2:]:
            candidate = line.strip()
            for pattern in patterns:
                if pattern.search(candidate):
                    return True
        return False

    def _trim_prompt(
        self,
        output: str,
        patterns: tuple[re.Pattern[str], ...],
    ) -> str:
        if not output:
            return ""

        lines = output.splitlines()
        if not lines:
            return ""

        last_line = lines[-1].strip()
        for pattern in patterns:
            if pattern.search(last_line):
                lines = lines[:-1]
                break
        return "\n".join(lines).strip()


PythonTTYManager = RemoteExecManager
