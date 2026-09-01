from __future__ import annotations

import asyncio
import base64
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from agent_zero_cli import computer_use_backend as backend_mod
import agent_zero_cli.computer_use as computer_use_mod
from agent_zero_cli.computer_use import (
    ComputerUseManager,
    _HELPER_STDIO_LIMIT,
    _HelperSession,
)
from agent_zero_cli.computer_use_backend import (
    COMPUTER_USE_CONTRACT_VERSION,
    ComputerUseBackendSelection,
    ComputerUseBackendSpec,
)
from agent_zero_cli.config import CLIConfig


pytestmark = pytest.mark.anyio


@pytest.fixture
def _temp_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    env_dir = tmp_path / ".agent-zero"
    env_dir.mkdir()
    env_file = env_dir / ".env"
    artifact_root = tmp_path / "computer-use-artifacts"

    import agent_zero_cli.config as config_mod

    monkeypatch.setattr(config_mod, "_ENV_DIR", env_dir)
    monkeypatch.setattr(config_mod, "_ENV_FILE", env_file)
    monkeypatch.setattr(computer_use_mod, "HOST_ARTIFACT_ROOT", artifact_root)
    monkeypatch.setattr(computer_use_mod, "CONTAINER_ARTIFACT_ROOT", "/a0/test-computer-use")
    return env_file


class _FakeHelperStdin:
    def __init__(self) -> None:
        self.buffer = bytearray()

    def write(self, data: bytes) -> None:
        self.buffer.extend(data)

    async def drain(self) -> None:
        return None


class _StuckHelperStdin(_FakeHelperStdin):
    async def drain(self) -> None:
        await asyncio.Event().wait()


class _FakeHelperProcess:
    def __init__(self, stdout_lines: list[str]) -> None:
        self.stdin = _FakeHelperStdin()
        self.stdout = asyncio.StreamReader()
        for line in stdout_lines:
            self.stdout.feed_data(line.encode("utf-8"))
        self.stdout.feed_eof()
        self.returncode = None


class _FakeStream:
    async def readline(self) -> bytes:
        return b""


class _FakeLineStream:
    def __init__(self, lines: list[str]) -> None:
        self._lines = [line.encode("utf-8") for line in lines]

    async def readline(self) -> bytes:
        if not self._lines:
            return b""
        return self._lines.pop(0)


def _manager(
    *,
    enabled: bool = False,
    trust_mode: str = "persistent",
    restore_token: str = "",
    backend_selection: ComputerUseBackendSelection | None = None,
    supported: bool | None = True,
) -> ComputerUseManager:
    manager = ComputerUseManager(
        CLIConfig(
            computer_use_enabled=enabled,
            computer_use_trust_mode=trust_mode,
            computer_use_restore_token=restore_token,
        ),
        backend_selection=backend_selection or _selection(),
    )
    if supported is not None:
        manager.supported = supported
    return manager


def _backend_spec(
    *,
    backend_id: str = "wayland-test",
    backend_family: str = "linux",
    priority: int = 10,
    detected: bool = True,
    features: tuple[str, ...] = ("inline-png-capture",),
    support_reason: str = "backend is available",
    helper_target: str = "/tmp/computer_use_helper.py",
) -> ComputerUseBackendSpec:
    return ComputerUseBackendSpec(
        backend_id=backend_id,
        backend_family=backend_family,
        priority=priority,
        detect=lambda: detected,
        features=features,
        interpreter_strategy="system_python",
        helper_target=helper_target,
        trust_mode_support=("interactive", "persistent", "allow"),
        support_reason=lambda: support_reason,
    )


def _selection(
    *,
    backend_id: str = "wayland-test",
    backend_family: str = "linux",
    priority: int = 10,
    detected: bool = True,
    features: tuple[str, ...] = ("inline-png-capture",),
    support_reason: str = "backend is available",
    helper_target: str = "/tmp/computer_use_helper.py",
) -> ComputerUseBackendSelection:
    spec = _backend_spec(
        backend_id=backend_id,
        backend_family=backend_family,
        priority=priority,
        detected=detected,
        features=features,
        support_reason=support_reason,
        helper_target=helper_target,
    )
    return ComputerUseBackendSelection(
        spec=spec,
        supported=detected,
        support_reason=support_reason,
    )


def test_default_host_artifact_root_honors_explicit_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_root = tmp_path / "configured-artifacts"
    monkeypatch.setenv(computer_use_mod._HOST_ARTIFACT_ROOT_ENV, str(artifact_root))

    host_root = computer_use_mod._default_host_artifact_root("/a0/tmp/_a0_connector/computer_use")

    assert host_root == artifact_root


