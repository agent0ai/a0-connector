from __future__ import annotations

import asyncio
import errno
import os
import pty
import re
import shlex
import sys
import termios
import time
from dataclasses import dataclass
from typing import Any

_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_PROMPT_RE = re.compile(r"(?:>>> |\.\.\. )$")


class _TTYSession:
    def __init__(
        self,
        cmd: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        encoding: str = "utf-8",
        echo: bool = False,
    ) -> None:
        self.cmd = cmd
        self.cwd = cwd
        self.env = env or os.environ.copy()
        self.encoding = encoding
        self.echo = echo
        self._proc: asyncio.subprocess.Process | None = None
        self._buf: asyncio.Queue[str] | None = None
        self._pump_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._buf = asyncio.Queue()
        self._proc = await self._spawn_posix_pty(self.cmd, self.cwd, self.env, self.echo)
        self._pump_task = asyncio.create_task(self._pump_stdout())

    async def close(self) -> None:
        if self._pump_task is not None:
            self._pump_task.cancel()
            try:
                await self._pump_task
            except asyncio.CancelledError:
                pass

        if self._proc is not None and self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=2)
            except asyncio.TimeoutError:
                self._proc.kill()
                await self._proc.wait()

        self._proc = None
        self._pump_task = None

    async def sendline(self, line: str) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise RuntimeError("TTY session not started")
        self._proc.stdin.write((line + "\n").encode(self.encoding))
        await self._proc.stdin.drain()

    async def read(self, timeout: float | None = None) -> str | None:
        if self._buf is None:
            raise RuntimeError("TTY session not started")
        try:
            return await asyncio.wait_for(self._buf.get(), timeout)
        except asyncio.TimeoutError:
            return None

    async def read_full_until_idle(self, *, idle_timeout: float, total_timeout: float) -> str:
        start = time.monotonic()
        chunks: list[str] = []
        while True:
            if time.monotonic() - start > total_timeout:
                break
            chunk = await self.read(timeout=idle_timeout)
            if chunk is None:
                break
            chunks.append(chunk)
        return "".join(chunks)

    async def _pump_stdout(self) -> None:
        if self._proc is None or self._proc.stdout is None or self._buf is None:
            return
        while True:
            chunk = await self._proc.stdout.read(4096)
            if not chunk:
                break
            self._buf.put_nowait(chunk.decode(self.encoding, "replace"))

    async def _spawn_posix_pty(
        self,
        cmd: str,
        cwd: str | None,
        env: dict[str, str],
        echo: bool,
    ) -> asyncio.subprocess.Process:
        master, slave = pty.openpty()

        if not echo:
            attrs = termios.tcgetattr(slave)
            attrs[3] &= ~termios.ECHO
            termios.tcsetattr(slave, termios.TCSANOW, attrs)

        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            cwd=cwd,
            env=env,
            close_fds=True,
        )
        os.close(slave)

        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader()

        def _on_data() -> None:
            try:
                data = os.read(master, 1 << 16)
            except OSError as exc:
                if exc.errno != errno.EIO:
                    raise
                data = b""

            if data:
                reader.feed_data(data)
            else:
                reader.feed_eof()
                loop.remove_reader(master)
                try:
                    os.close(master)
                except OSError:
                    pass

        loop.add_reader(master, _on_data)

        class _Stdin:
            def write(self, data: bytes) -> None:
                os.write(master, data)

            async def drain(self) -> None:
                await asyncio.sleep(0)

        proc.stdin = _Stdin()  # type: ignore[attr-defined]
        proc.stdout = reader  # type: ignore[attr-defined]
        return proc


@dataclass
class _SessionState:
    tty: _TTYSession
    running: bool = False


