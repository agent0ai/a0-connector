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


def _make_fake_core_tree(
    root: Path,
    *,
    code_between_timeout: int = 1,
    output_between_timeout: int = 1,
    dialog_timeout: int = 0,
) -> Path:
    for package_init in (
        root / "helpers" / "__init__.py",
        root / "plugins" / "__init__.py",
        root / "plugins" / "_code_execution" / "__init__.py",
        root / "plugins" / "_code_execution" / "helpers" / "__init__.py",
    ):
        _write(package_init, "")

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
                    self._delayed = []
                    self._idle_reads = 0
                    self._waiting_input = False

                async def connect(self):
                    self.session = tty_session.TTYSession(
                        runtime.get_terminal_executable(),
                        cwd=self.cwd,
                    )

                async def close(self):
                    return None

                async def send_command(self, command):
                    if self._waiting_input:
                        self._waiting_input = False
                        name = command.strip() or "friend"
                        self.full_output = ""
                        self._pending = [f"Hello {name}\\r\\nworkdir$ "]
                        self._delayed = []
                        self._idle_reads = 0
                        return

                    self.full_output = ""
                    self._pending = []
                    self._delayed = []
                    self._idle_reads = 0

                    cmd = command.strip()
                    if cmd == "echo ready":
                        self._pending = ["ready\\r\\nworkdir$ "]
                    elif cmd == "ansi":
                        self._pending = ["\\x1b[31mhello\\x1b[0m\\r\\nworkdir$ "]
                    elif cmd == "slowcmd":
                        self._pending = ["tick\\r\\n"]
                        self._delayed = ["done\\r\\nworkdir$ "]
                    elif cmd == "dialogcmd":
                        self._pending = ["Continue? "]
                    elif cmd.startswith("ipython -c "):
                        if "input(" in command:
                            self._waiting_input = True
                            self._pending = ["Name? "]
                        else:
                            self._pending = ["42\\r\\nworkdir$ "]
                    elif cmd.startswith("node /exe/node_eval.js "):
                        self._pending = ["node ok\\r\\nworkdir$ "]
                    else:
                        self._pending = [f"ran:{cmd}\\r\\nworkdir$ "]

                async def read_output(self, timeout=0, reset_full_output=False):
                    if reset_full_output:
                        self.full_output = ""

                    if self._pending:
                        partial = self._pending.pop(0)
                        self.full_output += partial
                        return clean_string(self.full_output), clean_string(partial)

                    if self._delayed:
                        self._idle_reads += 1
                        if self._idle_reads >= 2:
                            partial = self._delayed.pop(0)
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
            f"""
            ssh_enabled: false
            ssh_addr: ""
            ssh_port: 55022
            ssh_user: root
            ssh_pass: ""
            code_exec_first_output_timeout: 0
            code_exec_between_output_timeout: {code_between_timeout}
            code_exec_max_exec_timeout: 5
            code_exec_dialog_timeout: {dialog_timeout}
            output_first_output_timeout: 0
            output_between_output_timeout: {output_between_timeout}
            output_max_exec_timeout: 5
            output_dialog_timeout: {dialog_timeout}
            prompt_patterns: |
              workdir\\$ ?$
            dialog_patterns: |
              \\?\\s*$
              :\\s*$
            """
        ).strip()
        + "\n",
    )
    return root


