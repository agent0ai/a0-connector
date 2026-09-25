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


class _FakeDistribution:
    def __init__(self, direct_url_text: str | None) -> None:
        self._direct_url_text = direct_url_text

    def read_text(self, filename: str) -> str | None:
        assert filename == "direct_url.json"
        return self._direct_url_text


class _FakeReleaseResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def __enter__(self) -> _FakeReleaseResponse:
        return self

    def __exit__(self, *args: object) -> None:
        del args

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


@contextmanager
def _workspace_temp_dir() -> Path:
    temp_dir = Path(tempfile.mkdtemp())
    try:
        yield temp_dir
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def _load_updater_namespace() -> dict[str, object]:
    namespace = {"__name__": "agent_zero_cli_self_update_test"}
    exec(compile(self_update._build_updater_script(), "<a0-updater>", "exec"), namespace)
    return namespace


def test_resolve_package_spec_defaults_to_latest_release() -> None:
    package_spec = self_update.resolve_package_spec(
        {},
        latest_release_resolver=lambda: "v9.8",
    )

    assert package_spec == (
        "a0 @ https://github.com/agent0ai/a0-connector/archive/refs/tags/v9.8.zip"
    )


def test_fetch_latest_release_tag_parses_github_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, float]] = []

    def fake_urlopen(request: object, *, timeout: float) -> _FakeReleaseResponse:
        calls.append((request.full_url, timeout))  # type: ignore[attr-defined]
        return _FakeReleaseResponse({"tag_name": "v9.8"})

    monkeypatch.setattr(self_update, "urlopen", fake_urlopen)

    assert self_update.fetch_latest_release_tag(timeout=3.5) == "v9.8"
    assert calls == [(self_update.LATEST_RELEASE_API_URL, 3.5)]


def test_fetch_latest_release_tag_rejects_missing_tag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        self_update,
        "urlopen",
        lambda request, *, timeout: _FakeReleaseResponse({"name": "release"}),
    )

    with pytest.raises(self_update.LatestReleaseError):
        self_update.fetch_latest_release_tag()


def test_resolve_python_spec_defaults_to_managed_python_release() -> None:
    assert self_update.DEFAULT_PYTHON_SPEC == "3.12"
    assert self_update.resolve_python_spec({}) == self_update.DEFAULT_PYTHON_SPEC


def test_resolve_update_target_defaults_to_release_locks() -> None:
    target = self_update.resolve_update_target(
        {},
        latest_release_resolver=lambda: "v9.8",
    )

    assert target == self_update.UpdateTarget(
        package_spec=(
            "a0 @ https://github.com/agent0ai/a0-connector/archive/refs/tags/v9.8.zip"
        ),
        python_spec=self_update.DEFAULT_PYTHON_SPEC,
        runtime_constraints=(
            "https://raw.githubusercontent.com/agent0ai/a0-connector/"
            "refs/tags/v9.8/constraints/a0-runtime.txt"
        ),
        build_constraints=(
            "https://raw.githubusercontent.com/agent0ai/a0-connector/"
            "refs/tags/v9.8/constraints/a0-build.txt"
        ),
    )


def test_resolve_package_spec_honors_environment_override() -> None:
    env = {"A0_PACKAGE_SPEC": "a0 @ https://example.invalid/custom.zip"}
    resolver_calls = 0

    def resolver() -> str:
        nonlocal resolver_calls
        resolver_calls += 1
        return "v9.8"

    assert (
        self_update.resolve_package_spec(env, latest_release_resolver=resolver)
        == env["A0_PACKAGE_SPEC"]
    )
    assert resolver_calls == 0


def test_resolve_python_spec_honors_environment_override() -> None:
    env = {"A0_PYTHON_SPEC": "3.12"}
    assert self_update.resolve_python_spec(env) == env["A0_PYTHON_SPEC"]