def test_default_host_artifact_root_uses_tempdir_fallback_on_macos(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(computer_use_mod._HOST_ARTIFACT_ROOT_ENV, raising=False)
    monkeypatch.setattr(computer_use_mod.sys, "platform", "darwin")
    monkeypatch.setattr(computer_use_mod.tempfile, "gettempdir", lambda: str(tmp_path))

    host_root = computer_use_mod._default_host_artifact_root("/a0/tmp/_a0_connector/computer_use")

    assert host_root == tmp_path / "_a0_connector" / "computer_use"


def test_default_host_artifact_root_uses_tempdir_fallback_on_linux(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(computer_use_mod._HOST_ARTIFACT_ROOT_ENV, raising=False)
    monkeypatch.setattr(computer_use_mod.sys, "platform", "linux")
    monkeypatch.setattr(computer_use_mod.tempfile, "gettempdir", lambda: str(tmp_path))

    host_root = computer_use_mod._default_host_artifact_root("/a0/tmp/_a0_connector/computer_use")

    assert host_root == tmp_path / "_a0_connector" / "computer_use"


async def test_status_is_allowed_while_disabled_but_other_actions_are_rejected(
    _temp_env: Path,
) -> None:
    manager = _manager(enabled=False)

    status = await manager.handle_op({"op_id": "status-1", "action": "status", "context_id": "ctx-1"})
    rejected = await manager.handle_op({"op_id": "move-1", "action": "move", "context_id": "ctx-1", "x": 0.2, "y": 0.4})

    assert status["ok"] is True
    assert status["result"]["status"] == "disabled"
    assert rejected["ok"] is False
    assert rejected["code"] == "COMPUTER_USE_DISABLED"


async def test_launcher_tag_uses_private_backend_actions_without_remote_advertising(
    _temp_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(
        enabled=True,
        backend_selection=_selection(features=("inline-png-capture", "a0-tag")),
    )

    async def fake_start(op_id: str, session: _HelperSession) -> dict[str, object]:
        session.active = True
        session.session_id = "tag-session"
        return {"op_id": op_id, "ok": True, "result": {"session_id": session.session_id}}

    dispatched: list[dict[str, object]] = []

    async def fake_dispatch(
        op_id: str,
        session: _HelperSession,
        request: dict[str, object],
    ) -> dict[str, object]:
        assert session.session_id == "tag-session"
        dispatched.append(dict(request))
        return {"op_id": op_id, "ok": True, "result": {"action": request["action"]}}

    monkeypatch.setattr(manager, "_start_session", fake_start)
    monkeypatch.setattr(manager, "_dispatch_session_action", fake_dispatch)

    assert manager.launcher_tag_supported is True
    assert "tag_context" not in computer_use_mod._SUPPORTED_ACTIONS
    assert (await manager.tag_context())["ok"] is True
    assert (await manager.tag_replace("target-1", "reply"))["ok"] is True
    assert (await manager.tag_release("target-1"))["ok"] is True
    assert manager._sessions["launcher-tag"].active is False
    assert dispatched == [
        {"action": "tag_context", "context_id": "launcher-tag"},
        {
            "action": "tag_replace",
            "context_id": "launcher-tag",
            "target_token": "target-1",
            "replacement": "reply",
        },
        {
            "action": "tag_release",
            "context_id": "launcher-tag",
            "target_token": "target-1",
        },
    ]


async def test_launcher_tag_rejects_unadvertised_backend(_temp_env: Path) -> None:
    manager = _manager(enabled=True, backend_selection=_selection())

    result = await manager.tag_context()

    assert result["ok"] is False
    assert result["code"] == "COMPUTER_USE_UNSUPPORTED"


async def test_launcher_tag_capture_failure_closes_its_private_session(
    _temp_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(
        enabled=True,
        backend_selection=_selection(features=("inline-png-capture", "a0-tag")),
    )
    session = manager._sessions.setdefault("launcher-tag", _HelperSession(context_id="launcher-tag"))
    session.active = True
    session.session_id = "tag-session"

    async def failed_action(_action: str, **_payload: object) -> dict[str, object]:
        return {"ok": False, "code": "A0_TAG_NOT_FOUND", "error": "No tag found."}

    stopped: list[str] = []

    async def stop_session(_op_id: str, current: _HelperSession) -> dict[str, object]:
        stopped.append(current.context_id)
        current.active = False
        current.session_id = ""
        return {"ok": True, "result": {"status": "stopped"}}

    monkeypatch.setattr(manager, "_tag_action", failed_action)
    monkeypatch.setattr(manager, "_stop_session", stop_session)

    result = await manager.tag_context()

    assert result["code"] == "A0_TAG_NOT_FOUND"
    assert stopped == ["launcher-tag"]
    assert session.active is False


async def test_macos_launcher_tag_start_does_not_require_screen_recording_setup(
    _temp_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(
        enabled=True,
        trust_mode="persistent",
        restore_token="123e4567-e89b-12d3-a456-426614174000",
        backend_selection=_selection(
            backend_id="macos",
            backend_family="macos",
            features=("inline-png-capture", "a0-tag"),
        ),
    )
    prepare = AsyncMock(side_effect=AssertionError("tag startup must not require Screen Recording"))

    async def helper_request(
        _session: _HelperSession,
        request: dict[str, object],
    ) -> dict[str, object]:
        assert request["action"] == "start_session"
        assert request["context_id"] == "launcher-tag"
        return {
            "ok": True,
            "result": {
                "context_id": "launcher-tag",
                "session_id": "tag-session",
                "status": "active",
                "active": True,
            },
        }

    monkeypatch.setattr(manager, "_prepare_macos_permissions", prepare)
    monkeypatch.setattr(manager, "_helper_request", helper_request)

    session = _HelperSession(context_id="launcher-tag")
    result = await manager._start_session("tag-start", session)

    assert result["ok"] is True
    assert session.active is True
    assert session.session_id == "tag-session"
    prepare.assert_not_awaited()


async def test_allow_without_restore_token_returns_rearm_required(
    _temp_env: Path,
) -> None:
    manager = _manager(enabled=True, trust_mode="allow")

    result = await manager.handle_op({"op_id": "start-1", "action": "start_session", "context_id": "ctx-1"})

    assert result["ok"] is False
    assert result["code"] == "COMPUTER_USE_REARM_REQUIRED"
    assert "desktop-control backend is not armed" in result["error"]
    assert manager.status_label == "rearm required"
    assert manager.hello_metadata()["status"] == "rearm required"
    assert manager.hello_metadata()["restore_token_present"] is False


async def test_windows_rearm_uses_arming_status_without_approval_prompt(
    _temp_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(
        enabled=True,
        trust_mode="allow",
        backend_selection=_selection(backend_id="windows", backend_family="windows"),
    )
    statuses: list[str] = []
    manager.set_status_callback(lambda status, detail: statuses.append(status))

    async def fake_start_session(op_id: str, session: _HelperSession) -> dict[str, object]:
        del op_id
        assert statuses[-1] == "arming"
        session.active = True
        session.session_id = "sess-windows"
        session.session_result = {
            "session_id": "sess-windows",
            "status": "active",
            "active": True,
        }
        return {"op_id": "rearm-test", "ok": True, "result": dict(session.session_result)}

    monkeypatch.setattr(manager, "_start_session", fake_start_session)

    result = await manager.rearm(context_id="ctx-win")

    assert result["ok"] is True
    assert "approval required" not in statuses
    assert "arming" in statuses
    assert statuses[-1] == "active"


async def test_prompt_backends_rearm_keep_approval_required_status(
    _temp_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(enabled=True, trust_mode="allow")
    statuses: list[str] = []
    manager.set_status_callback(lambda status, detail: statuses.append(status))

    async def fake_start_session(op_id: str, session: _HelperSession) -> dict[str, object]:
        del op_id
        assert statuses[-1] == "approval required"
        session.active = True
        session.session_id = "sess-linux"
        session.session_result = {
            "session_id": "sess-linux",
            "status": "active",
            "active": True,
        }
        return {"op_id": "rearm-test", "ok": True, "result": dict(session.session_result)}

    monkeypatch.setattr(manager, "_start_session", fake_start_session)

    result = await manager.rearm(context_id="ctx-linux")

    assert result["ok"] is True
    assert "approval required" in statuses
    assert statuses[-1] == "active"


async def test_macos_permission_setup_uses_fresh_helpers_and_advances_in_order(
    _temp_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(
        enabled=True,
        backend_selection=_selection(backend_id="macos", backend_family="macos"),
    )
    session = _HelperSession(context_id="launcher")
    calls: list[str] = []
    responses = iter(
        [
            {
                "ok": True,
                "result": {
                    "accessibility": "required",
                    "screen_recording": "required",
                },
            },
            {"ok": True, "result": {"accessibility": "required"}},
            {
                "ok": True,
                "result": {
                    "accessibility": "granted",
                    "screen_recording": "required",
                },
            },
            {
                "ok": True,
                "result": {
                    "screen_recording": "required",
                    "restart_required": False,
                },
            },
            {
                "ok": True,
                "result": {
                    "accessibility": "granted",
                    "screen_recording": "granted",
                },
            },
        ]
    )

    async def fresh(
        _session: _HelperSession,
        action: str,
        **_options: object,
    ) -> dict[str, object]:
        calls.append(action)
        return next(responses)

    monkeypatch.setattr(manager, "_fresh_permission_request", fresh)
    monkeypatch.setattr(computer_use_mod, "_MACOS_PERMISSION_POLL_INTERVAL_SECONDS", 0.0)

    result = await manager._prepare_macos_permissions(session, prompt=True, timeout=5.0)

    assert result["state"] == "ready"
    assert calls == [
        "permission_status",
        "request_accessibility",
        "permission_status",
        "request_screen_recording",
        "permission_status",
    ]
    assert manager.hello_metadata()["setup"]["state"] == "ready"


async def test_macos_permission_checks_close_the_helper_before_and_after_each_poll(
    _temp_env: Path,
) -> None:
    manager = _manager(
        enabled=True,
        backend_selection=_selection(backend_id="macos", backend_family="macos"),
    )
    session = _HelperSession(context_id="launcher")
    manager._close_helper_session = AsyncMock()  # type: ignore[method-assign]
    manager._helper_request = AsyncMock(  # type: ignore[method-assign]
        return_value={"ok": True, "result": {"state": "ready"}}
    )

    result = await manager._fresh_permission_request(session, "permission_status")

    assert result["ok"] is True
    assert manager._close_helper_session.await_count == 2
    request = manager._helper_request.await_args.args[1]
    assert request["action"] == "permission_status"
    assert request["request_timeout_seconds"] == 2.0


async def test_macos_start_session_resumes_original_request_after_permission_setup(
    _temp_env: Path,
) -> None:
    manager = _manager(
        enabled=True,
        trust_mode="persistent",
        backend_selection=_selection(backend_id="macos", backend_family="macos"),
    )
    manager._prepare_macos_permissions = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "state": "ready",
            "accessibility": "granted",
            "screen_recording": "granted",
        }
    )
    manager._helper_request = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "ok": True,
            "result": {
                "active": True,
                "status": "active",
                "session_id": "sess-mac",
                "width": 1280,
                "height": 720,
            },
        }
    )

    result = await manager.handle_op(
        {"op_id": "start-mac", "action": "start_session", "context_id": "ctx-mac"}
    )

    assert result["ok"] is True
    manager._prepare_macos_permissions.assert_awaited_once()
    request = manager._helper_request.await_args.args[1]
    assert request["allow_prompt"] is False
    assert request["request_timeout_seconds"] == 2.0


async def test_macos_rearm_runs_staged_setup_and_keeps_the_approved_session(
    _temp_env: Path,
) -> None:
    old_restore_token = "123e4567-e89b-12d3-a456-426614174000"
    new_restore_token = "123e4567-e89b-12d3-a456-426614174001"
    manager = _manager(
        enabled=True,
        trust_mode="allow",
        restore_token=old_restore_token,
        backend_selection=_selection(backend_id="macos", backend_family="macos"),
    )
    manager._prepare_macos_permissions = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "state": "ready",
            "accessibility": "granted",
            "screen_recording": "granted",
        }
    )
    manager._helper_request = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "ok": True,
            "result": {
                "active": True,
                "status": "active",
                "session_id": "sess-mac-rearm",
                "restore_token": new_restore_token,
                "width": 1280,
                "height": 720,
            },
        }
    )

    result = await manager.rearm("ctx-mac")

    assert result["ok"] is True
    manager._prepare_macos_permissions.assert_awaited_once()
    setup_call = manager._prepare_macos_permissions.await_args
    assert setup_call.kwargs == {
        "prompt": True,
        "timeout": computer_use_mod._MACOS_PERMISSION_SETUP_TIMEOUT_SECONDS,
    }
    request = manager._helper_request.await_args.args[1]
    assert request["action"] == "start_session"
    assert request["trust_mode"] == "persistent"
    assert request["allow_prompt"] is False
    assert request["restore_token"] == ""
    assert manager.trust_mode == "allow"
    assert manager.status_label == "active"
    assert manager.restore_token == new_restore_token
    assert manager._sessions["ctx-mac"].session_id == "sess-mac-rearm"


