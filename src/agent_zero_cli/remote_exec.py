from __future__ import annotations

import asyncio
import importlib
import importlib.util
import os
import re
import shlex
import sys
import time
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

_CORE_ROOT_ENV = "AGENT_ZERO_CORE_ROOT"
_CORE_ROOT_CANDIDATES = (
    "/a0",
)
_CORE_REQUIRED_FILES = (
    "helpers/runtime.py",
    "plugins/_code_execution/default_config.yaml",
    "plugins/_code_execution/helpers/tty_session.py",
    "plugins/_code_execution/helpers/shell_local.py",
    "plugins/_code_execution/helpers/shell_ssh.py",
)
_CORE_MODULE_NAMES = (
    "helpers",
    "helpers.runtime",
    "plugins",
    "plugins._code_execution",
    "plugins._code_execution.helpers",
    "plugins._code_execution.helpers.tty_session",
    "plugins._code_execution.helpers.shell_local",
    "plugins._code_execution.helpers.shell_ssh",
)
_CORE_OPTIONAL_STUB_MODULE_NAMES = (
    "helpers.dotenv",
    "helpers.rfc",
    "helpers.settings",
    "helpers.files",
    "helpers.log",
    "helpers.print_style",
)
_CORE_OPTIONAL_DEPENDENCY_STUB_NAMES = (
    "nest_asyncio",
    "paramiko",
)
_CORE_PACKAGE_PATHS = {
    "helpers": "helpers",
    "plugins": "plugins",
    "plugins._code_execution": "plugins/_code_execution",
    "plugins._code_execution.helpers": "plugins/_code_execution/helpers",
}
_HEX_ESCAPE_RE = re.compile(r"(?<!\\)\\x[0-9A-Fa-f]{2}")
_TIMEOUT_KEYS = (
    "first_output_timeout",
    "between_output_timeout",
    "max_exec_timeout",
    "dialog_timeout",
)
_SUPPORTED_RUNTIMES = ("terminal", "python", "nodejs", "output", "reset", "input")

