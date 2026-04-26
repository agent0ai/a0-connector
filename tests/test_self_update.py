from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace

import pytest

from agent_zero_cli import self_update


ROOT = Path(__file__).resolve().parents[1]


class _FakeDistribution:
    def __init__(self, direct_url_text: str | None) -> None:
        self._direct_url_text = direct_url_text

    def read_text(self, filename: str) -> str | None:
        assert filename == "direct_url.json"
        return self._direct_url_text


@contextmanager
def _workspace_temp_dir() -> Path:
    base_dir = ROOT / ".tmp-tests"
    base_dir.mkdir(exist_ok=True)
    temp_dir = Path(tempfile.mkdtemp(dir=base_dir))
    try:
        yield temp_dir
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def _load_updater_namespace() -> dict[str, object]:
    namespace = {"__name__": "agent_zero_cli_self_update_test"}
    exec(compile(self_update._build_updater_script(), "<a0-updater>", "exec"), namespace)
    return namespace


def test_resolve_package_spec_defaults_to_stable_release() -> None:
    assert self_update.resolve_package_spec({}) == self_update.DEFAULT_PACKAGE_SPEC


def test_resolve_python_spec_defaults_to_managed_python_release() -> None:
    assert self_update.resolve_python_spec({}) == self_update.DEFAULT_PYTHON_SPEC


def test_resolve_package_spec_honors_environment_override() -> None:
    env = {"A0_PACKAGE_SPEC": "a0 @ https://example.invalid/custom.zip"}
    assert self_update.resolve_package_spec(env) == env["A0_PACKAGE_SPEC"]


def test_resolve_python_spec_honors_environment_override() -> None:
    env = {"A0_PYTHON_SPEC": "3.12"}
    assert self_update.resolve_python_spec(env) == env["A0_PYTHON_SPEC"]


def test_detect_install_provenance_flags_local_editable_checkout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    direct_url = json.dumps(
        {
            "url": "file:///C:/Users/example/src/a0-connector",
            "dir_info": {"editable": True},
        }
    )
    monkeypatch.setattr(
        self_update.metadata,
        "distribution",
        lambda name: _FakeDistribution(direct_url),
    )

    provenance = self_update.detect_install_provenance()

    assert provenance.editable is True
    assert provenance.is_local_checkout is True
    assert provenance.local_path is not None
    assert "a0-connector" in provenance.local_path


def test_run_self_update_handoff_requires_uv(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with _workspace_temp_dir() as temp_dir:
        monkeypatch.setattr(self_update.shutil, "which", lambda name: None)

        exit_code = self_update.run_self_update_handoff(temp_dir=temp_dir)

        captured = capsys.readouterr()
        assert exit_code == 1
        assert "Install uv or rerun the existing installer." in captured.out
        assert list(temp_dir.iterdir()) == []


def test_run_self_update_handoff_writes_script_and_spawns_updater(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with _workspace_temp_dir() as temp_dir:
        direct_url = json.dumps(
            {
                "url": "file:///C:/Users/example/src/a0-connector",
                "dir_info": {"editable": True},
            }
        )
        monkeypatch.setattr(
            self_update.metadata,
            "distribution",
            lambda name: _FakeDistribution(direct_url),
        )
        monkeypatch.setattr(self_update.shutil, "which", lambda name: "uv")

        calls: list[tuple[list[str], dict[str, object]]] = []

        def fake_popen(argv: list[str], **kwargs: object) -> SimpleNamespace:
            calls.append((argv, kwargs))
            return SimpleNamespace(pid=4321)

        monkeypatch.setattr(self_update.subprocess, "Popen", fake_popen)
        env = {
            "A0_PACKAGE_SPEC": "a0 @ https://example.invalid/build.zip",
            "A0_PYTHON_SPEC": "3.12",
        }

        exit_code = self_update.run_self_update_handoff(env=env, temp_dir=temp_dir)

        captured = capsys.readouterr()
        assert exit_code == 0
        assert "standalone uv-managed tool channel" in captured.out
        assert "Handing off update to a separate process." in captured.out
        assert len(calls) == 1

        argv, kwargs = calls[0]
        assert argv[0] == sys.executable
        assert argv[2] == str(os.getpid())
        assert argv[3] == env["A0_PACKAGE_SPEC"]
        assert argv[4] == env["A0_PYTHON_SPEC"]
        assert kwargs["stdin"] is subprocess.DEVNULL

        script_path = Path(argv[1])
        assert script_path.parent == temp_dir
        script_text = script_path.read_text(encoding="utf-8")
        assert "agent_zero_cli" not in script_text
        assert '"tool"' in script_text
        assert '"--python"' in script_text
        assert "python_spec" in script_text
        assert '"--managed-python"' in script_text
        assert '"--upgrade"' in script_text
        assert "package_spec" in script_text
        assert "Update complete. Run a0." in script_text


def test_generated_updater_script_waits_then_runs_uv_on_success(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    namespace = _load_updater_namespace()
    kill_results = iter([None, None, ProcessLookupError()])
    sleep_calls: list[float] = []
    run_calls: list[tuple[list[str], bool]] = []

    monkeypatch.setattr(namespace["os"], "name", "posix")

    def fake_kill(pid: int, sig: int) -> None:
        result = next(kill_results)
        if isinstance(result, BaseException):
            raise result

    def fake_run(argv: list[str], *, check: bool) -> SimpleNamespace:
        run_calls.append((argv, check))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(namespace["os"], "kill", fake_kill)
    monkeypatch.setattr(namespace["time"], "sleep", lambda seconds: sleep_calls.append(seconds))
    monkeypatch.setattr(namespace["shutil"], "which", lambda name: "uv")
    monkeypatch.setattr(namespace["subprocess"], "run", fake_run)

    exit_code = namespace["main"](["123", self_update.DEFAULT_PACKAGE_SPEC, "3.11"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert sleep_calls == [0.1, 0.1]
    assert run_calls == [
        (
            [
                "uv",
                "tool",
                "install",
                "--python",
                "3.11",
                "--managed-python",
                "--upgrade",
                self_update.DEFAULT_PACKAGE_SPEC,
            ],
            False,
        )
    ]
    assert captured.out.strip().endswith("Update complete. Run a0.")


def test_generated_updater_script_propagates_uv_exit_code_and_ignores_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script_source = self_update._build_updater_script()

    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(shutil, "which", lambda name: "uv")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda argv, *, check: SimpleNamespace(returncode=7),
    )
    monkeypatch.setattr(Path, "unlink", lambda self: (_ for _ in ()).throw(OSError("locked")))
    monkeypatch.setattr(sys, "argv", ["a0-update-temp.py", "321", "a0 @ https://example.invalid/fail.zip", "3.11"])

    with pytest.raises(SystemExit) as exc_info:
        exec(
            compile(script_source, "a0-update-temp.py", "exec"),
            {
                "__name__": "__main__",
                "__file__": "a0-update-temp.py",
            },
        )

    assert exc_info.value.code == 7