def _make_realistic_core_tree(root: Path) -> Path:
    _write(
        root / "helpers" / "runtime.py",
        textwrap.dedent(
            """
            from helpers import dotenv, rfc, settings, files
            import nest_asyncio

            nest_asyncio.apply()


            async def handle_rfc(rfc_call: rfc.RFCCall):
                return None


            def get_terminal_executable():
                return "/bin/realistic-shell"
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
            import paramiko
            from helpers.log import Log
            from helpers.print_style import PrintStyle
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
                    self._pending = [f"ran:{command.strip()}\\r\\nworkdir$ "]

                async def read_output(self, timeout=0, reset_full_output=False):
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


def _manager(tmp_path: Path, *, enabled: bool = True, poll_interval: float = 0.01) -> RemoteExecManager:
    return RemoteExecManager(cwd=str(tmp_path), enabled=enabled, poll_interval=poll_interval)


def test_resolve_core_root_prefers_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_root = _make_fake_core_tree(tmp_path / "env-root")
    fallback_root = _make_fake_core_tree(tmp_path / "fallback-root")

    monkeypatch.setenv("AGENT_ZERO_CORE_ROOT", str(env_root))
    monkeypatch.setattr(
        remote_exec_mod,
        "_CORE_ROOT_CANDIDATES",
        (str(fallback_root), str(tmp_path / "unused-root")),
    )

    assert remote_exec_mod._resolve_core_root() == env_root.resolve()


def test_resolve_core_root_falls_back_through_candidates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fallback_root = _make_fake_core_tree(tmp_path / "fallback-root")

    monkeypatch.setattr(
        remote_exec_mod,
        "_CORE_ROOT_CANDIDATES",
        (str(tmp_path / "missing-root"), str(fallback_root)),
    )

    assert remote_exec_mod._resolve_core_root() == fallback_root.resolve()


def test_core_runtime_imports_are_cached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_root = _make_fake_core_tree(tmp_path / "core-root")
    monkeypatch.setenv("AGENT_ZERO_CORE_ROOT", str(fake_root))

    first = remote_exec_mod._load_core_runtime()
    second = remote_exec_mod._load_core_runtime()

    assert first is second
    assert first.root == fake_root.resolve()
    assert first.terminal_executable == "/bin/fake-shell"


def test_core_runtime_imports_realistic_core_helpers_without_extra_dependencies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_root = _make_realistic_core_tree(tmp_path / "core-root")
    monkeypatch.setenv("AGENT_ZERO_CORE_ROOT", str(fake_root))

    runtime = remote_exec_mod._load_core_runtime()

    assert runtime.terminal_executable == "/bin/realistic-shell"
    assert runtime.clean_string("\x1b[31mhello\x1b[0m") == "hello"


def test_core_runtime_imports_when_stdio_lacks_reconfigure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_root = _make_realistic_core_tree(tmp_path / "core-root")
    monkeypatch.setenv("AGENT_ZERO_CORE_ROOT", str(fake_root))

    class DummyStream:
        def write(self, *_args, **_kwargs) -> None:
            return None

        def flush(self) -> None:
            return None

    monkeypatch.setattr(remote_exec_mod.sys, "stdin", DummyStream())
    monkeypatch.setattr(remote_exec_mod.sys, "stdout", DummyStream())

    runtime = remote_exec_mod._load_core_runtime()

    assert runtime.terminal_executable == "/bin/realistic-shell"


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


async def test_terminal_runtime_uses_shell_session_and_cleans_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_root = _make_fake_core_tree(tmp_path / "core-root")
    monkeypatch.setenv("AGENT_ZERO_CORE_ROOT", str(fake_root))
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
    payload = result["result"]
    assert payload["running"] is False
    assert payload["message"] == "Session 0 completed."
    assert payload["output"] == "hello"


async def test_python_and_nodejs_runtimes_are_supported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_root = _make_fake_core_tree(tmp_path / "core-root")
    monkeypatch.setenv("AGENT_ZERO_CORE_ROOT", str(fake_root))
    manager = _manager(tmp_path)

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

    assert python_result["ok"] is True
    assert python_result["result"]["output"] == "42"
    assert python_result["result"]["running"] is False

    assert node_result["ok"] is True
    assert node_result["result"]["output"] == "node ok"
    assert node_result["result"]["running"] is False


async def test_output_runtime_continues_running_shell_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_root = _make_fake_core_tree(
        tmp_path / "core-root",
        code_between_timeout=0,
        output_between_timeout=1,
    )
    monkeypatch.setenv("AGENT_ZERO_CORE_ROOT", str(fake_root))
    manager = _manager(tmp_path)

    started = await manager.handle_exec_op(
        {
            "op_id": "exec-slow-start",
            "runtime": "terminal",
            "session": 3,
            "code": "slowcmd",
        }
    )
    finished = await manager.handle_exec_op(
        {
            "op_id": "exec-slow-output",
            "runtime": "output",
            "session": 3,
        }
    )

    assert started["ok"] is True
    assert started["result"]["running"] is True
    assert "tick" in started["result"]["output"]
    assert "last output update" in started["result"]["message"]

    assert finished["ok"] is True
    assert finished["result"]["running"] is False
    assert "done" in finished["result"]["output"]


async def test_input_runtime_alias_sends_line_into_running_shell_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_root = _make_fake_core_tree(
        tmp_path / "core-root",
        code_between_timeout=1,
        output_between_timeout=1,
        dialog_timeout=0,
    )
    monkeypatch.setenv("AGENT_ZERO_CORE_ROOT", str(fake_root))
    manager = _manager(tmp_path)

    started = await manager.handle_exec_op(
        {
            "op_id": "exec-input-start",
            "runtime": "python",
            "session": 4,
            "code": "name = input('Name? ')\nprint(f'Hello {name}')",
        }
    )
    resumed = await manager.handle_exec_op(
        {
            "op_id": "exec-input-finish",
            "runtime": "input",
            "session": 4,
            "keyboard": "Ada",
        }
    )

    assert started["ok"] is True
    assert started["result"]["running"] is True
    assert "Name?" in started["result"]["output"]
    assert "Potential dialog detected" in started["result"]["message"]

    assert resumed["ok"] is True
    assert resumed["result"]["running"] is False
    assert "Hello Ada" in resumed["result"]["output"]


async def test_dialog_detection_returns_control_early(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_root = _make_fake_core_tree(
        tmp_path / "core-root",
        code_between_timeout=1,
        output_between_timeout=1,
        dialog_timeout=0,
    )
    monkeypatch.setenv("AGENT_ZERO_CORE_ROOT", str(fake_root))
    manager = _manager(tmp_path)

    result = await manager.handle_exec_op(
        {
            "op_id": "exec-dialog",
            "runtime": "terminal",
            "session": 5,
            "code": "dialogcmd",
        }
    )

    assert result["ok"] is True
    assert result["result"]["running"] is True
    assert "Continue?" in result["result"]["output"]
    assert "Potential dialog detected" in result["result"]["message"]


async def test_reset_recreates_session_instead_of_deleting_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_root = _make_fake_core_tree(
        tmp_path / "core-root",
        code_between_timeout=1,
        output_between_timeout=0,
    )
    monkeypatch.setenv("AGENT_ZERO_CORE_ROOT", str(fake_root))
    manager = _manager(tmp_path)

    await manager.handle_exec_op(
        {
            "op_id": "exec-before-reset",
            "runtime": "terminal",
            "session": 6,
            "code": "echo ready",
        }
    )
    reset = await manager.handle_exec_op(
        {
            "op_id": "exec-reset",
            "runtime": "reset",
            "session": 6,
            "reason": "test cleanup",
        }
    )
    output_after_reset = await manager.handle_exec_op(
        {
            "op_id": "exec-after-reset",
            "runtime": "output",
            "session": 6,
        }
    )

    assert reset["ok"] is True
    assert "Terminal session has been reset." in reset["result"]["message"]

    assert output_after_reset["ok"] is True
    assert output_after_reset["result"]["running"] is True
    assert "with no output" in output_after_reset["result"]["message"]
