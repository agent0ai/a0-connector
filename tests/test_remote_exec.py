from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

import agent_zero_cli.remote_exec as remote_exec_mod
from agent_zero_cli.remote_exec import RemoteExecManager


pytestmark = pytest.mark.anyio


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _make_fake_core_tree(root: Path) -> Path:
    _write(
        root / "helpers" / "runtime.py",
        textwrap.dedent(
            """
            def get_terminal_executable():
                return "/bin/fake-shell"
            """
        ).strip()
        + "\n",
    )
    _write(
        root / "plugins" / "_code_execution" / "helpers" / "tty_session.py",
        textwrap.dedent(
            """
            class TTYSession:
                def __init__(self, cmd, *, cwd=None, env=None, encoding="utf-8", echo=False):
                    self.cmd = cmd
                    self.cwd = cwd
                    self.env = env or {}
                    self.encoding = encoding
                    self.echo = echo
            """
        ).strip()
        + "\n",
    )
    _write(
        root / "plugins" / "_code_execution" / "helpers" / "shell_ssh.py",
        textwrap.dedent(
            r"""
            import re


            def clean_string(input_string):
                ansi_escape = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
                cleaned = ansi_escape.sub("", input_string)
                cleaned = cleaned.replace("\x00", "")
                cleaned = cleaned.replace("\r\n", "\n")
                cleaned = cleaned.replace("\r", "\n")
                return cleaned.strip("\n")
            """
        ).strip()
        + "\n",
    )
    _write(
        root / "plugins" / "_code_execution" / "helpers" / "shell_local.py",
        textwrap.dedent(
            """
            from helpers import runtime
            from plugins._code_execution.helpers import tty_session
            from plugins._code_execution.helpers.shell_ssh import clean_string


            class LocalInteractiveSession:
                def __init__(self, cwd=None):
                    self.session = None
                    self.full_output = ""
                    self.cwd = cwd
                    self._pending = []

                async def connect(self):
                    self.session = tty_session.TTYSession(
                        runtime.get_terminal_executable(),
                        cwd=self.cwd,
                    )

                async def close(self):
                    return None

                async def send_command(self, command):
                    self.full_output = ""
                    cmd = command.strip()
                    if cmd == "ansi":
                        self._pending = ["\\x1b[31mhello\\x1b[0m\\r\\nworkdir$ "]
                    elif cmd.startswith("ipython -c "):
                        self._pending = ["42\\r\\nworkdir$ "]
                    elif cmd.startswith("node /exe/node_eval.js "):
                        self._pending = ["node ok\\r\\nworkdir$ "]
                    else:
                        self._pending = [f"ran:{cmd}\\r\\nworkdir$ "]

                async def read_output(self, timeout=0, reset_full_output=False):
                    del timeout
                    if reset_full_output:
                        self.full_output = ""
                    if self._pending:
                        partial = self._pending.pop(0)
                        self.full_output += partial
                        return clean_string(self.full_output), clean_string(partial)
                    return clean_string(self.full_output), None
            """
        ).strip()
        + "\n",
    )
    _write(
        root / "plugins" / "_code_execution" / "default_config.yaml",
        textwrap.dedent(
            """
            code_exec_first_output_timeout: 0
            code_exec_between_output_timeout: 1
            code_exec_max_exec_timeout: 5
            code_exec_dialog_timeout: 0
            output_first_output_timeout: 0
            output_between_output_timeout: 1
            output_max_exec_timeout: 5
            output_dialog_timeout: 0
            prompt_patterns: |
              workdir\\$ ?$
            dialog_patterns: |
              \\?\\s*$
            """
        ).strip()
        + "\n",
    )
    return root


@pytest.fixture(autouse=True)
def _reset_core_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENT_ZERO_CORE_ROOT", raising=False)
    remote_exec_mod._reset_core_runtime_cache()
    yield
    remote_exec_mod._reset_core_runtime_cache()


def _manager(tmp_path: Path, *, enabled: bool = True) -> RemoteExecManager:
    return RemoteExecManager(cwd=str(tmp_path), enabled=enabled, poll_interval=0.01)


def test_resolve_core_root_prefers_env_var(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_root = _make_fake_core_tree(tmp_path / "env-root")
    fallback_root = _make_fake_core_tree(tmp_path / "fallback-root")

    monkeypatch.setenv("AGENT_ZERO_CORE_ROOT", str(env_root))
    monkeypatch.setattr(remote_exec_mod, "_CORE_ROOT_CANDIDATES", (str(fallback_root),))

    assert remote_exec_mod._resolve_core_root() == env_root.resolve()


async def test_missing_core_root_returns_unavailable_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        remote_exec_mod,
        "_CORE_ROOT_CANDIDATES",
        (str(tmp_path / "missing-a"), str(tmp_path / "missing-b")),
    )
    manager = _manager(tmp_path)

    result = await manager.handle_exec_op(
        {
            "op_id": "exec-missing",
            "runtime": "terminal",
            "session": 0,
            "code": "echo ready",
        }
    )

    assert result["ok"] is False
    assert "Agent Zero Core could not be found" in result["error"]


async def test_terminal_python_and_nodejs_runtimes_are_supported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_root = _make_fake_core_tree(tmp_path / "core-root")
    monkeypatch.setenv("AGENT_ZERO_CORE_ROOT", str(fake_root))
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
    assert terminal_result["result"]["running"] is False

    assert python_result["ok"] is True
    assert python_result["result"]["output"] == "42"
    assert python_result["result"]["running"] is False

    assert node_result["ok"] is True
    assert node_result["result"]["output"] == "node ok"
    assert node_result["result"]["running"] is False