_NO_CORE_ERROR = (
    "Remote execution is unavailable because Agent Zero Core could not be found. "
    "Set AGENT_ZERO_CORE_ROOT or ensure /a0 exists "
    "on this machine."
)
_EXEC_DISABLED_ERROR = (
    "Remote execution is disabled in this CLI session. Press F4 to switch exec on."
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


class CoreRuntimeUnavailableError(RuntimeError):
    """Raised when the local A0 Core tree needed for remote exec is unavailable."""


@dataclass(frozen=True)
class _CoreRuntime:
    root: Path
    terminal_executable: str
    get_terminal_executable: Callable[[], str]
    tty_session_type: type[Any]
    shell_session_type: type[Any]
    clean_string: Callable[[str], str]
    code_exec_timeouts: dict[str, int]
    output_timeouts: dict[str, int]
    prompt_patterns: list[re.Pattern[str]]
    dialog_patterns: list[re.Pattern[str]]


@dataclass
class _SessionState:
    shell: Any
    running: bool = False


_CORE_RUNTIME_CACHE: _CoreRuntime | None = None
_CORE_CREATED_STUBS: set[str] = set()


def _path_is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _resolve_core_root() -> Path | None:
    candidates: list[str] = []
    env_root = os.environ.get(_CORE_ROOT_ENV, "").strip()
    if env_root:
        candidates.append(env_root)
    candidates.extend(_CORE_ROOT_CANDIDATES)

    for candidate in candidates:
        root = Path(candidate).expanduser()
        if not root.exists():
            continue
        if all((root / rel_path).exists() for rel_path in _CORE_REQUIRED_FILES):
            return root.resolve()
    return None


def _prepare_core_imports(root: Path) -> None:
    root_str = str(root)
    if root_str in sys.path:
        sys.path.remove(root_str)
    sys.path.insert(0, root_str)

    for name in (*_CORE_MODULE_NAMES, *_CORE_OPTIONAL_STUB_MODULE_NAMES):
        module = sys.modules.get(name)
        if module is None:
            continue
        module_file = getattr(module, "__file__", None)
        if not module_file:
            sys.modules.pop(name, None)
            continue
        if not _path_is_relative_to(Path(module_file), root):
            sys.modules.pop(name, None)


def _ensure_core_package(root: Path, name: str) -> types.ModuleType:
    existing = sys.modules.get(name)
    package_path = str(root / _CORE_PACKAGE_PATHS[name])
    if existing is not None:
        paths = list(getattr(existing, "__path__", []))
        if package_path not in paths:
            existing.__path__ = [*paths, package_path]
        return existing

    module = types.ModuleType(name)
    module.__package__ = name
    module.__path__ = [package_path]
    sys.modules[name] = module

    parent_name, _, attr = name.rpartition(".")
    if parent_name:
        parent = _ensure_core_package(root, parent_name)
        setattr(parent, attr, module)

    return module


def _make_core_stub_module(name: str) -> types.ModuleType:
    module = types.ModuleType(name)

    if name == "helpers.rfc":
        module.RFCCall = type("RFCCall", (), {})
    elif name == "helpers.log":
        module.Log = type("Log", (), {})
    elif name == "helpers.print_style":
        module.PrintStyle = type(
            "PrintStyle",
            (),
            {"standard": staticmethod(lambda *args, **kwargs: None)},
        )
    elif name == "nest_asyncio":
        module.apply = lambda: None
    elif name == "paramiko":
        module.SSHClient = type("SSHClient", (), {})
        module.AutoAddPolicy = type("AutoAddPolicy", (), {})

    return module


def _install_core_import_stubs(root: Path) -> None:
    global _CORE_CREATED_STUBS

    helpers_pkg = _ensure_core_package(root, "helpers")
    for name in (
        "helpers.dotenv",
        "helpers.rfc",
        "helpers.settings",
        "helpers.files",
        "helpers.log",
        "helpers.print_style",
    ):
        module = _make_core_stub_module(name)
        sys.modules[name] = module
        setattr(helpers_pkg, name.rsplit(".", 1)[-1], module)
        _CORE_CREATED_STUBS.add(name)

    for name in _CORE_OPTIONAL_DEPENDENCY_STUB_NAMES:
        if importlib.util.find_spec(name) is not None:
            continue
        sys.modules[name] = _make_core_stub_module(name)
        _CORE_CREATED_STUBS.add(name)


class _StreamWithNoOpReconfigure:
    def __init__(self, wrapped: Any) -> None:
        self._wrapped = wrapped

    def reconfigure(self, **kwargs: Any) -> None:
        return None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._wrapped, name)


def _load_core_module(root: Path, name: str, relative_path: str) -> Any:
    parent_name, _, attr = name.rpartition(".")
    if parent_name:
        _ensure_core_package(root, parent_name)

    module_path = root / relative_path
    spec = importlib.util.spec_from_file_location(name, module_path)
    if spec is None or spec.loader is None:
        raise CoreRuntimeUnavailableError(
            f"Remote execution is unavailable because {module_path} could not be loaded."
        )

    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module

    if parent_name:
        setattr(sys.modules[parent_name], attr, module)

    original_stdin = sys.stdin
    original_stdout = sys.stdout
    patched_stdin = (
        original_stdin
        if hasattr(original_stdin, "reconfigure")
        else _StreamWithNoOpReconfigure(original_stdin)
    )
    patched_stdout = (
        original_stdout
        if hasattr(original_stdout, "reconfigure")
        else _StreamWithNoOpReconfigure(original_stdout)
    )

    try:
        if patched_stdin is not original_stdin:
            sys.stdin = patched_stdin  # type: ignore[assignment]
        if patched_stdout is not original_stdout:
            sys.stdout = patched_stdout  # type: ignore[assignment]
        spec.loader.exec_module(module)
    finally:
        sys.stdin = original_stdin
        sys.stdout = original_stdout
    return module


def _parse_scalar(raw_value: str) -> str | int | bool:
    value = raw_value.strip()
    if value.startswith(("'", '"')) and value.endswith(("'", '"')) and len(value) >= 2:
        return value[1:-1]

    lowered = value.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"

    try:
        return int(value)
    except ValueError:
        return value


