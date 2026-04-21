from __future__ import annotations

import asyncio
import base64
import json
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
        backend_selection=backend_selection,
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
) -> ComputerUseBackendSpec:
    return ComputerUseBackendSpec(
        backend_id=backend_id,
        backend_family=backend_family,
        priority=priority,
        detect=lambda: detected,
        features=features,
        interpreter_strategy="system_python",
        helper_target="/tmp/computer_use_helper.py",
        trust_mode_support=("interactive", "persistent", "free_run"),
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
) -> ComputerUseBackendSelection:
    spec = _backend_spec(
        backend_id=backend_id,
        backend_family=backend_family,
        priority=priority,
        detected=detected,
        features=features,
        support_reason=support_reason,
    )
    return ComputerUseBackendSelection(
        spec=spec,
        supported=detected,
        support_reason=support_reason,
    )


def test_default_host_artifact_root_uses_dockervolume_mapping_on_macos(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    volume_root = tmp_path / "dockervolume"
    volume_root.mkdir()
    monkeypatch.delenv(computer_use_mod._HOST_ARTIFACT_ROOT_ENV, raising=False)
    monkeypatch.setattr(computer_use_mod, "_find_dockervolume_root", lambda: volume_root)
    monkeypatch.setattr(computer_use_mod.sys, "platform", "darwin")

    host_root = computer_use_mod._default_host_artifact_root("/a0/tmp/_a0_connector/computer_use")

    assert host_root == volume_root / "tmp" / "_a0_connector" / "computer_use"


def test_default_host_artifact_root_uses_tempdir_fallback_on_macos(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(computer_use_mod._HOST_ARTIFACT_ROOT_ENV, raising=False)
    monkeypatch.setattr(computer_use_mod, "_find_dockervolume_root", lambda: None)
    monkeypatch.setattr(computer_use_mod.sys, "platform", "darwin")
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


async def test_free_run_without_restore_token_returns_rearm_required(
    _temp_env: Path,
) -> None:
    manager = _manager(enabled=True, trust_mode="free_run")

    result = await manager.handle_op({"op_id": "start-1", "action": "start_session", "context_id": "ctx-1"})

    assert result["ok"] is False
    assert result["code"] == "COMPUTER_USE_REARM_REQUIRED"
    assert manager.status_label == "rearm required"


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
    monkeypatch.setattr(backend_mod, "available_backend_specs", lambda: [undetected])

    selection = backend_mod.resolve_backend_selection()

    assert selection.spec is undetected
    assert selection.supported is False
    assert "Wayland portal backend" in selection.support_reason


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
    assert result["result"]["host_path"].startswith(str(computer_use_mod.HOST_ARTIFACT_ROOT / "ctx-1"))
    assert result["result"]["capture_path"] == result["result"]["host_path"]
    assert result["result"]["container_path"].startswith(f"{computer_use_mod.CONTAINER_ARTIFACT_ROOT}/ctx-1/")


async def test_capture_preserves_path_based_result_without_reinlining_png(
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
    assert result["result"]["host_path"] == str(capture_path)


async def test_capture_requests_shared_artifact_path_and_adds_container_path(
    _temp_env: Path,
) -> None:
    manager = _manager(enabled=True)

    async def helper_request(_session: _HelperSession, request: dict[str, object]) -> dict[str, object]:
        capture_path = str(request.get("capture_path") or "")
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
    assert result["result"]["capture_path"].startswith(
        str(computer_use_mod.HOST_ARTIFACT_ROOT / "ctx-1")
    )
    assert result["result"]["host_path"] == result["result"]["capture_path"]
    assert result["result"]["container_path"].startswith(
        f"{computer_use_mod.CONTAINER_ARTIFACT_ROOT}/ctx-1/"
    )


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


async def test_ensure_helper_uses_expanded_stdio_limit_for_large_capture_payloads(
    _temp_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(enabled=True)
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
    typed = manager._normalize_action_payload("type", {"text": "hello"}, context_id="ctx-1")
    submitted = manager._normalize_action_payload(
        "type",
        {"text": "hello", "submit": True},
        context_id="ctx-1",
    )

    assert move["x"] == 0.25 and move["y"] == 0.75
    assert click["button"] == "right" and click["count"] == 2
    assert scroll["dx"] == 1 and scroll["dy"] == -2
    assert key["keys"] == ["ctrl", "alt", "t"]
    assert typed["text"] == "hello"
    assert submitted["submit"] is True


async def test_normalized_coordinates_are_clamped_to_unit_interval(
    _temp_env: Path,
) -> None:
    manager = _manager(enabled=True)

    move = manager._normalize_action_payload("move", {"x": -5, "y": 99}, context_id="ctx-1")

    assert move["x"] == 0.0
    assert move["y"] == 1.0


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


async def test_free_run_with_invalid_restore_token_returns_rearm_required(
    _temp_env: Path,
) -> None:
    manager = _manager(enabled=True, trust_mode="free_run", restore_token="restore-123")
    manager._helper_request = AsyncMock()  # type: ignore[method-assign]

    result = await manager.handle_op({"op_id": "start-1", "action": "start_session", "context_id": "ctx-1"})

    assert result["ok"] is False
    assert result["code"] == "COMPUTER_USE_REARM_REQUIRED"
    manager._helper_request.assert_not_awaited()
    assert manager.restore_token == ""


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