def test_resolve_update_target_requires_locks_for_custom_package() -> None:
    env = {"A0_PACKAGE_SPEC": "a0 @ https://example.invalid/custom.zip"}

    with pytest.raises(self_update.LatestReleaseError, match="A0_PACKAGE_SPEC requires"):
        self_update.resolve_update_target(env)


def test_resolve_update_target_honors_custom_package_locks() -> None:
    env = {
        "A0_PACKAGE_SPEC": "a0 @ https://example.invalid/custom.zip",
        "A0_PYTHON_SPEC": "3.12",
        "A0_RUNTIME_CONSTRAINTS": "/tmp/runtime.txt",
        "A0_BUILD_CONSTRAINTS": "/tmp/build.txt",
    }

    assert self_update.resolve_update_target(env) == self_update.UpdateTarget(
        package_spec=env["A0_PACKAGE_SPEC"],
        python_spec=env["A0_PYTHON_SPEC"],
        runtime_constraints=env["A0_RUNTIME_CONSTRAINTS"],
        build_constraints=env["A0_BUILD_CONSTRAINTS"],
    )


def test_check_for_update_detects_newer_release() -> None:
    result = self_update.check_for_update(
        "1.10",
        {},
        latest_release_resolver=lambda: "v1.11",
        provenance_resolver=lambda: self_update.InstallProvenance(),
    )

    assert result == self_update.UpdateCheckResult(
        current_version="1.10",
        latest_version="1.11",
        latest_tag="v1.11",
        is_local_checkout=False,
    )


def test_check_for_update_ignores_current_release() -> None:
    result = self_update.check_for_update(
        "1.10",
        {},
        latest_release_resolver=lambda: "v1.10",
        provenance_resolver=lambda: self_update.InstallProvenance(editable=True),
    )

    assert result is None


def test_check_for_update_can_be_disabled_by_environment() -> None:
    resolver_calls = 0

    def resolver() -> str:
        nonlocal resolver_calls
        resolver_calls += 1
        return "v9.8"

    result = self_update.check_for_update(
        "1.10",
        {"A0_UPDATE_CHECK": "off"},
        latest_release_resolver=resolver,
    )

    assert result is None
    assert resolver_calls == 0


def test_format_update_available_message_mentions_local_checkout() -> None:
    message = self_update.format_update_available_message(
        self_update.UpdateCheckResult(
            current_version="1.10",
            latest_version="1.11",
            latest_tag="v1.11",
            is_local_checkout=True,
        )
    )

    assert "current checkout reports 1.10" in message
    assert "Pull this checkout" in message
    assert "`a0 update`" in message


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