async def test_macos_setup_runs_a_harmless_start_capture_stop_probe(
    _temp_env: Path,
) -> None:
    manager = _manager(
        enabled=True,
        trust_mode="persistent",
        backend_selection=_selection(backend_id="macos", backend_family="macos"),
    )
    manager._prepare_macos_permissions = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "state": "ready",
            "accessibility": "granted",
            "screen_recording": "granted",
        }
    )
    manager._helper_request = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            {
                "ok": True,
                "result": {
                    "active": True,
                    "status": "active",
                    "session_id": "probe-session",
                    "width": 1280,
                    "height": 720,
                },
            },
            {
                "ok": True,
                "result": {
                    "session_id": "probe-session",
                    "width": 1280,
                    "height": 720,
                    "png_base64": "iVBORw0KGgo=",
                },
            },
        ]
    )
    stop_calls: list[str] = []

    async def stop_probe(op_id: str, _session: _HelperSession) -> dict[str, object]:
        stop_calls.append("stop_session")
        return {"op_id": op_id, "ok": True, "result": {"status": "stopped"}}

    manager._stop_session = stop_probe  # type: ignore[method-assign]

    result = await manager.setup_permissions("launcher", prompt=True)

    assert result["ok"] is True
    actions = [call.args[1]["action"] for call in manager._helper_request.await_args_list]
    assert actions + stop_calls == ["start_session", "capture", "stop_session"]
    assert manager.hello_metadata()["setup"]["state"] == "ready"


async def test_macos_agent_start_waits_for_inflight_launcher_setup(
    _temp_env: Path,
) -> None:
    manager = _manager(
        enabled=True,
        trust_mode="persistent",
        backend_selection=_selection(backend_id="macos", backend_family="macos"),
    )
    setup_started = asyncio.Event()
    release_setup = asyncio.Event()
    agent_started = asyncio.Event()

    async def setup_probe(*_args: object, **_kwargs: object) -> dict[str, object]:
        setup_started.set()
        await release_setup.wait()
        return {"ok": True, "result": {"state": "ready"}}

    async def agent_start(*_args: object) -> dict[str, object]:
        agent_started.set()
        return {"op_id": "agent-start", "ok": True, "result": {"active": True}}

    manager._setup_macos_permissions = setup_probe  # type: ignore[method-assign]
    manager._start_session_unlocked = agent_start  # type: ignore[method-assign]
    launcher_task = asyncio.create_task(manager.setup_permissions("launcher", prompt=True))
    await setup_started.wait()
    agent_task = asyncio.create_task(
        manager._start_session("agent-start", _HelperSession(context_id="agent"))
    )
    await asyncio.sleep(0)

    assert agent_started.is_set() is False
    release_setup.set()
    await launcher_task
    await agent_task
    assert agent_started.is_set() is True


async def test_macos_permission_setup_timeout_is_bounded_below_core_tool_timeout(
    _temp_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(
        enabled=True,
        backend_selection=_selection(backend_id="macos", backend_family="macos"),
    )
    manager._fresh_permission_request = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            {
                "ok": True,
                "result": {
                    "accessibility": "required",
                    "screen_recording": "required",
                },
            },
            {"ok": True, "result": {"accessibility": "required"}},
        ]
    )
    monkeypatch.setattr(computer_use_mod, "_MACOS_PERMISSION_POLL_INTERVAL_SECONDS", 0.0)

    result = await manager._prepare_macos_permissions(
        _HelperSession(context_id="launcher"),
        prompt=True,
        timeout=0.0,
    )

    assert computer_use_mod._MACOS_PERMISSION_SETUP_TIMEOUT_SECONDS == 120.0
    assert result["state"] == "error"
    assert result["code"] == "COMPUTER_USE_APPROVAL_TIMEOUT"
    assert "macOS permissions" in result["message"]


def test_allow_with_restore_token_starts_in_allow_status(
    _temp_env: Path,
) -> None:
    restore_token = "123e4567-e89b-12d3-a456-426614174000"
    manager = _manager(
        enabled=True,
        trust_mode="allow",
        restore_token=restore_token,
    )

    assert manager.status_label == "allow"
    assert manager.hello_metadata()["status"] == "allow"
    assert manager.hello_metadata()["restore_token_present"] is True


def test_manager_hello_metadata_includes_portable_contract(
    _temp_env: Path,
) -> None:
    manager = _manager(
        enabled=True,
        backend_selection=_selection(
            features=(
                "inline-png-capture",
                "native-window-list",
                "window-state",
                "element-index-targeting",
                "background-dispatch",
                "foreground-dispatch-fallback",
                "atspi-tree-snapshot",
                "atspi-structural-targeting",
                "atspi-element-action",
            ),
        ),
    )

    metadata = manager.hello_metadata()

    assert metadata["contract_version"] == COMPUTER_USE_CONTRACT_VERSION
    assert metadata["capabilities"]["identity"] == {
        "pid": True,
        "window_id": True,
        "element_index": True,
    }
    assert metadata["capabilities"]["dispatch"]["default"] == "background"
    assert metadata["capabilities"]["dispatch"]["background"] is True
    assert metadata["capabilities"]["dispatch"]["foreground_fallback"] is True
    assert metadata["capabilities"]["elements"]["tree_backends"] == ["at-spi"]


def test_reset_enabled_for_shutdown_disables_next_run_without_erasing_restore_token(
    _temp_env: Path,
) -> None:
    restore_token = "123e4567-e89b-12d3-a456-426614174000"
    manager = _manager(enabled=True, trust_mode="allow", restore_token=restore_token)

    manager.reset_enabled_for_shutdown()

    assert manager.enabled is False
    assert manager.config.computer_use_enabled is False
    assert manager.restore_token == restore_token
    assert manager.config.computer_use_restore_token == restore_token
    assert manager.status_label == "disabled"
    assert "AGENT_ZERO_COMPUTER_USE_ENABLED=0" in _temp_env.read_text(encoding="utf-8")


async def test_start_session_persists_restore_token_in_persistent_mode(
    _temp_env: Path,
) -> None:
    restore_token = "123e4567-e89b-12d3-a456-426614174000"
    manager = _manager(enabled=True, trust_mode="persistent")
    manager._helper_request = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "ok": True,
            "result": {
                "active": True,
                "status": "active",
                "session_id": "sess-1",
                "restore_token": restore_token,
                "width": 1280,
                "height": 720,
            },
        }
    )

    result = await manager.handle_op({"op_id": "start-2", "action": "start_session", "context_id": "ctx-1"})

    assert result["ok"] is True
    assert result["result"]["session_id"] == "sess-1"
    assert manager.restore_token == restore_token
    assert f"AGENT_ZERO_COMPUTER_USE_RESTORE_TOKEN={restore_token}" in _temp_env.read_text(encoding="utf-8")