def _parse_default_config(config_path: Path) -> dict[str, Any]:
    config: dict[str, Any] = {}
    lines = config_path.read_text(encoding="utf-8").splitlines()
    index = 0

    while index < len(lines):
        raw_line = lines[index]
        stripped = raw_line.strip()

        if not stripped or stripped.startswith("#"):
            index += 1
            continue

        if ":" not in raw_line:
            index += 1
            continue

        key, value = raw_line.split(":", 1)
        key = key.strip()
        value = value.strip()

        if value == "|":
            index += 1
            block_lines: list[str] = []
            while index < len(lines):
                next_line = lines[index]
                if next_line.startswith("  "):
                    block_lines.append(next_line[2:])
                    index += 1
                    continue
                if not next_line.strip():
                    block_lines.append("")
                    index += 1
                    continue
                break
            config[key] = "\n".join(block_lines).rstrip("\n")
            continue

        config[key] = _parse_scalar(value)
        index += 1

    return config


def _parse_patterns(raw: Any, flags: int = 0) -> list[re.Pattern[str]]:
    lines = [str(value) for value in raw] if isinstance(raw, list) else str(raw).splitlines()
    return [re.compile(line.strip(), flags) for line in lines if line.strip()]


def _parse_timeout_group(config: dict[str, Any], prefix: str) -> dict[str, int]:
    timeouts: dict[str, int] = {}
    for key in _TIMEOUT_KEYS:
        config_key = f"{prefix}_{key}"
        if config_key not in config:
            raise CoreRuntimeUnavailableError(
                f"Remote execution is unavailable because {config_key!r} is missing from "
                "Agent Zero Core _code_execution/default_config.yaml."
            )
        timeouts[key] = int(config[config_key])
    return timeouts


def _load_core_runtime() -> _CoreRuntime:
    global _CORE_RUNTIME_CACHE

    if _CORE_RUNTIME_CACHE is not None:
        return _CORE_RUNTIME_CACHE

    root = _resolve_core_root()
    if root is None:
        raise CoreRuntimeUnavailableError(_NO_CORE_ERROR)

    _prepare_core_imports(root)
    _install_core_import_stubs(root)

    try:
        runtime_mod = _load_core_module(root, "helpers.runtime", "helpers/runtime.py")
        tty_mod = _load_core_module(
            root,
            "plugins._code_execution.helpers.tty_session",
            "plugins/_code_execution/helpers/tty_session.py",
        )
        shell_ssh_mod = _load_core_module(
            root,
            "plugins._code_execution.helpers.shell_ssh",
            "plugins/_code_execution/helpers/shell_ssh.py",
        )
        shell_local_mod = _load_core_module(
            root,
            "plugins._code_execution.helpers.shell_local",
            "plugins/_code_execution/helpers/shell_local.py",
        )
        config = _parse_default_config(root / "plugins/_code_execution/default_config.yaml")
        terminal_executable = str(runtime_mod.get_terminal_executable())
    except Exception as exc:  # pragma: no cover - exercised via tests
        raise CoreRuntimeUnavailableError(
            "Remote execution is unavailable because Agent Zero Core could not be imported: "
            f"{exc}"
        ) from exc

    _CORE_RUNTIME_CACHE = _CoreRuntime(
        root=root,
        terminal_executable=terminal_executable,
        get_terminal_executable=runtime_mod.get_terminal_executable,
        tty_session_type=tty_mod.TTYSession,
        shell_session_type=shell_local_mod.LocalInteractiveSession,
        clean_string=shell_ssh_mod.clean_string,
        code_exec_timeouts=_parse_timeout_group(config, "code_exec"),
        output_timeouts=_parse_timeout_group(config, "output"),
        prompt_patterns=_parse_patterns(config.get("prompt_patterns", "")),
        dialog_patterns=_parse_patterns(config.get("dialog_patterns", ""), re.IGNORECASE),
    )
    return _CORE_RUNTIME_CACHE


