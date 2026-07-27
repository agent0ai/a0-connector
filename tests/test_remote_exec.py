from __future__ import annotations

import asyncio
import hashlib
import os
import shlex
import sys
from pathlib import Path

import pytest

from agent_zero_cli import remote_exec
from agent_zero_cli.remote_exec import LocalShellSession, RemoteExecManager


pytestmark = pytest.mark.anyio


class FakeShellSession:
    def __init__(self, *, cwd: str | None = None) -> None:
        self.cwd = cwd
        self.commands: list[str] = []
        self.inputs: list[str] = []
        self.is_alive = True
        self.command_completed = False
        self._full_output = ""
        self._partial_output = ""

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        self.is_alive = False

    async def send_command(self, command: str) -> None:
        self.commands.append(command)
        if command == "ansi":
            self._full_output = "\x1b[31mhello\x1b[0m\r\n"
            self._partial_output = self._full_output
            self.command_completed = True
            return

        if command == "osc":
            self._full_output = "\x1b]8;;https://example.test\x07link\x1b]8;;\x07\r\n"
            self._partial_output = self._full_output
            self.command_completed = True
            return

        if command == "large":
            self._full_output = "prefix\n" + "x" * (remote_exec.EXEC_OUTPUT_MAX_BYTES + 4096)
            self._partial_output = self._full_output
            self.command_completed = True
            return

        if "A0_PY_CODE" in command:
            self._full_output = "42\r\n"
            self._partial_output = self._full_output
            self.command_completed = True
            return

        if "A0_NODE_CODE" in command:
            self._full_output = "node ok\r\n"
            self._partial_output = self._full_output
            self.command_completed = True
            return

        if command == "ask":
            self._full_output = "Continue? "
            self._partial_output = self._full_output
            self.command_completed = False
            return

        if command == "silent":
            self._full_output = ""
            self._partial_output = ""
            self.command_completed = False
            return

        self._full_output = f"ran:{command}\r\n"
        self._partial_output = self._full_output
        self.command_completed = True

    async def send_input(self, text: str) -> None:
        self.inputs.append(text)
        self._full_output = f"input:{text}\r\n"
        self._partial_output = self._full_output
        self.command_completed = True

    async def reset_output(self) -> None:
        self._full_output = ""
        self._partial_output = ""

    async def read_output(
        self,
        *,
        timeout: float = 0,
        reset_full_output: bool = False,
    ) -> tuple[str, str | None]:
        del timeout, reset_full_output
        partial = self._partial_output or None
        self._partial_output = ""
        return self._full_output, partial


@pytest.fixture
def created_shells(monkeypatch: pytest.MonkeyPatch) -> list[FakeShellSession]:
    shells: list[FakeShellSession] = []

    def _create_shell_session(self: RemoteExecManager) -> FakeShellSession:
        shell = FakeShellSession(cwd=self.cwd)
        shells.append(shell)
        return shell

    monkeypatch.setattr(RemoteExecManager, "_create_shell_session", _create_shell_session)
    return shells


def _manager(tmp_path: Path, *, enabled: bool = True) -> RemoteExecManager:
    return RemoteExecManager(cwd=str(tmp_path), enabled=enabled, poll_interval=0.01)


@pytest.mark.parametrize(
    "size",
    [
        0,
        1,
        remote_exec.EXEC_OUTPUT_MAX_BYTES - 1,
        remote_exec.EXEC_OUTPUT_MAX_BYTES,
        remote_exec.EXEC_OUTPUT_MAX_BYTES + 1,
    ],
)
def test_exec_output_boundary_matrix(tmp_path: Path, size: int) -> None:
    manager = _manager(tmp_path)
    output = "x" * size

    result = manager._bound_exec_output({"output": output}, session=7)

    if size <= remote_exec.EXEC_OUTPUT_MAX_BYTES:
        assert result == {"output": output}
        assert list(tmp_path.glob("a0-exec-output-*.log")) == []
    else:
        output_file = Path(result["output_file"])
        assert result["output_truncated"] is True
        assert len(result["output"].encode("utf-8")) <= remote_exec.EXEC_OUTPUT_MAX_BYTES
        assert result["output_total_bytes"] == size
        assert result["output_sha256"] == hashlib.sha256(output.encode()).hexdigest()
        assert output_file.read_text(encoding="utf-8") == output