async def test_start_session_reuses_stable_session_metadata_after_other_actions(
    _temp_env: Path,
) -> None:
    restore_token = "123e4567-e89b-12d3-a456-426614174000"
    manager = _manager(enabled=True, trust_mode="persistent")
    manager._helper_request = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            {
                "ok": True,
                "result": {
                    "active": True,
                    "status": "active",
                    "session_id": "sess-1",
                    "restore_token": restore_token,
                    "width": 1280,
                    "height": 720,
                },
            },
            {
                "ok": True,
                "result": {
                    "host_path": "/tmp/capture.png",
                    "container_path": "/a0/tmp/capture.png",
                    "width": 1280,
                    "height": 720,
                },
            },
        ]
    )

    first = await manager.handle_op({"op_id": "start-1", "action": "start_session", "context_id": "ctx-1"})
    capture = await manager.handle_op({"op_id": "cap-1", "action": "capture", "context_id": "ctx-1"})
    second = await manager.handle_op({"op_id": "start-2", "action": "start_session", "context_id": "ctx-1"})

    assert first["result"]["session_id"] == "sess-1"
    assert capture["ok"] is True
    assert second["ok"] is True
    assert second["result"]["session_id"] == "sess-1"
    assert second["result"]["width"] == 1280
    assert second["result"]["height"] == 720
    assert "host_path" not in second["result"]


def test_backend_selection_prefers_highest_priority_detected_spec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    low = _backend_spec(
        backend_id="low",
        priority=5,
        detected=True,
        support_reason="low priority",
    )
    high = _backend_spec(
        backend_id="high",
        priority=20,
        detected=True,
        support_reason="high priority",
    )
    monkeypatch.setattr(backend_mod, "available_backend_specs", lambda: [low, high])

    selection = backend_mod.resolve_backend_selection()

    assert selection.spec is high
    assert selection.supported is True
    assert selection.support_reason == "high priority"


def test_backend_selection_returns_support_reason_when_no_backend_detects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    undetected = _backend_spec(
        backend_id="wayland",
        detected=False,
        support_reason="XDG_SESSION_TYPE=tty is not supported by the Wayland portal backend.",
    )
    monkeypatch.setattr(backend_mod.sys, "platform", "linux")
    monkeypatch.setattr(backend_mod, "available_backend_specs", lambda: [undetected])

    selection = backend_mod.resolve_backend_selection()

    assert selection.spec is undetected
    assert selection.supported is False
    assert "Wayland portal backend" in selection.support_reason


def test_available_backend_specs_filters_remote_x11_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wayland = _backend_spec(backend_id="wayland", priority=100)
    x11 = _backend_spec(backend_id="x11", priority=90)
    monkeypatch.setattr(backend_mod, "_entry_point_specs", lambda: [wayland, x11])
    backend_mod.clear_backend_specs()

    specs = backend_mod.available_backend_specs()

    assert [spec.backend_id for spec in specs] == ["wayland"]


def test_backend_selection_prefers_current_platform_reason_when_no_backend_detects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    macos = _backend_spec(
        backend_id="macos",
        backend_family="macos",
        priority=100,
        detected=False,
        support_reason="macOS computer-use backend is only available on macOS.",
    )
    windows = _backend_spec(
        backend_id="windows",
        backend_family="windows",
        priority=100,
        detected=False,
        support_reason="Windows desktop backend is only available on Windows.",
    )
    wayland = _backend_spec(
        backend_id="wayland",
        backend_family="linux",
        priority=100,
        detected=False,
        support_reason="Wayland portal backend is unavailable.",
    )
    monkeypatch.setattr(backend_mod.sys, "platform", "linux")
    monkeypatch.setattr(backend_mod, "available_backend_specs", lambda: [macos, windows, wayland])

    selection = backend_mod.resolve_backend_selection()

    assert selection.spec is wayland
    assert selection.supported is False
    assert selection.support_reason == "Wayland portal backend is unavailable."


def test_backend_selection_prefers_active_linux_display_stack_when_unsupported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wayland = _backend_spec(
        backend_id="wayland",
        backend_family="linux",
        priority=100,
        detected=False,
        support_reason="XDG_SESSION_TYPE=x11 is not supported by the Wayland portal backend.",
    )
    x11 = _backend_spec(
        backend_id="x11",
        backend_family="linux",
        priority=90,
        detected=False,
        support_reason="python-xlib is not importable.",
    )
    monkeypatch.setattr(backend_mod.sys, "platform", "linux")
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(backend_mod, "available_backend_specs", lambda: [wayland, x11])

    selection = backend_mod.resolve_backend_selection()

    assert selection.spec is wayland
    assert selection.supported is False
    assert selection.support_reason == "XDG_SESSION_TYPE=x11 is not supported by the Wayland portal backend."


async def test_hello_metadata_includes_backend_fields(
    _temp_env: Path,
) -> None:
    selection = _selection(
        backend_id="wayland",
        backend_family="linux",
        priority=100,
        detected=True,
        features=("portal-remote-desktop", "inline-png-capture"),
        support_reason="Wayland portal backend is available.",
    )
    manager = _manager(enabled=True, backend_selection=selection)

    metadata = manager.hello_metadata()

    assert metadata["backend_id"] == "wayland"
    assert metadata["backend_family"] == "linux"
    assert metadata["features"] == ["portal-remote-desktop", "inline-png-capture"]
    assert metadata["support_reason"] == "Wayland portal backend is available."
    assert metadata["supported"] is True
    assert metadata["enabled"] is True


async def test_status_snapshot_includes_backend_fields(
    _temp_env: Path,
) -> None:
    selection = _selection(
        backend_id="wayland",
        backend_family="linux",
        detected=True,
        support_reason="Wayland portal backend is available.",
    )
    manager = _manager(enabled=True, backend_selection=selection)

    status = await manager.handle_op(
        {"op_id": "status-1", "action": "status", "context_id": "ctx-1"}
    )

    assert status["ok"] is True
    assert status["result"]["backend_id"] == "wayland"
    assert status["result"]["backend_family"] == "linux"
    assert status["result"]["features"] == ["inline-png-capture"]
    assert status["result"]["support_reason"] == "Wayland portal backend is available."


async def test_capture_strips_inline_png_base64_response_when_artifact_path_is_advertised(
    _temp_env: Path,
) -> None:
    manager = _manager(enabled=True)
    payload = b"inline-capture-bytes"
    manager._helper_request = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "ok": True,
            "result": {
                "png_base64": base64.b64encode(payload).decode("ascii"),
                "width": 1280,
                "height": 720,
                "session_id": "sess-1",
            },
        }
    )
    session = _HelperSession(context_id="ctx-1", session_id="sess-1", active=True)
    session.process = type("FakeProcess", (), {"returncode": None})()
    manager._sessions["ctx-1"] = session

    result = await manager.handle_op(
        {"op_id": "cap-1", "action": "capture", "context_id": "ctx-1", "session_id": "sess-1"}
    )

    assert result["ok"] is True
    assert "png_base64" not in result["result"]
    assert result["result"]["artifact"]["encoding"] == "base64"
    assert result["result"]["artifact"]["mime"] == "image/png"
    assert base64.b64decode(result["result"]["artifact"]["data"]) == payload
    assert result["result"]["ephemeral"] is True
    assert "host_path" not in result["result"]
    assert "capture_path" not in result["result"]
    assert "container_path" not in result["result"]
    assert not computer_use_mod.HOST_ARTIFACT_ROOT.exists()


