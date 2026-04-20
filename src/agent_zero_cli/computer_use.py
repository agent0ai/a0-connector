from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from agent_zero_cli import computer_use_wayland as _builtin_computer_use_wayland  # noqa: F401
from agent_zero_cli.computer_use_backend import (
    ComputerUseBackendSelection,
    resolve_backend_selection,
)
from agent_zero_cli.config import (
    CLIConfig,
    normalize_computer_use_trust_mode,
    save_computer_use_enabled,
    save_computer_use_restore_token,
    save_computer_use_trust_mode,
)

HELPER_PYTHON = "/usr/bin/python3"
HOST_ARTIFACT_ROOT = Path("/home/eclypso/agentdocker/tmp/_a0_connector/computer_use")
CONTAINER_ARTIFACT_ROOT = "/a0/tmp/_a0_connector/computer_use"
_CAPTURE_RETENTION_MAX_FILES = 24
_CAPTURE_RETENTION_MAX_AGE_SECONDS = 60 * 60 * 24
_SUPPORTED_ACTIONS = {
    "start_session",
    "status",
    "capture",
    "move",
    "click",
    "scroll",
    "key",
    "type",
    "stop_session",
}
_SUPPORTED_TRUST_MODES = {"interactive", "persistent", "free_run"}
_DISABLED_ERROR = "COMPUTER_USE_DISABLED"
_REARM_REQUIRED_ERROR = "COMPUTER_USE_REARM_REQUIRED"
_SESSION_REQUIRED_ERROR = "COMPUTER_USE_SESSION_REQUIRED"
_UNSUPPORTED_ERROR = "COMPUTER_USE_UNSUPPORTED"


def _normalize_context_id(value: object) -> str:
    context_id = str(value or "").strip()
    if context_id:
        return context_id
    return "default"


def _safe_context_segment(value: str) -> str:
    cleaned = []
    for char in value:
        if char.isalnum() or char in {"-", "_", "."}:
            cleaned.append(char)
        else:
            cleaned.append("_")
    return "".join(cleaned) or "default"


def _clamp_unit_interval(value: object, *, name: str) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number in [0, 1]") from exc
    return min(max(numeric, 0.0), 1.0)