def test_run_self_update_handoff_reports_latest_release_resolution_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with _workspace_temp_dir() as temp_dir:
        monkeypatch.setattr(self_update.shutil, "which", lambda name: "uv")
        monkeypatch.setattr(
            self_update,
            "fetch_latest_release_tag",
            lambda: (_ for _ in ()).throw(self_update.LatestReleaseError("offline")),
        )

        popen_calls: list[object] = []
        monkeypatch.setattr(
            self_update.subprocess,
            "Popen",
            lambda *args, **kwargs: popen_calls.append((args, kwargs)),
        )

        exit_code = self_update.run_self_update_handoff(temp_dir=temp_dir)

        captured = capsys.readouterr()
        assert exit_code == 1
        assert "Failed to resolve a locked a0 update target: offline" in captured.out
        assert "custom locked package source" in captured.out
        assert popen_calls == []
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
        run_calls: list[list[str]] = []

        def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
            run_calls.append(argv)
            assert kwargs == {"capture_output": True, "text": True, "check": False}
            return SimpleNamespace(
                returncode=0,
                stdout="C:/Users/example/AppData/Roaming/uv/python/cpython-3.11/python.exe\n",
                stderr="",
            )

        monkeypatch.setattr(self_update.subprocess, "run", fake_run)
        popen_calls: list[tuple[list[str], dict[str, object]]] = []

        def fake_popen(argv: list[str], **kwargs: object) -> SimpleNamespace:
            popen_calls.append((argv, kwargs))
            return SimpleNamespace(pid=4321)

        monkeypatch.setattr(self_update.subprocess, "Popen", fake_popen)
        env = {
            "A0_PACKAGE_SPEC": "a0 @ https://example.invalid/build.zip",
            "A0_PYTHON_SPEC": "3.12",
            "A0_RUNTIME_CONSTRAINTS": "https://example.invalid/runtime.txt",
            "A0_BUILD_CONSTRAINTS": "https://example.invalid/build.txt",
        }

        exit_code = self_update.run_self_update_handoff(env=env, temp_dir=temp_dir)

        captured = capsys.readouterr()
        assert exit_code == 0
        assert "standalone uv-managed tool channel" in captured.out
        assert "Handing off update to a separate process." in captured.out
        assert run_calls == [
            [
                "uv",
                "python",
                "find",
                env["A0_PYTHON_SPEC"],
                "--managed-python",
                "--no-project",
            ]
        ]
        assert len(popen_calls) == 1

        argv, kwargs = popen_calls[0]
        assert argv[0].endswith("python.exe")
        assert argv[2] == str(os.getpid())
        assert argv[3] == env["A0_PACKAGE_SPEC"]
        assert argv[4] == env["A0_PYTHON_SPEC"]
        assert argv[5] == env["A0_RUNTIME_CONSTRAINTS"]
        assert argv[6] == env["A0_BUILD_CONSTRAINTS"]
        assert kwargs["stdin"] is subprocess.DEVNULL

        script_path = Path(argv[1])
        assert script_path.parent == temp_dir
        script_text = script_path.read_text(encoding="utf-8")
        assert "agent_zero_cli" not in script_text
        assert '"tool"' in script_text
        assert '"--python"' in script_text
        assert '"--force"' in script_text
        assert "python_spec" in script_text
        assert '"--managed-python"' in script_text
        assert '"--upgrade-package"' in script_text
        assert '"--constraints"' in script_text
        assert '"--build-constraints"' in script_text
        assert "_uv_tool_install_supports" in script_text
        assert '"--upgrade"' not in script_text
        assert "package_spec" in script_text
        assert "Update complete. Run a0." in script_text


def test_build_updater_argv_uses_uv_run_when_managed_python_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_calls: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        del kwargs
        run_calls.append(argv)
        return SimpleNamespace(returncode=1, stdout="", stderr="missing")

    monkeypatch.setattr(self_update.subprocess, "run", fake_run)
    target = self_update.UpdateTarget(
        package_spec="a0 @ https://example.invalid/build.zip",
        python_spec="3.12",
        runtime_constraints=None,
        build_constraints=None,
    )

    argv = self_update._build_updater_argv(
        uv_executable="uv",
        script_path=Path("a0-update.py"),
        parent_pid=123,
        target=target,
    )

    assert run_calls == [
        ["uv", "python", "find", "3.12", "--managed-python", "--no-project"]
    ]
    assert argv == [
        "uv",
        "run",
        "--no-project",
        "--python",
        "3.12",
        "--managed-python",
        "--script",
        "a0-update.py",
        "123",
        target.package_spec,
        "3.12",
        "",
        "",
    ]