async def test_capture_embeds_legacy_path_result_without_advertising_path(
    _temp_env: Path,
    tmp_path: Path,
) -> None:
    manager = _manager(enabled=True)
    payload = b"legacy-path-bytes"
    capture_path = tmp_path / "legacy.png"
    capture_path.write_bytes(payload)
    manager._helper_request = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "ok": True,
            "result": {
                "host_path": str(capture_path),
                "width": 640,
                "height": 480,
                "session_id": "sess-1",
            },
        }
    )
    session = _HelperSession(context_id="ctx-1", session_id="sess-1", active=True)
    session.process = type("FakeProcess", (), {"returncode": None})()
    manager._sessions["ctx-1"] = session

    result = await manager.handle_op(
        {"op_id": "cap-1", "action": "capture", "context_id": "ctx-1", "session_id": "sess-1"}
    )

    assert result["ok"] is True
    assert "png_base64" not in result["result"]
    assert result["result"]["artifact"]["encoding"] == "base64"
    assert base64.b64decode(result["result"]["artifact"]["data"]) == payload
    assert "host_path" not in result["result"]
    assert "capture_path" not in result["result"]
    assert "container_path" not in result["result"]


async def test_capture_includes_base64_artifact_from_written_capture_path(
    _temp_env: Path,
) -> None:
    manager = _manager(enabled=True)
    payload = b"written-capture-bytes"

    async def helper_request(_session: _HelperSession, request: dict[str, object]) -> dict[str, object]:
        capture_path = Path(str(request.get("capture_path") or ""))
        capture_path.parent.mkdir(parents=True, exist_ok=True)
        capture_path.write_bytes(payload)
        return {
            "ok": True,
            "result": {
                "capture_path": str(capture_path),
                "width": 640,
                "height": 480,
                "session_id": "sess-1",
            },
        }

    manager._helper_request = helper_request  # type: ignore[method-assign]
    session = _HelperSession(context_id="ctx-1", session_id="sess-1", active=True)
    session.process = type("FakeProcess", (), {"returncode": None})()
    manager._sessions["ctx-1"] = session

    result = await manager.handle_op(
        {"op_id": "cap-1", "action": "capture", "context_id": "ctx-1", "session_id": "sess-1"}
    )

    assert result["ok"] is True
    assert result["result"]["artifact"]["filename"].endswith(".png")
    assert result["result"]["artifact"]["mime"] == "image/png"
    assert result["result"]["artifact"]["encoding"] == "base64"
    assert base64.b64decode(result["result"]["artifact"]["data"]) == payload
    assert result["result"]["ephemeral"] is True
    assert "capture_path" not in result["result"]
    assert "host_path" not in result["result"]
    assert "container_path" not in result["result"]
    assert not computer_use_mod.HOST_ARTIFACT_ROOT.exists()


async def test_capture_requests_shared_artifact_path_and_adds_container_path(
    _temp_env: Path,
) -> None:
    manager = _manager(enabled=True)

    async def helper_request(_session: _HelperSession, request: dict[str, object]) -> dict[str, object]:
        capture_path = str(request.get("capture_path") or "")
        Path(capture_path).parent.mkdir(parents=True, exist_ok=True)
        Path(capture_path).write_bytes(b"shared-artifact-bytes")
        return {
            "ok": True,
            "result": {
                "capture_path": capture_path,
                "width": 640,
                "height": 480,
                "session_id": "sess-1",
            },
        }

    manager._helper_request = helper_request  # type: ignore[method-assign]
    session = _HelperSession(context_id="ctx-1", session_id="sess-1", active=True)
    session.process = type("FakeProcess", (), {"returncode": None})()
    manager._sessions["ctx-1"] = session

    result = await manager.handle_op(
        {"op_id": "cap-2", "action": "capture", "context_id": "ctx-1", "session_id": "sess-1"}
    )

    assert result["ok"] is True
    assert "png_base64" not in result["result"]
    assert result["result"]["ephemeral"] is True
    assert "capture_path" not in result["result"]
    assert "host_path" not in result["result"]
    assert "container_path" not in result["result"]
    assert result["result"]["capture_id"]
    assert result["result"]["coordinate_space"] == "normalized_global_screen"
    assert result["result"]["coordinate_origin"] == "top_left"
    assert result["result"]["coordinate_range"] == [0.0, 1.0]
    assert not computer_use_mod.HOST_ARTIFACT_ROOT.exists()


async def test_capture_artifacts_are_deleted_after_embedding_and_disconnect_is_idempotent(
    _temp_env: Path,
) -> None:
    manager = _manager(enabled=True)
    captured_paths: list[Path] = []

    async def helper_request(_session: _HelperSession, request: dict[str, object]) -> dict[str, object]:
        capture_path = Path(str(request.get("capture_path") or ""))
        capture_path.parent.mkdir(parents=True, exist_ok=True)
        capture_path.write_bytes(f"capture-{len(captured_paths)}".encode("utf-8"))
        captured_paths.append(capture_path)
        return {
            "ok": True,
            "result": {
                "capture_path": str(capture_path),
                "width": 640,
                "height": 480,
                "session_id": "sess-1",
            },
        }

    manager._helper_request = helper_request  # type: ignore[method-assign]
    session = _HelperSession(context_id="ctx-1", session_id="sess-1", active=True)
    session.process = type("FakeProcess", (), {"returncode": 0})()
    manager._sessions["ctx-1"] = session

    first = await manager.handle_op(
        {"op_id": "cap-1", "action": "capture", "context_id": "ctx-1", "session_id": "sess-1"}
    )
    second = await manager.handle_op(
        {"op_id": "cap-2", "action": "capture", "context_id": "ctx-1", "session_id": "sess-1"}
    )

    assert first["result"]["ephemeral"] is True
    assert second["result"]["ephemeral"] is True
    assert captured_paths[0] != captured_paths[1]
    assert all(not path.exists() for path in captured_paths)
    assert not computer_use_mod.HOST_ARTIFACT_ROOT.exists()

    await manager.disconnect()

    assert not computer_use_mod.HOST_ARTIFACT_ROOT.exists()


async def test_disconnect_allows_missing_host_artifact_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(computer_use_mod, "HOST_ARTIFACT_ROOT", None)
    manager = _manager(enabled=True)

    await manager.disconnect()

    assert manager.status == "persistent"


async def test_status_snapshot_allows_missing_host_artifact_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(computer_use_mod, "HOST_ARTIFACT_ROOT", None)
    manager = _manager(enabled=True)

    result = await manager.handle_op({"op_id": "status-1", "action": "status"})

    assert result["ok"] is True
    assert result["result"]["host_artifact_root"] is None


async def test_failed_capture_removes_stale_artifacts(
    _temp_env: Path,
) -> None:
    stale_path = computer_use_mod.HOST_ARTIFACT_ROOT / "ctx-1" / "stale.png"
    stale_path.parent.mkdir(parents=True, exist_ok=True)
    stale_path.write_bytes(b"stale")

    manager = _manager(enabled=True)
    manager._helper_request = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "ok": False,
            "code": "COMPUTER_USE_ERROR",
            "error": "capture failed",
        }
    )
    session = _HelperSession(context_id="ctx-1", session_id="sess-1", active=True)
    session.process = type("FakeProcess", (), {"returncode": 0})()
    manager._sessions["ctx-1"] = session

    result = await manager.handle_op(
        {"op_id": "cap-1", "action": "capture", "context_id": "ctx-1", "session_id": "sess-1"}
    )

    assert result["ok"] is False
    assert not stale_path.exists()
    assert not computer_use_mod.HOST_ARTIFACT_ROOT.exists()


async def test_capture_failure_preserves_active_session_for_retry(
    _temp_env: Path,
) -> None:
    manager = _manager(enabled=True)
    manager._helper_request = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            {
                "ok": False,
                "code": "COMPUTER_USE_ERROR",
                "error": "capture failed",
            },
            {
                "ok": True,
                "result": {
                    "width": 640,
                    "height": 480,
                    "session_id": "sess-1",
                    "capture_path": str(computer_use_mod.HOST_ARTIFACT_ROOT / "ctx-1" / "capture.png"),
                },
            },
        ]
    )
    session = _HelperSession(context_id="ctx-1", session_id="sess-1", active=True, status="active")
    session.process = type("FakeProcess", (), {"returncode": None})()
    session.session_result = {
        "session_id": "sess-1",
        "status": "active",
        "width": 1280,
        "height": 720,
    }
    manager._sessions["ctx-1"] = session

    first = await manager.handle_op(
        {"op_id": "cap-fail", "action": "capture", "context_id": "ctx-1", "session_id": "sess-1"}
    )

    assert first["ok"] is False
    assert first["error"] == "capture failed"
    assert manager._sessions["ctx-1"].active is True
    assert manager._sessions["ctx-1"].session_id == "sess-1"
    assert manager.status_label == "active"
    assert manager.status_detail == "capture failed"

    second = await manager.handle_op(
        {"op_id": "cap-retry", "action": "capture", "context_id": "ctx-1", "session_id": "sess-1"}
    )

    assert second["ok"] is True
    assert second["result"]["session_id"] == "sess-1"