class PythonTTYManager:
    def __init__(
        self,
        *,
        cwd: str,
        python_executable: str | None = None,
        exec_timeouts: tuple[float, float, float] = (30.0, 15.0, 180.0),
        output_timeouts: tuple[float, float, float] = (90.0, 45.0, 300.0),
    ) -> None:
        self.cwd = cwd
        self.python_executable = python_executable or sys.executable or "python3"
        self.exec_timeouts = exec_timeouts
        self.output_timeouts = output_timeouts
        self._sessions: dict[int, _SessionState] = {}

    async def close(self) -> None:
        sessions = list(self._sessions.values())
        self._sessions.clear()
        for state in sessions:
            await state.tty.close()

    async def handle_exec_op(self, data: dict[str, Any]) -> dict[str, Any]:
        op_id = str(data.get("op_id", "")).strip()
        runtime = str(data.get("runtime", "")).strip().lower()

        try:
            session = int(data.get("session", 0) or 0)
        except (TypeError, ValueError):
            return {
                "op_id": op_id,
                "ok": False,
                "error": "session must be an integer",
            }

        try:
            if runtime == "python":
                code = data.get("code")
                if code is None or not str(code).strip():
                    raise ValueError("code is required for runtime=python")
                result = await self.execute_python(session=session, code=str(code))
            elif runtime == "input":
                keyboard = data.get("keyboard")
                if keyboard is None:
                    raise ValueError("keyboard is required for runtime=input")
                result = await self.send_input(session=session, keyboard=str(keyboard))
            elif runtime == "output":
                result = await self.collect_output(session=session)
            elif runtime == "reset":
                result = await self.reset_session(session=session, reason=str(data.get("reason") or ""))
            else:
                raise ValueError("runtime must be one of: python, input, output, reset")
        except Exception as exc:
            return {
                "op_id": op_id,
                "ok": False,
                "error": str(exc),
            }

        return {
            "op_id": op_id,
            "ok": True,
            "result": result,
        }

    async def execute_python(self, *, session: int, code: str) -> dict[str, Any]:
        state = await self._ensure_session(session)
        if state.running:
            return {
                "message": f"Session {session} is already running. Use runtime='input', runtime='output', or reset first.",
                "output": "",
                "running": True,
            }

        command = f"exec(compile({code!r}, '<remote>', 'exec'))"
        await state.tty.sendline(command)
        state.running = True
        return await self._collect_until_pause(session=session, timeouts=self.exec_timeouts)

    async def send_input(self, *, session: int, keyboard: str) -> dict[str, Any]:
        state = await self._ensure_session(session)
        await state.tty.sendline(keyboard.rstrip("\n"))
        state.running = True
        return await self._collect_until_pause(session=session, timeouts=self.exec_timeouts)

    async def collect_output(self, *, session: int) -> dict[str, Any]:
        if session not in self._sessions:
            raise ValueError(f"Session {session} is not initialized")
        return await self._collect_until_pause(session=session, timeouts=self.output_timeouts)

    async def reset_session(self, *, session: int, reason: str = "") -> dict[str, Any]:
        state = self._sessions.pop(session, None)
        if state is not None:
            await state.tty.close()

        message = f"Session {session} reset."
        if reason.strip():
            message = f"Session {session} reset ({reason.strip()})."
        return {
            "message": message,
            "output": "",
            "running": False,
        }

    async def _ensure_session(self, session: int) -> _SessionState:
        existing = self._sessions.get(session)
        if existing is not None:
            return existing

        python_cmd = f"{shlex.quote(self.python_executable)} -i -q"
        tty = _TTYSession(python_cmd, cwd=self.cwd)
        await tty.start()
        await tty.read_full_until_idle(idle_timeout=0.05, total_timeout=1.0)

        state = _SessionState(tty=tty, running=False)
        self._sessions[session] = state
        return state

    async def _collect_until_pause(
        self,
        *,
        session: int,
        timeouts: tuple[float, float, float],
    ) -> dict[str, Any]:
        first_output_timeout, between_output_timeout, max_exec_timeout = timeouts
        state = self._sessions[session]

        start = time.monotonic()
        last_output = start
        got_output = False
        output_parts: list[str] = []

        while True:
            chunk = await state.tty.read_full_until_idle(idle_timeout=0.05, total_timeout=0.5)
            now = time.monotonic()

            if chunk:
                cleaned = self._clean_chunk(chunk)
                if cleaned:
                    output_parts.append(cleaned)
                    got_output = True
                    last_output = now

                full_output = "".join(output_parts)
                if self._has_prompt(full_output):
                    state.running = False
                    trimmed = self._trim_prompt(full_output).strip()
                    return {
                        "message": f"Session {session} completed.",
                        "output": trimmed,
                        "running": False,
                    }
                state.running = True

            full_output = "".join(output_parts).strip()

            if now - start >= max_exec_timeout:
                state.running = True
                return {
                    "message": (
                        f"Session {session} is still running (max timeout reached). "
                        "Use runtime='output' to continue waiting."
                    ),
                    "output": full_output,
                    "running": True,
                }

            if not got_output and now - start >= first_output_timeout:
                state.running = True
                return {
                    "message": (
                        f"Session {session} started with no output yet. "
                        "Use runtime='output' to continue waiting."
                    ),
                    "output": full_output,
                    "running": True,
                }

            if got_output and now - last_output >= between_output_timeout:
                state.running = True
                return {
                    "message": (
                        f"Session {session} is still running. "
                        "Use runtime='output' to continue waiting or runtime='input' if input is expected."
                    ),
                    "output": full_output,
                    "running": True,
                }

            await asyncio.sleep(0.05)

    def _clean_chunk(self, chunk: str) -> str:
        cleaned = chunk.replace("\r\n", "\n").replace("\r", "")
        cleaned = _ANSI_RE.sub("", cleaned)
        return cleaned

    def _has_prompt(self, text: str) -> bool:
        stripped = text.rstrip("\n")
        if not stripped:
            return False
        last_line = stripped.split("\n")[-1]
        return bool(_PROMPT_RE.search(last_line))

    def _trim_prompt(self, text: str) -> str:
        stripped = text.rstrip("\n")
        if not stripped:
            return ""
        lines = stripped.split("\n")
        if lines and _PROMPT_RE.search(lines[-1]):
            lines = lines[:-1]
        return "\n".join(lines)