def test_default_timeout_config_matches_core_code_execution(tmp_path: Path) -> None:
    manager = _manager(tmp_path)

    assert manager._exec_config.code_exec_timeouts == {
        "first_output_timeout": 30,
        "between_output_timeout": 15,
        "max_exec_timeout": 240,
        "dialog_timeout": 5,
    }
    assert manager._exec_config.output_timeouts == {
        "first_output_timeout": 120,
        "between_output_timeout": 60,
        "max_exec_timeout": 600,
        "dialog_timeout": 5,
    }


async def test_remote_exec_uses_connector_local_runtime_without_core_checkout(
    tmp_path: Path,
    created_shells: list[FakeShellSession],
) -> None:
    manager = _manager(tmp_path)

    result = await manager.handle_exec_op(
        {
            "op_id": "exec-terminal",
            "runtime": "terminal",
            "session": 0,
            "code": "ansi",
        }
    )

    assert result["ok"] is True
    assert result["result"]["output"] == "hello"
    assert result["result"]["running"] is False
    assert created_shells[0].commands == ["ansi"]

    await manager.close()


async def test_remote_exec_strips_osc_terminal_sequences(
    tmp_path: Path,
    created_shells: list[FakeShellSession],
) -> None:
    manager = _manager(tmp_path)

    result = await manager.handle_exec_op(
        {
            "op_id": "exec-osc",
            "runtime": "terminal",
            "session": 0,
            "code": "osc",
        }
    )

    assert result["ok"] is True
    assert result["result"]["output"] == "link"
    assert created_shells[0].commands == ["osc"]

    await manager.close()


async def test_remote_exec_spills_oversized_output_and_returns_bounded_tail(
    tmp_path: Path,
    created_shells: list[FakeShellSession],
) -> None:
    manager = _manager(tmp_path)

    result = await manager.handle_exec_op(
        {
            "op_id": "exec-large",
            "runtime": "terminal",
            "session": 7,
            "code": "large",
        }
    )

    assert result["ok"] is True
    output = result["result"]["output"]
    output_path = Path(result["result"]["output_file"])
    full_output = created_shells[0]._full_output
    assert len(output.encode("utf-8")) <= remote_exec.EXEC_OUTPUT_MAX_BYTES
    assert output.startswith("[Output truncated to the final bytes. Full output: ")
    assert output.endswith("x" * 1024)
    assert result["result"]["output_truncated"] is True
    assert result["result"]["output_total_bytes"] == len(full_output.encode("utf-8"))
    assert result["result"]["output_sha256"] == hashlib.sha256(
        full_output.encode("utf-8")
    ).hexdigest()
    assert output_path.parent == tmp_path
    assert output_path.read_text(encoding="utf-8") == full_output
    assert list(tmp_path.glob(".*.partial-*")) == []

    await manager.close()


def test_windows_shell_prefers_pwsh_when_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands = {
        "pwsh.exe": r"C:\Program Files\PowerShell\7\pwsh.exe",
        "powershell.exe": r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
    }
    monkeypatch.setattr(remote_exec.os, "name", "nt")
    monkeypatch.setattr(remote_exec.shutil, "which", lambda name: commands.get(name))

    command = LocalShellSession(cwd=None)._shell_command()

    assert command[0] == commands["pwsh.exe"]


async def test_exec_op_payload_timeouts_override_connector_defaults(
    tmp_path: Path,
    created_shells: list[FakeShellSession],
) -> None:
    manager = _manager(tmp_path)

    result = await manager.handle_exec_op(
        {
            "op_id": "exec-timeout",
            "runtime": "terminal",
            "session": 0,
            "code": "silent",
            "timeouts": {
                "first_output_timeout": 0,
                "between_output_timeout": 1,
                "max_exec_timeout": 5,
                "dialog_timeout": 0,
            },
        }
    )

    assert result["ok"] is True
    assert result["result"]["running"] is True
    assert "after 0 seconds with no output" in result["result"]["message"]
    assert created_shells[0].commands == ["silent"]

    await manager.close()


async def test_terminal_python_and_nodejs_runtimes_are_supported(
    tmp_path: Path,
    created_shells: list[FakeShellSession],
) -> None:
    manager = _manager(tmp_path)

    terminal_result = await manager.handle_exec_op(
        {
            "op_id": "exec-terminal",
            "runtime": "terminal",
            "session": 0,
            "code": "ansi",
        }
    )
    python_result = await manager.handle_exec_op(
        {
            "op_id": "exec-python",
            "runtime": "python",
            "session": 1,
            "code": "print(42)",
        }
    )
    node_result = await manager.handle_exec_op(
        {
            "op_id": "exec-node",
            "runtime": "nodejs",
            "session": 2,
            "code": "console.log('ok')",
        }
    )

    assert terminal_result["ok"] is True
    assert terminal_result["result"]["output"] == "hello"

    assert python_result["ok"] is True
    assert python_result["result"]["output"] == "42"
    assert "A0_PY_CODE" in created_shells[1].commands[0]
    assert sys.executable in created_shells[1].commands[0]

    assert node_result["ok"] is True
    assert node_result["result"]["output"] == "node ok"
    assert "A0_NODE_CODE" in created_shells[2].commands[0]

    await manager.close()