async def test_helper_request_ignores_protocol_noise_until_matching_response(
    _temp_env: Path,
) -> None:
    manager = _manager(enabled=True)
    session = _HelperSession(context_id="ctx-1")
    session.process = _FakeHelperProcess(
        [
            "Right button pressed at (960, 540)\n",
            json.dumps({"request_id": "stale-1", "ok": True, "result": {"ignored": True}}) + "\n",
            json.dumps({"request_id": "req-1", "ok": True, "result": {"status": "active"}}) + "\n",
        ]
    )
    manager._ensure_helper = AsyncMock(return_value=session)  # type: ignore[method-assign]

    result = await manager._helper_request(
        session,
        {
            "request_id": "req-1",
            "action": "click",
            "context_id": "ctx-1",
            "session_id": "sess-1",
        },
    )

    assert result["ok"] is True
    assert result["result"]["status"] == "active"
    assert json.loads(session.process.stdin.buffer.decode("utf-8"))["request_id"] == "req-1"


async def test_helper_request_times_out_stuck_permission_prompt(
    _temp_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(enabled=True)
    session = _HelperSession(context_id="ctx-1")

    class FakeProcess:
        def __init__(self) -> None:
            self.stdin = _FakeHelperStdin()
            self.stdout = asyncio.StreamReader()
            self.stderr = _FakeStream()
            self.returncode = None
            self.pid = 123

        def terminate(self) -> None:
            self.returncode = -15
            self.stdout.feed_eof()

        def kill(self) -> None:
            self.returncode = -9
            self.stdout.feed_eof()

        async def wait(self) -> int | None:
            return self.returncode

    session.process = FakeProcess()
    manager._ensure_helper = AsyncMock(return_value=session)  # type: ignore[method-assign]
    monkeypatch.setattr(computer_use_mod, "_helper_response_timeout_seconds", lambda _payload: 0.01)

    result = await manager._helper_request(
        session,
        {
            "request_id": "req-timeout",
            "action": "start_session",
            "context_id": "ctx-1",
            "allow_prompt": True,
            "request_timeout_seconds": 180.0,
        },
    )

    assert result["ok"] is False
    assert result["code"] == "COMPUTER_USE_REARM_REQUIRED"
    assert "Timed out while waiting for the platform permission prompt" in result["error"]
    assert session.process is None


async def test_helper_request_times_out_when_helper_stops_reading_stdin(
    _temp_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(enabled=True)
    session = _HelperSession(context_id="ctx-1")

    class FakeProcess:
        def __init__(self) -> None:
            self.stdin = _StuckHelperStdin()
            self.stdout = asyncio.StreamReader()
            self.stderr = _FakeStream()
            self.returncode = None
            self.pid = 123

        def terminate(self) -> None:
            self.returncode = -15
            self.stdout.feed_eof()

        def kill(self) -> None:
            self.returncode = -9
            self.stdout.feed_eof()

        async def wait(self) -> int | None:
            return self.returncode

    session.process = FakeProcess()
    manager._ensure_helper = AsyncMock(return_value=session)  # type: ignore[method-assign]
    monkeypatch.setattr(computer_use_mod, "_helper_response_timeout_seconds", lambda _payload: 0.01)
    monkeypatch.setattr(computer_use_mod, "_HELPER_CLOSE_DRAIN_TIMEOUT_SECONDS", 0.01)

    result = await manager._helper_request(
        session,
        {
            "request_id": "req-timeout",
            "action": "start_session",
            "context_id": "ctx-1",
            "allow_prompt": True,
            "request_timeout_seconds": 180.0,
        },
    )

    assert result["ok"] is False
    assert result["code"] == "COMPUTER_USE_REARM_REQUIRED"
    assert session.process is None


async def test_ensure_helper_uses_expanded_stdio_limit_for_large_capture_payloads(
    _temp_env: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    package_src = tmp_path / "package" / "src"
    helper_dir = package_src / "a0_computer_use_wayland"
    helper_dir.mkdir(parents=True)
    (helper_dir / "__init__.py").write_text("", encoding="utf-8")
    helper_script = helper_dir / "computer_use_helper.py"
    manager = _manager(
        enabled=True,
        backend_selection=_selection(helper_target=str(helper_script)),
    )
    session = _HelperSession(context_id="ctx-1")
    calls: list[dict[str, object]] = []

    class FakeProcess:
        def __init__(self) -> None:
            self.stdin = _FakeHelperStdin()
            self.stdout = _FakeStream()
            self.stderr = _FakeStream()
            self.returncode = None

    async def fake_create_subprocess_exec(*args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
        return FakeProcess()

    def fake_create_task(coro):
        coro.close()
        return AsyncMock()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(asyncio, "create_task", fake_create_task)

    await manager._ensure_helper(session)

    assert session.process is not None
    assert calls
    assert calls[0]["kwargs"]["limit"] == _HELPER_STDIO_LIMIT
    helper_env = calls[0]["kwargs"]["env"]
    assert isinstance(helper_env, dict)
    pythonpath = str(helper_env["PYTHONPATH"]).split(computer_use_mod.os.pathsep)
    assert str(Path(computer_use_mod.__file__).resolve().parents[1]) in pythonpath
    assert str(package_src) in pythonpath


async def test_helper_stderr_is_forwarded_when_debug_is_enabled(
    _temp_env: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("A0_COMPUTER_USE_DEBUG", "1")
    manager = _manager(enabled=True)

    process = type(
        "FakeProcess",
        (),
        {
            "stderr": _FakeLineStream(["waiting for permissions\n"]),
            "pid": 4242,
        },
    )()

    await manager._drain_stderr(process)

    stderr = capsys.readouterr().err
    assert "helper.stderr" in stderr
    assert "waiting for permissions" in stderr
    assert "4242" in stderr


async def test_move_click_scroll_key_type_normalize_payloads(
    _temp_env: Path,
) -> None:
    manager = _manager(enabled=True)

    move = manager._normalize_action_payload("move", {"x": 0.25, "y": 0.75}, context_id="ctx-1")
    click = manager._normalize_action_payload(
        "click",
        {"x": 0.4, "y": 0.6, "button": "right", "count": 2},
        context_id="ctx-1",
    )
    scroll = manager._normalize_action_payload("scroll", {"dx": 1, "dy": -2}, context_id="ctx-1")
    key = manager._normalize_action_payload("key", {"key": "ctrl+alt+t"}, context_id="ctx-1")
    typed = manager._normalize_action_payload(
        "type",
        {"text": "hello", "window_id": "atspi-pid:123:path:0.0"},
        context_id="ctx-1",
    )
    capture = manager._normalize_action_payload(
        "capture",
        {"fresh": True, "fresh_after": 123.5, "fresh_timeout_seconds": 0.25},
        context_id="ctx-1",
    )
    ax_snapshot = manager._normalize_action_payload(
        "ax_snapshot",
        {
            "pid": "123",
            "window_id": "atspi-pid:123:path:0.0",
            "max_depth": 3,
            "max_nodes": 50,
        },
        context_id="ctx-1",
    )
    ax_action = manager._normalize_action_payload(
        "ax_action",
        {"target": {"role": "AXButton", "title": "Save"}, "operation": "press"},
        context_id="ctx-1",
    )
    uia_snapshot = manager._normalize_action_payload(
        "uia_snapshot",
        {"max_depth": 4, "max_nodes": 80},
        context_id="ctx-1",
    )
    uia_action = manager._normalize_action_payload(
        "uia_action",
        {
            "target": {"role": "Button", "title": "Save"},
            "selector": "role:Button && name:Save",
            "operation": "invoke",
        },
        context_id="ctx-1",
    )
    windows = manager._normalize_action_payload(
        "list_windows",
        {"include_hidden": True, "max_windows": 12},
        context_id="ctx-1",
    )
    window_state = manager._normalize_action_payload(
        "get_window_state",
        {"pid": "1234", "window_id": "uia-hwnd:5678", "max_depth": 2, "max_nodes": 25},
        context_id="ctx-1",
    )
    element_action = manager._normalize_action_payload(
        "element_action",
        {
            "window_id": "uia-hwnd:5678",
            "element_index": "3",
            "operation": "invoke",
            "dispatch": "background",
        },
        context_id="ctx-1",
    )
    window_focus = manager._normalize_action_payload(
        "element_action",
        {
            "window_id": "atspi-pid:57929:path:20.0",
            "operation": "focus",
            "dispatch": "foreground",
        },
        context_id="ctx-1",
    )
    submitted = manager._normalize_action_payload(
        "type",
        {"text": "hello", "window_id": "atspi-pid:123:path:0.0", "submit": True},
        context_id="ctx-1",
    )

    assert move["x"] == 0.25 and move["y"] == 0.75
    assert click["button"] == "right" and click["count"] == 2
    assert scroll["dx"] == 1 and scroll["dy"] == -2
    assert key["keys"] == ["ctrl", "alt", "t"]
    assert typed["text"] == "hello"
    assert typed["window_id"] == "atspi-pid:123:path:0.0"
    assert capture["fresh"] is True
    assert capture["fresh_after"] == 123.5
    assert capture["fresh_timeout_seconds"] == 0.25
    assert ax_snapshot["max_depth"] == 3
    assert ax_snapshot["max_nodes"] == 50
    assert ax_snapshot["pid"] == 123
    assert ax_snapshot["window_id"] == "atspi-pid:123:path:0.0"
    assert ax_action["target"]["title"] == "Save"
    assert ax_action["operation"] == "press"
    assert uia_snapshot["max_depth"] == 4
    assert uia_snapshot["max_nodes"] == 80
    assert uia_action["target"]["title"] == "Save"
    assert uia_action["target"]["selector"] == "role:Button && name:Save"
    assert uia_action["operation"] == "invoke"
    assert windows["include_hidden"] is True
    assert windows["max_windows"] == 12
    assert window_state["pid"] == 1234
    assert window_state["window_id"] == "uia-hwnd:5678"
    assert window_state["max_depth"] == 2
    assert window_state["max_nodes"] == 25
    assert element_action["window_id"] == "uia-hwnd:5678"
    assert element_action["element_index"] == 3
    assert element_action["operation"] == "invoke"
    assert element_action["dispatch"] == "background"
    assert window_focus["window_id"] == "atspi-pid:57929:path:20.0"
    assert window_focus["operation"] == "focus"
    assert window_focus["dispatch"] == "foreground"
    assert submitted["submit"] is True
    assert submitted["window_id"] == "atspi-pid:123:path:0.0"


async def test_normalized_coordinates_are_clamped_to_unit_interval(
    _temp_env: Path,
) -> None:
    manager = _manager(enabled=True)

    move = manager._normalize_action_payload("move", {"x": -5, "y": 99}, context_id="ctx-1")

    assert move["x"] == 0.0
    assert move["y"] == 1.0


async def test_fresh_capture_uses_recent_action_completion_time(
    _temp_env: Path,
) -> None:
    manager = _manager(enabled=True, trust_mode="persistent")
    session = _HelperSession(context_id="ctx-1", session_id="sess-1", active=True)
    manager._sessions["ctx-1"] = session
    requests: list[dict[str, object]] = []

    async def helper_request(_session: _HelperSession, request: dict[str, object]) -> dict[str, object]:
        requests.append(dict(request))
        if request["action"] == "click":
            return {
                "ok": True,
                "result": {
                    "session_id": "sess-1",
                    "button": "left",
                    "count": 1,
                },
            }
        capture_path = Path(str(request.get("capture_path") or ""))
        capture_path.parent.mkdir(parents=True, exist_ok=True)
        capture_path.write_bytes(b"fresh-capture")
        return {
            "ok": True,
            "result": {
                "session_id": "sess-1",
                "capture_path": str(capture_path),
                "frame_captured_at": time.time(),
                "width": 1,
                "height": 1,
            },
        }

    manager._helper_request = helper_request  # type: ignore[method-assign]

    clicked = await manager.handle_op(
        {
            "op_id": "click-1",
            "action": "click",
            "context_id": "ctx-1",
            "session_id": "sess-1",
        }
    )
    captured = await manager.handle_op(
        {
            "op_id": "cap-1",
            "action": "capture",
            "context_id": "ctx-1",
            "session_id": "sess-1",
            "fresh": True,
        }
    )

    assert clicked["ok"] is True
    assert captured["ok"] is True
    assert requests[-1]["action"] == "capture"
    assert requests[-1]["fresh"] is True
    assert requests[-1]["fresh_after"] == session.last_action_completed_at
    assert requests[-1]["fresh_timeout_seconds"] == computer_use_mod._DEFAULT_FRESH_CAPTURE_TIMEOUT_SECONDS
    assert captured["result"]["fresh"] is True
    assert captured["result"]["fresh_after"] == session.last_action_completed_at
    assert captured["result"]["fresh_after_satisfied"] is True
    assert captured["result"]["ephemeral"] is True
    assert captured["result"]["capture_id"]
    assert "host_path" not in captured["result"]
    assert "capture_path" not in captured["result"]
    assert not computer_use_mod.HOST_ARTIFACT_ROOT.exists()


async def test_disconnect_closes_active_sessions_and_resets_status(
    _temp_env: Path,
) -> None:
    manager = _manager(enabled=True, trust_mode="persistent")
    session = _HelperSession(context_id="ctx-1", session_id="sess-1", active=True)
    manager._sessions["ctx-1"] = session
    manager._close_helper_session = AsyncMock()  # type: ignore[method-assign]

    await manager.disconnect()

    manager._close_helper_session.assert_awaited_once_with(session)
    assert manager.status_label == "persistent"


async def test_persistent_mode_discards_invalid_restore_token_before_helper_request(
    _temp_env: Path,
) -> None:
    restore_token = "123e4567-e89b-12d3-a456-426614174000"
    manager = _manager(enabled=True, trust_mode="persistent", restore_token="restore-123")
    manager._helper_request = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "ok": True,
            "result": {
                "active": True,
                "status": "active",
                "session_id": "sess-1",
                "restore_token": restore_token,
                "width": 1280,
                "height": 720,
            },
        }
    )

    result = await manager.handle_op({"op_id": "start-1", "action": "start_session", "context_id": "ctx-1"})

    assert result["ok"] is True
    manager._helper_request.assert_awaited_once()
    request = manager._helper_request.await_args.args[1]
    assert request["restore_token"] == ""
    assert manager.restore_token == restore_token


async def test_allow_with_invalid_restore_token_returns_rearm_required(
    _temp_env: Path,
) -> None:
    manager = _manager(enabled=True, trust_mode="allow", restore_token="restore-123")
    manager._helper_request = AsyncMock()  # type: ignore[method-assign]

    result = await manager.handle_op({"op_id": "start-1", "action": "start_session", "context_id": "ctx-1"})

    assert result["ok"] is False
    assert result["code"] == "COMPUTER_USE_REARM_REQUIRED"
    assert "desktop-control backend is not armed" in result["error"]
    manager._helper_request.assert_not_awaited()
    assert manager.restore_token == ""


async def test_allow_silent_restore_rearm_preserves_helper_message(
    _temp_env: Path,
) -> None:
    manager = _manager(
        enabled=True,
        trust_mode="allow",
        restore_token="123e4567-e89b-12d3-a456-426614174000",
    )
    manager._helper_request = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "ok": False,
            "code": "COMPUTER_USE_REARM_REQUIRED",
            "error": (
                "Silent restore was not available. Run /computer-use on and approve "
                "the platform permission prompt."
            ),
        }
    )

    result = await manager.handle_op({"op_id": "start-1", "action": "start_session", "context_id": "ctx-1"})

    assert result["ok"] is False
    assert result["code"] == "COMPUTER_USE_REARM_REQUIRED"
    assert result["error"] == (
        "Silent restore was not available. Run /computer-use on and approve "
        "the platform permission prompt."
    )
    assert result["result"]["status"] == "rearm required"
    assert result["result"]["last_error"] == result["error"]
    assert result["result"]["restore_token_present"] is True
    assert manager.status_label == "rearm required"
    assert manager.status_detail == result["error"]


async def test_ensure_armed_validates_saved_allow_token(
    _temp_env: Path,
) -> None:
    restore_token = "123e4567-e89b-12d3-a456-426614174000"
    manager = _manager(
        enabled=True,
        trust_mode="allow",
        restore_token=restore_token,
    )
    status_updates: list[str] = []
    manager.set_status_callback(lambda label, _detail: status_updates.append(label))
    status_updates.clear()
    manager._helper_request = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "ok": True,
            "result": {
                "active": True,
                "status": "active",
                "session_id": "sess-1",
                "width": 1280,
                "height": 720,
            },
        }
    )

    result = await manager.ensure_armed("ctx-1")

    assert result["ok"] is True
    assert manager.status_label == "active"
    assert "arming" in status_updates
    assert "approval required" not in status_updates
    request = manager._helper_request.await_args.args[1]
    assert request["trust_mode"] == "allow"
    assert request["allow_prompt"] is False
    assert request["restore_token"] == restore_token
    assert request["context_id"] == "ctx-1"