def _coerce_int(value: object, *, name: str, default: int | None = None) -> int:
    if value is None and default is not None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _coerce_bool(value: object, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off", ""}:
        return False
    return default


def _normalize_restore_token(value: object) -> str:
    token = str(value or "").strip()
    if not token:
        return ""
    try:
        return str(uuid.UUID(token))
    except (ValueError, AttributeError, TypeError):
        return ""


def _helper_script_path() -> str:
    return str(Path(__file__).with_name("computer_use_helper.py"))


def _backend_metadata_from_selection(selection: ComputerUseBackendSelection) -> dict[str, Any]:
    spec = selection.spec
    if spec is None:
        return {
            "backend_id": "unsupported",
            "backend_family": "unknown",
            "features": [],
            "support_reason": selection.support_reason,
        }
    return {
        "backend_id": spec.backend_id,
        "backend_family": spec.backend_family,
        "features": list(spec.features),
        "support_reason": selection.support_reason,
    }


@dataclass
class _HelperSession:
    context_id: str
    process: asyncio.subprocess.Process | None = None
    stderr_task: asyncio.Task[None] | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    session_id: str = ""
    active: bool = False
    status: str = "idle"
    last_result: dict[str, Any] = field(default_factory=dict)
    session_result: dict[str, Any] = field(default_factory=dict)


class ComputerUseManager:
    def __init__(
        self,
        config: CLIConfig,
        *,
        backend_selection: ComputerUseBackendSelection | None = None,
    ) -> None:
        self.config = config
        self.enabled = bool(config.computer_use_enabled)
        self.trust_mode = normalize_computer_use_trust_mode(config.computer_use_trust_mode)
        self.restore_token = str(config.computer_use_restore_token or "").strip()
        self._backend_selection = backend_selection or resolve_backend_selection()
        self._backend_spec = self._backend_selection.spec
        self._backend_metadata = _backend_metadata_from_selection(self._backend_selection)
        self.supported = self._backend_selection.supported
        self.status = "disabled" if not self.enabled else self.trust_mode
        self.last_error = ""
        self._sessions: dict[str, _HelperSession] = {}
        self._status_callback: Callable[[str, str], None] | None = None

    @property
    def status_label(self) -> str:
        return self.status

    @property
    def status_detail(self) -> str:
        return self.last_error

    def hello_metadata(self) -> dict[str, Any]:
        metadata = {
            "supported": self.supported,
            "enabled": self.enabled and self.supported,
            "trust_mode": self.trust_mode,
            "artifact_root": CONTAINER_ARTIFACT_ROOT,
        }
        metadata.update(self._backend_metadata)
        return metadata

    def metadata(self) -> dict[str, Any]:
        return self.hello_metadata()

    def set_status_callback(self, callback: Callable[[str, str], None] | None) -> None:
        self._status_callback = callback
        if callback is not None:
            callback(self.status, self.last_error)

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = bool(enabled)
        self.config.computer_use_enabled = self.enabled
        save_computer_use_enabled(self.enabled)
        if not self.enabled:
            self._set_status("disabled")
            return
        if self.status == "disabled":
            self._set_status(self.trust_mode)

    def set_trust_mode(self, mode: str) -> str:
        normalized = normalize_computer_use_trust_mode(mode)
        self.trust_mode = normalized
        self.config.computer_use_trust_mode = normalized
        save_computer_use_trust_mode(normalized)
        if self.enabled and self.status in {"interactive", "persistent", "free_run"}:
            self._set_status(normalized)
        return normalized

    def update_restore_token(self, token: str) -> None:
        normalized = _normalize_restore_token(token)
        self.restore_token = normalized
        self.config.computer_use_restore_token = normalized
        save_computer_use_restore_token(normalized)

    def _current_restore_token(self) -> str:
        normalized = _normalize_restore_token(self.restore_token)
        if normalized != self.restore_token:
            self.update_restore_token(normalized)
        return normalized

    def _set_status(self, status: str, *, error: str = "") -> None:
        self.status = status
        self.last_error = error
        if self._status_callback is not None:
            self._status_callback(status, error)

    def _session_snapshot(self) -> dict[str, Any]:
        active_contexts = sorted(
            session.context_id for session in self._sessions.values() if session.active
        )
        snapshot = {
            "supported": self.supported,
            "enabled": self.enabled,
            "trust_mode": self.trust_mode,
            "status": self.status,
            "restore_token_present": bool(_normalize_restore_token(self.restore_token)),
            "artifact_root": CONTAINER_ARTIFACT_ROOT,
            "host_artifact_root": str(HOST_ARTIFACT_ROOT),
            "active_contexts": active_contexts,
            "last_error": self.last_error or None,
        }
        snapshot.update(self._backend_metadata)
        return snapshot

    def _success(self, op_id: str, result: dict[str, Any]) -> dict[str, Any]:
        return {"op_id": op_id, "ok": True, "result": result}

    def _error(
        self,
        op_id: str,
        code: str,
        *,
        message: str | None = None,
        result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = {
            "op_id": op_id,
            "ok": False,
            "error": message or code,
            "code": code,
        }
        if result is not None:
            payload["result"] = result
        return payload

    async def close(self) -> None:
        for session in list(self._sessions.values()):
            await self._close_helper_session(session)
        self._sessions.clear()
        if self.enabled:
            self._set_status(self.trust_mode)
        else:
            self._set_status("disabled")

    async def disconnect(self) -> None:
        await self.close()

    async def handle_op(self, payload: dict[str, Any]) -> dict[str, Any]:
        op_id = str(payload.get("op_id", "")).strip()
        action = str(payload.get("action", "")).strip().lower()
        context_id = _normalize_context_id(payload.get("context_id"))

        if not op_id:
            return {"op_id": "", "ok": False, "error": "op_id is required", "code": "MISSING_OP_ID"}

        if action not in _SUPPORTED_ACTIONS:
            return self._error(op_id, "UNKNOWN_ACTION", message=f"Unknown action: {action!r}")

        if action == "status":
            snapshot = self._session_snapshot()
            snapshot["context_id"] = context_id
            return self._success(op_id, snapshot)

        if not self.supported:
            self._set_status("error", error=_UNSUPPORTED_ERROR)
            return self._error(op_id, _UNSUPPORTED_ERROR, result=self._session_snapshot())

        if not self.enabled:
            self._set_status("disabled", error=_DISABLED_ERROR)
            return self._error(op_id, _DISABLED_ERROR, result=self._session_snapshot())

        session = self._sessions.setdefault(context_id, _HelperSession(context_id=context_id))
        try:
            if action == "start_session":
                return await self._start_session(op_id, session)
            if action == "stop_session":
                return await self._stop_session(op_id, session)
            request = self._normalize_action_payload(action, payload, context_id=context_id)
            return await self._dispatch_session_action(op_id, session, request)
        except ValueError as exc:
            self._set_status("error", error=str(exc))
            return self._error(op_id, "BAD_REQUEST", message=str(exc))

    def _normalize_action_payload(
        self,
        action: str,
        payload: dict[str, Any],
        *,
        context_id: str,
    ) -> dict[str, Any]:
        request: dict[str, Any] = {
            "action": action,
            "context_id": context_id,
        }
        session_id = str(payload.get("session_id", "")).strip()
        if session_id:
            request["session_id"] = session_id

        if action == "capture":
            return request

        if action == "move":
            request["x"] = _clamp_unit_interval(payload.get("x"), name="x")
            request["y"] = _clamp_unit_interval(payload.get("y"), name="y")
            return request

        if action == "click":
            if payload.get("x") is not None:
                request["x"] = _clamp_unit_interval(payload.get("x"), name="x")
            if payload.get("y") is not None:
                request["y"] = _clamp_unit_interval(payload.get("y"), name="y")
            request["button"] = str(payload.get("button", "left") or "left").strip().lower()
            request["count"] = _coerce_int(payload.get("count"), name="count", default=1)
            if request["count"] < 1:
                raise ValueError("count must be >= 1")
            return request

        if action == "scroll":
            delta_x = payload.get("dx", payload.get("delta_x", payload.get("steps_x", 0)))
            delta_y = payload.get("dy", payload.get("delta_y", payload.get("steps_y", 0)))
            request["dx"] = _coerce_int(delta_x, name="dx", default=0)
            request["dy"] = _coerce_int(delta_y, name="dy", default=0)
            if request["dx"] == 0 and request["dy"] == 0:
                raise ValueError("scroll requires dx or dy")
            return request

        if action == "key":
            keys_value = payload.get("keys")
            if isinstance(keys_value, (list, tuple)):
                keys = [str(item).strip() for item in keys_value if str(item).strip()]
            else:
                raw = str(payload.get("key", keys_value or "")).strip()
                keys = [part.strip() for part in raw.split("+") if part.strip()]
            if not keys:
                raise ValueError("key requires key or keys")
            request["keys"] = keys
            return request

        if action == "type":
            text = str(payload.get("text", "") or "")
            if not text:
                raise ValueError("type requires text")
            request["text"] = text
            if _coerce_bool(payload.get("submit")):
                request["submit"] = True
            return request

        raise ValueError(f"Unsupported action: {action}")

    def _next_capture_paths(self, context_id: str) -> tuple[str, str]:
        stamp = uuid.uuid4().hex
        context_segment = _safe_context_segment(context_id)
        host_dir = HOST_ARTIFACT_ROOT / context_segment
        host_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{stamp}.png"
        host_path = host_dir / filename
        container_path = f"{CONTAINER_ARTIFACT_ROOT}/{context_segment}/{filename}"
        return str(host_path), container_path

    def _prune_capture_artifacts(self, context_id: str, *, keep_path: str = "") -> None:
        context_segment = _safe_context_segment(context_id)
        host_dir = HOST_ARTIFACT_ROOT / context_segment
        if not host_dir.exists():
            return

        keep_target = Path(keep_path) if keep_path else None
        captures: list[tuple[Path, float]] = []
        now = time.time()

        for entry in host_dir.iterdir():
            if not entry.is_file() or entry.suffix.lower() != ".png":
                continue
            with contextlib.suppress(OSError):
                stat = entry.stat()
                if keep_target is not None and entry == keep_target:
                    captures.append((entry, stat.st_mtime))
                    continue
                if now - stat.st_mtime > _CAPTURE_RETENTION_MAX_AGE_SECONDS:
                    entry.unlink(missing_ok=True)
                    continue
                captures.append((entry, stat.st_mtime))

        if len(captures) > _CAPTURE_RETENTION_MAX_FILES:
            captures.sort(key=lambda item: (item[1], item[0].name), reverse=True)
            for stale_path, _ in captures[_CAPTURE_RETENTION_MAX_FILES :]:
                if keep_target is not None and stale_path == keep_target:
                    continue
                with contextlib.suppress(OSError):
                    stale_path.unlink(missing_ok=True)

        with contextlib.suppress(OSError):
            if not any(host_dir.iterdir()):
                host_dir.rmdir()

    async def _ensure_helper(self, session: _HelperSession) -> _HelperSession:
        process = session.process
        if process is not None and process.returncode is None:
            return session

        helper_target = str(getattr(self._backend_spec, "helper_target", "") or "").strip()
        if not helper_target:
            helper_target = _helper_script_path()
        interpreter_strategy = str(getattr(self._backend_spec, "interpreter_strategy", "") or "").strip()
        if interpreter_strategy == "current_python":
            helper_python = sys.executable or HELPER_PYTHON
        else:
            helper_python = HELPER_PYTHON

        process = await asyncio.create_subprocess_exec(
            helper_python,
            helper_target,
            "--stdio",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        session.process = process
        session.stderr_task = asyncio.create_task(self._drain_stderr(process))
        session.active = False
        session.status = "idle"
        session.session_id = ""
        return session

    async def _drain_stderr(self, process: asyncio.subprocess.Process) -> None:
        if process.stderr is None:
            return
        try:
            while True:
                line = await process.stderr.readline()
                if not line:
                    return
        except Exception:
            return

    async def _helper_request(self, session: _HelperSession, request: dict[str, Any]) -> dict[str, Any]:
        await self._ensure_helper(session)
        process = session.process
        if process is None or process.stdin is None or process.stdout is None:
            raise RuntimeError("computer use helper is unavailable")
        if process.returncode is not None:
            raise RuntimeError("computer use helper exited unexpectedly")

        payload = dict(request)
        payload.setdefault("request_id", uuid.uuid4().hex)

        async with session.lock:
            process.stdin.write((json.dumps(payload, ensure_ascii=True) + "\n").encode("utf-8"))
            await process.stdin.drain()
            raw = await process.stdout.readline()

        if not raw:
            raise RuntimeError("computer use helper closed its stdout")

        try:
            response = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise RuntimeError("computer use helper returned invalid JSON") from exc

        if not isinstance(response, dict):
            raise RuntimeError("computer use helper returned an invalid response")
        session.last_result = response
        return response

    async def _dispatch_session_action(
        self,
        op_id: str,
        session: _HelperSession,
        request: dict[str, Any],
    ) -> dict[str, Any]:
        if not session.active or not session.session_id:
            self._set_status(self.trust_mode)
            return self._error(op_id, _SESSION_REQUIRED_ERROR)

        requested_session_id = str(request.get("session_id", "")).strip()
        if requested_session_id and requested_session_id != session.session_id:
            return self._error(
                op_id,
                "COMPUTER_USE_SESSION_MISMATCH",
                message="Requested session_id does not match the active computer-use session",
            )

        action_name = str(request.get("action", "")).strip().lower()
        helper_request = {
            **request,
            "session_id": session.session_id,
        }
        capture_host_path = ""
        capture_container_path = ""
        if action_name == "capture":
            capture_host_path, capture_container_path = self._next_capture_paths(session.context_id)
            helper_request["capture_path"] = capture_host_path

        response = await self._helper_request(session, helper_request)
        if action_name == "capture" and bool(response.get("ok")) and isinstance(response.get("result"), dict):
            result_dict = dict(response["result"])
            if capture_host_path:
                result_dict.setdefault("host_path", capture_host_path)
                result_dict.setdefault("capture_path", capture_host_path)
            if capture_container_path:
                result_dict.setdefault("container_path", capture_container_path)
            response = {
                **response,
                "result": result_dict,
            }
        return self._normalize_helper_response(op_id, session, response, action=str(request.get("action", "")))

    async def _start_session(self, op_id: str, session: _HelperSession) -> dict[str, Any]:
        restore_token = self._current_restore_token()
        if self.trust_mode == "free_run" and not restore_token:
            self._set_status("rearm required", error=_REARM_REQUIRED_ERROR)
            return self._error(op_id, _REARM_REQUIRED_ERROR, result=self._session_snapshot())

        if session.active and session.session_id:
            result = dict(session.session_result)
            result.setdefault("session_id", session.session_id)
            result.setdefault("status", "active")
            result.setdefault("active", True)
            self._set_status("active")
            return self._success(op_id, result)

        request = {
            "action": "start_session",
            "context_id": session.context_id,
            "trust_mode": self.trust_mode,
            "restore_token": restore_token,
        }
        if self.trust_mode == "free_run":
            request["allow_prompt"] = False
            request["request_timeout_seconds"] = 2.0
        else:
            request["allow_prompt"] = True
            request["request_timeout_seconds"] = 180.0

        self._set_status("approval required" if self.trust_mode != "free_run" else "free_run")
        response = await self._helper_request(session, request)
        return self._normalize_helper_response(op_id, session, response, action="start_session")

    async def _stop_session(self, op_id: str, session: _HelperSession) -> dict[str, Any]:
        if session.process is None or session.process.returncode is not None:
            session.active = False
            session.session_id = ""
            session.status = "stopped"
            self._set_status(self.trust_mode)
            return self._success(
                op_id,
                {
                    "context_id": session.context_id,
                    "session_id": "",
                    "status": "stopped",
                },
            )

        try:
            response = await self._helper_request(
                session,
                {
                    "action": "stop_session",
                    "context_id": session.context_id,
                    "session_id": session.session_id,
                },
            )
        finally:
            await self._close_helper_session(session)

        self._set_status(self.trust_mode)
        return self._normalize_helper_response(op_id, session, response, action="stop_session")

    async def _close_helper_session(self, session: _HelperSession) -> None:
        process = session.process
        session.process = None
        session.active = False
        session.session_id = ""
        session.status = "stopped"

        if process is not None and process.returncode is None:
            with contextlib.suppress(Exception):
                if process.stdin is not None:
                    process.stdin.write(b"{\"action\":\"shutdown\"}\n")
                    await process.stdin.drain()
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(process.wait(), timeout=2.0)
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                with contextlib.suppress(Exception):
                    await process.wait()

        stderr_task = session.stderr_task
        session.stderr_task = None
        if stderr_task is not None:
            stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stderr_task

    def _normalize_helper_response(
        self,
        op_id: str,
        session: _HelperSession,
        response: dict[str, Any],
        *,
        action: str,
    ) -> dict[str, Any]:
        ok = bool(response.get("ok"))
        result = response.get("result")
        result_dict = dict(result) if isinstance(result, dict) else {}
        code = str(response.get("code", "") or "")
        error = str(response.get("error", "") or "")
        action_name = str(action or "").strip().lower()
        previous_session_id = session.session_id
        previous_active = session.active
        previous_status = session.status

        if ok:
            session.session_id = str(result_dict.get("session_id", session.session_id or "")).strip()
            if "active" in result_dict:
                session.active = bool(result_dict.get("active"))
            elif "status" in result_dict:
                session.active = str(result_dict.get("status", "")).strip().lower() == "active"
            elif action == "start_session":
                session.active = True
            elif action == "stop_session":
                session.active = False

            if "status" in result_dict:
                session.status = str(result_dict.get("status", "")).strip() or "idle"
            elif action == "start_session":
                session.status = "active" if session.active else "idle"
            elif action == "stop_session":
                session.status = "stopped"
            if action == "start_session":
                session.session_result = dict(result_dict)
            elif action == "stop_session":
                session.session_result = {}
            if action_name == "capture":
                result_dict = self._normalize_capture_result(result_dict)
            restore_token = str(result_dict.get("restore_token", "") or "").strip()
            if restore_token and self.trust_mode in {"persistent", "free_run"}:
                self.update_restore_token(restore_token)
            if action_name == "capture":
                keep_path = str(
                    result_dict.get("capture_path")
                    or result_dict.get("container_path")
                    or result_dict.get("host_path")
                    or ""
                ).strip()
                if keep_path:
                    self._prune_capture_artifacts(session.context_id, keep_path=keep_path)
            self._set_status("active" if session.active else self.trust_mode)
            return self._success(op_id, result_dict)

        if code == _REARM_REQUIRED_ERROR or error == _REARM_REQUIRED_ERROR:
            self._set_status("rearm required", error=_REARM_REQUIRED_ERROR)
            return self._error(op_id, _REARM_REQUIRED_ERROR, result=self._session_snapshot())

        if code == _DISABLED_ERROR or error == _DISABLED_ERROR:
            self._set_status("disabled", error=_DISABLED_ERROR)
            return self._error(op_id, _DISABLED_ERROR, result=self._session_snapshot())

        message = error or code or "Computer-use operation failed"
        preserve_session = (
            action_name not in {"start_session", "stop_session"}
            and bool(previous_session_id or previous_active)
            and code not in {_SESSION_REQUIRED_ERROR, "COMPUTER_USE_SESSION_MISMATCH"}
            and error not in {_SESSION_REQUIRED_ERROR, "COMPUTER_USE_SESSION_MISMATCH"}
        )
        if preserve_session:
            session.session_id = previous_session_id
            session.active = previous_active
            session.status = previous_status or ("active" if previous_active else "idle")
            self._set_status("active" if session.active else self.trust_mode, error=message)
            return self._error(op_id, code or "COMPUTER_USE_ERROR", message=message, result=result_dict or None)

        session.active = False
        session.status = "error"
        self._set_status("error", error=message)
        return self._error(op_id, code or "COMPUTER_USE_ERROR", message=message, result=result_dict or None)

    def _normalize_capture_result(self, result: dict[str, Any]) -> dict[str, Any]:
        if str(result.get("png_base64", "") or "").strip():
            return result

        candidates = [
            str(result.get("capture_path", "") or "").strip(),
            str(result.get("container_path", "") or "").strip(),
            str(result.get("host_path", "") or "").strip(),
        ]
        for candidate in candidates:
            if not candidate:
                continue
            path = Path(candidate)
            if not path.exists():
                continue
            normalized = dict(result)
            normalized.setdefault("capture_path", candidate)
            return normalized
        return result