def test_generated_updater_script_waits_then_runs_uv_on_success(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    namespace = _load_updater_namespace()
    kill_results = iter([None, None, ProcessLookupError()])
    sleep_calls: list[float] = []
    run_calls: list[tuple[list[str], bool, bool, bool]] = []

    def fake_kill(pid: int, sig: int) -> None:
        result = next(kill_results)
        if isinstance(result, BaseException):
            raise result

    def fake_run(
        argv: list[str],
        *,
        check: bool,
        capture_output: bool = False,
        text: bool = False,
    ) -> SimpleNamespace:
        run_calls.append((argv, check, capture_output, text))
        if argv == ["uv", "tool", "install", "--help"]:
            return SimpleNamespace(returncode=0, stdout="--build-constraints", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(namespace["os"], "kill", fake_kill)
    monkeypatch.setattr(namespace["time"], "sleep", lambda seconds: sleep_calls.append(seconds))
    monkeypatch.setattr(namespace["shutil"], "which", lambda name: "uv")
    monkeypatch.setattr(namespace["subprocess"], "run", fake_run)

    with _workspace_temp_dir() as temp_dir:
        runtime_constraints = temp_dir / "runtime.txt"
        build_constraints = temp_dir / "build.txt"
        runtime_constraints.write_text("textual==8.2.7\n", encoding="utf-8")
        build_constraints.write_text("hatchling==1.29.0\n", encoding="utf-8")

        package_spec = self_update.package_spec_for_release_tag("v9.8")
        monkeypatch.setattr(namespace["os"], "name", "posix")
        exit_code = namespace["main"](
            [
                "123",
                package_spec,
                "3.11",
                str(runtime_constraints),
                str(build_constraints),
            ]
        )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert sleep_calls == [0.1, 0.1]
    assert run_calls == [
        (
            ["uv", "tool", "install", "--help"],
            False,
            True,
            True,
        ),
        (
            [
                "uv",
                "tool",
                "install",
                "--force",
                "--python",
                "3.11",
                "--managed-python",
                "--upgrade-package",
                "a0",
                "--constraints",
                str(runtime_constraints),
                "--build-constraints",
                str(build_constraints),
                package_spec,
            ],
            False,
            False,
            False,
        )
    ]
    assert captured.out.strip().endswith("Update complete. Run a0.")


def test_generated_updater_script_skips_build_constraint_for_old_uv(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    namespace = _load_updater_namespace()
    run_calls: list[list[str]] = []

    monkeypatch.setattr(namespace["shutil"], "which", lambda name: "uv")

    def fake_run(
        argv: list[str],
        *,
        check: bool,
        capture_output: bool = False,
        text: bool = False,
    ) -> SimpleNamespace:
        del check, capture_output, text
        run_calls.append(argv)
        if argv == ["uv", "tool", "install", "--help"]:
            return SimpleNamespace(returncode=0, stdout="--constraints", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(namespace["subprocess"], "run", fake_run)

    with _workspace_temp_dir() as temp_dir:
        runtime_constraints = temp_dir / "runtime.txt"
        build_constraints = temp_dir / "build.txt"
        runtime_constraints.write_text("textual==8.2.7\n", encoding="utf-8")
        build_constraints.write_text("hatchling==1.29.0\n", encoding="utf-8")

        package_spec = self_update.package_spec_for_release_tag("v9.8")
        exit_code = namespace["main"](
            [
                "0",
                package_spec,
                "3.11",
                str(runtime_constraints),
                str(build_constraints),
            ]
        )

    captured = capsys.readouterr()
    install_command = run_calls[-1]
    assert exit_code == 0
    assert "--constraints" in install_command
    assert "--build-constraints" not in install_command
    assert "does not support --build-constraints" in captured.out


def test_generated_updater_script_propagates_uv_exit_code_and_ignores_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script_source = self_update._build_updater_script()

    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(shutil, "which", lambda name: "uv")
    def fake_run(
        argv: list[str],
        *,
        check: bool,
        capture_output: bool = False,
        text: bool = False,
    ) -> SimpleNamespace:
        del check, capture_output, text
        if argv == ["uv", "tool", "install", "--help"]:
            return SimpleNamespace(returncode=0, stdout="--build-constraints", stderr="")
        return SimpleNamespace(returncode=7, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(Path, "unlink", lambda self: (_ for _ in ()).throw(OSError("locked")))
    monkeypatch.setattr(
        sys,
        "argv",
        ["a0-update-temp.py", "321", "a0 @ https://example.invalid/fail.zip", "3.11", "", ""],
    )

    with pytest.raises(SystemExit) as exc_info:
        exec(
            compile(script_source, "a0-update-temp.py", "exec"),
            {
                "__name__": "__main__",
                "__file__": "a0-update-temp.py",
            },
        )

    assert exc_info.value.code == 7