async def test_ensure_armed_marks_stale_allow_token_rearm_required(
    _temp_env: Path,
) -> None:
    manager = _manager(
        enabled=True,
        trust_mode="allow",
        restore_token="123e4567-e89b-12d3-a456-426614174000",
    )
    manager._helper_request = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "ok": False,
            "code": "COMPUTER_USE_REARM_REQUIRED",
            "error": "Silent restore was not available.",
        }
    )

    result = await manager.ensure_armed("ctx-1")

    assert result["ok"] is False
    assert result["code"] == "COMPUTER_USE_REARM_REQUIRED"
    assert manager.status_label == "rearm required"
    assert manager.status_detail == "Silent restore was not available."
    assert manager.hello_metadata()["status"] == "rearm required"


async def test_ensure_armed_maps_approval_required_to_rearm_required(
    _temp_env: Path,
) -> None:
    manager = _manager(
        enabled=True,
        trust_mode="allow",
        restore_token="123e4567-e89b-12d3-a456-426614174000",
    )
    manager._helper_request = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "ok": False,
            "code": "COMPUTER_USE_APPROVAL_REQUIRED",
            "error": "macOS Accessibility permission is required.",
        }
    )

    result = await manager.ensure_armed("ctx-1")

    assert result["ok"] is False
    assert result["code"] == "COMPUTER_USE_REARM_REQUIRED"
    assert result["error"] == "macOS Accessibility permission is required."
    assert manager.status_label == "rearm required"
    assert manager.status_detail == "macOS Accessibility permission is required."
    assert manager.hello_metadata()["status"] == "rearm required"