def _reset_core_runtime_cache() -> None:
    global _CORE_RUNTIME_CACHE
    _CORE_RUNTIME_CACHE = None
    for name in _CORE_MODULE_NAMES:
        sys.modules.pop(name, None)
    for name in list(_CORE_CREATED_STUBS):
        sys.modules.pop(name, None)
    _CORE_CREATED_STUBS.clear()


class RemoteExecManager:
    def __init__(
        self,
        *,
        cwd: str,
        enabled: bool = True,
        poll_interval: float = 0.1,
    ) -> None:
        self.cwd = cwd
        self.enabled = enabled
        self.poll_interval = poll_interval
        self._sessions: dict[int, _SessionState] = {}

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled

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
            timeouts=_load_core_runtime().code_exec_timeouts,
        )

    async def execute_python(self, *, session: int, code: str) -> dict[str, Any]:
        escaped_code = shlex.quote(code)
        return await self._run_shell_command(
            session=session,
            command=f"ipython -c {escaped_code}",
            timeouts=_load_core_runtime().code_exec_timeouts,
        )

    async def execute_nodejs(self, *, session: int, code: str) -> dict[str, Any]:
        escaped_code = shlex.quote(code)
        return await self._run_shell_command(
            session=session,
            command=f"node /exe/node_eval.js {escaped_code}",
            timeouts=_load_core_runtime().code_exec_timeouts,
        )

    async def send_input(self, *, session: int, keyboard: str) -> dict[str, Any]:
        if session not in self._sessions or not self._sessions[session].running:
            raise ValueError(
                f"Session {session} is not awaiting input. runtime=input is a deprecated "
                "compatibility alias for sending a line into a running shell session."
            )

        state = self._sessions[session]
        await state.shell.send_command(keyboard.rstrip("\n"))
        state.running = True
        return await self._get_terminal_output(
            session=session,
            timeouts=_load_core_runtime().code_exec_timeouts,
            reset_full_output=True,
        )

    async def collect_output(self, *, session: int) -> dict[str, Any]:
        return await self._get_terminal_output(
            session=session,
            timeouts=_load_core_runtime().output_timeouts,
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
            reset_full_output=True,
        )

    async def _ensure_session(self, session: int, *, reset: bool = False) -> _SessionState:
        runtime = _load_core_runtime()

        if reset:
            existing = self._sessions.pop(session, None)
            if existing is not None:
                await existing.shell.close()

        existing = self._sessions.get(session)
        if existing is not None:
            return existing

        if not runtime.terminal_executable:
            raise CoreRuntimeUnavailableError(
                "Remote execution is unavailable because Agent Zero Core did not return "
                "a terminal executable."
            )

        shell = runtime.shell_session_type(cwd=self.cwd)
        await shell.connect()
        state = _SessionState(shell=shell, running=False)
        self._sessions[session] = state
        return state

    async def _handle_running_session(self, *, session: int) -> dict[str, Any] | None:
        state = self._sessions.get(session)
        if state is None or not state.running:
            return None

        runtime = _load_core_runtime()
        full_output, _ = await state.shell.read_output(timeout=1, reset_full_output=True)
        output = self._normalize_output(full_output)

        if self._detect_prompt(output, runtime.prompt_patterns):
            state.running = False
            return None

        message = _RUNNING_MESSAGE.format(session=session)
        if self._detect_dialog(output, runtime.dialog_patterns):
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
        runtime = _load_core_runtime()
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
                if self._detect_prompt(output, runtime.prompt_patterns):
                    state.running = False
                    completed_output = self._trim_prompt(output, runtime.prompt_patterns)
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
                and self._detect_dialog(output, runtime.dialog_patterns)
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
        runtime = _load_core_runtime()
        cleaned = runtime.clean_string(str(output))
        cleaned = _HEX_ESCAPE_RE.sub("", cleaned)
        return cleaned.strip()

    def _detect_prompt(
        self,
        output: str,
        patterns: list[re.Pattern[str]],
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
        patterns: list[re.Pattern[str]],
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
        patterns: list[re.Pattern[str]],
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