async def test_input_runtime_sends_keystrokes_into_running_session(
    tmp_path: Path,
    created_shells: list[FakeShellSession],
) -> None:
    manager = _manager(tmp_path)
    manager.set_exec_config(
        {
            "version": 1,
            "code_exec_timeouts": {
                "first_output_timeout": 0,
                "between_output_timeout": 1,
                "max_exec_timeout": 5,
                "dialog_timeout": 0,
            },
            "output_timeouts": {
                "first_output_timeout": 0,
                "between_output_timeout": 1,
                "max_exec_timeout": 5,
                "dialog_timeout": 0,
            },
            "dialog_patterns": [r"\?\s*$"],
        }
    )

    running_result = await manager.handle_exec_op(
        {
            "op_id": "exec-ask",
            "runtime": "terminal",
            "session": 0,
            "code": "ask",
        }
    )
    input_result = await manager.handle_exec_op(
        {
            "op_id": "exec-input",
            "runtime": "input",
            "session": 0,
            "keyboard": "y",
        }
    )

    assert running_result["ok"] is True
    assert running_result["result"]["running"] is True
    assert "Potential dialog detected" in running_result["result"]["message"]

    assert input_result["ok"] is True
    assert input_result["result"]["output"] == "input:y"
    assert input_result["result"]["running"] is False
    assert created_shells[0].inputs == ["y"]

    await manager.close()


async def test_terminal_runtime_reset_true_closes_running_session_and_runs_replacement(
    tmp_path: Path,
    created_shells: list[FakeShellSession],
) -> None:
    manager = _manager(tmp_path)
    manager.set_exec_config(
        {
            "version": 1,
            "code_exec_timeouts": {
                "first_output_timeout": 0,
                "between_output_timeout": 1,
                "max_exec_timeout": 5,
                "dialog_timeout": 0,
            },
            "output_timeouts": {
                "first_output_timeout": 0,
                "between_output_timeout": 1,
                "max_exec_timeout": 5,
                "dialog_timeout": 0,
            },
            "dialog_patterns": [r"\?\s*$"],
        }
    )

    running_result = await manager.handle_exec_op(
        {
            "op_id": "exec-ask",
            "runtime": "terminal",
            "session": 0,
            "code": "ask",
        }
    )
    replacement_result = await manager.handle_exec_op(
        {
            "op_id": "exec-reset-run",
            "runtime": "terminal",
            "session": 0,
            "code": "ansi",
            "reset": True,
        }
    )

    assert running_result["ok"] is True
    assert running_result["result"]["running"] is True
    assert len(created_shells) == 2
    assert created_shells[0].is_alive is False
    assert created_shells[0].commands == ["ask"]
    assert created_shells[1].commands == ["ansi"]
    assert replacement_result["ok"] is True
    assert replacement_result["result"]["output"] == "hello"
    assert replacement_result["result"]["running"] is False

    await manager.close()


@pytest.mark.skipif(os.name == "nt", reason="uses POSIX shell quoting")
async def test_shell_close_terminates_child_process_that_keeps_stdout_open(
    tmp_path: Path,
) -> None:
    shell = LocalShellSession(cwd=str(tmp_path))
    sleeper = "import time; print('ready', flush=True); time.sleep(30)"

    await shell.connect()
    await shell.send_command(f"{shlex.quote(sys.executable)} -c {shlex.quote(sleeper)}")
    output, _ = await shell.read_output(timeout=2)

    assert "ready" in output
    await asyncio.wait_for(shell.close(), timeout=5)


async def test_mutating_exec_runtimes_are_blocked_when_local_access_is_read_only(
    tmp_path: Path,
    created_shells: list[FakeShellSession],
) -> None:
    manager = RemoteExecManager(
        cwd=str(tmp_path),
        enabled=True,
        allow_writes=False,
        poll_interval=0.01,
    )

    result = await manager.handle_exec_op(
        {
            "op_id": "exec-read-only",
            "runtime": "terminal",
            "session": 0,
            "code": "ansi",
        }
    )

    assert result["ok"] is False
    assert "Press F3 to switch to Read&Write" in result["error"]
    assert created_shells == []

    await manager.close()