async def test_rearm_forces_prompt_then_restores_allow_mode(
    _temp_env: Path,
) -> None:
    old_restore_token = "123e4567-e89b-12d3-a456-426614174000"
    new_restore_token = "123e4567-e89b-12d3-a456-426614174001"
    manager = _manager(
        enabled=True,
        trust_mode="allow",
        restore_token=old_restore_token,
    )
    status_updates: list[str] = []
    manager.set_status_callback(lambda label, _detail: status_updates.append(label))
    status_updates.clear()
    manager._helper_request = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "ok": True,
            "result": {
                "active": True,
                "status": "active",
                "session_id": "sess-1",
                "restore_token": new_restore_token,
                "width": 1280,
                "height": 720,
            },
        }
    )

    result = await manager.rearm("ctx-1")

    assert result["ok"] is True
    assert "approval required" in status_updates
    assert "persistent" not in status_updates
    assert manager.trust_mode == "allow"
    assert manager.status_label == "active"
    assert manager.restore_token == new_restore_token
    request = manager._helper_request.await_args.args[1]
    assert request["trust_mode"] == "persistent"
    assert request["allow_prompt"] is True
    assert request["restore_token"] == ""
    assert request["context_id"] == "ctx-1"


async def test_rearm_closes_stale_active_session_before_prompting(
    _temp_env: Path,
) -> None:
    old_restore_token = "123e4567-e89b-12d3-a456-426614174000"
    new_restore_token = "123e4567-e89b-12d3-a456-426614174001"
    manager = _manager(
        enabled=True,
        trust_mode="allow",
        restore_token=old_restore_token,
    )
    manager._sessions["ctx-1"] = _HelperSession(
        context_id="ctx-1",
        session_id="stale-session",
        active=True,
    )
    manager._helper_request = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "ok": True,
            "result": {
                "active": True,
                "status": "active",
                "session_id": "fresh-session",
                "restore_token": new_restore_token,
                "width": 1280,
                "height": 720,
            },
        }
    )

    result = await manager.rearm("ctx-1")

    assert result["ok"] is True
    manager._helper_request.assert_awaited_once()
    request = manager._helper_request.await_args.args[1]
    assert request["trust_mode"] == "persistent"
    assert request["allow_prompt"] is True
    assert request["restore_token"] == ""
    assert manager._sessions["ctx-1"].session_id == "fresh-session"
    assert manager.restore_token == new_restore_token
    assert manager.status_label == "active"


async def test_rearm_failure_preserves_previous_allow_token_but_marks_rearm_required(
    _temp_env: Path,
) -> None:
    restore_token = "123e4567-e89b-12d3-a456-426614174000"
    manager = _manager(
        enabled=True,
        trust_mode="allow",
        restore_token=restore_token,
    )
    manager._helper_request = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "ok": False,
            "code": "COMPUTER_USE_REARM_REQUIRED",
            "error": "User approval is required.",
        }
    )

    result = await manager.rearm("ctx-1")

    assert result["ok"] is False
    assert result["code"] == "COMPUTER_USE_REARM_REQUIRED"
    assert manager.trust_mode == "allow"
    assert manager.restore_token == restore_token
    assert manager.status_label == "rearm required"
    assert manager.status_detail == "User approval is required."


async def test_rearm_approval_required_restores_allow_mode_and_marks_rearm_required(
    _temp_env: Path,
) -> None:
    restore_token = "123e4567-e89b-12d3-a456-426614174000"
    manager = _manager(
        enabled=True,
        trust_mode="allow",
        restore_token=restore_token,
    )
    manager._helper_request = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "ok": False,
            "code": "COMPUTER_USE_APPROVAL_REQUIRED",
            "error": "macOS Accessibility permission is required.",
        }
    )

    result = await manager.rearm("ctx-1")

    assert result["ok"] is False
    assert result["code"] == "COMPUTER_USE_REARM_REQUIRED"
    assert manager.trust_mode == "allow"
    assert manager.restore_token == restore_token
    assert manager.status_label == "rearm required"
    assert manager.status_detail == "macOS Accessibility permission is required."


async def test_stop_session_normalizes_success_and_closes_helper(
    _temp_env: Path,
) -> None:
    manager = _manager(enabled=True, trust_mode="persistent")
    session = _HelperSession(context_id="ctx-1", session_id="sess-1", active=True)
    session.process = type("FakeProcess", (), {"returncode": None})()
    manager._sessions["ctx-1"] = session
    manager._helper_request = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "ok": True,
            "result": {
                "active": False,
                "status": "stopped",
                "session_id": "",
            },
        }
    )
    manager._close_helper_session = AsyncMock()  # type: ignore[method-assign]

    result = await manager.handle_op(
        {"op_id": "stop-1", "action": "stop_session", "context_id": "ctx-1"}
    )

    assert result == {
        "op_id": "stop-1",
        "ok": True,
        "result": {
            "active": False,
            "status": "stopped",
            "session_id": "",
        },
    }
    manager._helper_request.assert_awaited_once_with(
        session,
        {
            "action": "stop_session",
            "context_id": "ctx-1",
            "session_id": "sess-1",
        },
    )
    manager._close_helper_session.assert_awaited_once_with(session)
    assert session.active is False
    assert session.status == "stopped"
