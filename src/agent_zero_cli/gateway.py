from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
import platform
import re
import signal
import sys
import threading
import uuid
from typing import Any, Callable, TextIO
from urllib.parse import urlsplit, urlunsplit

from agent_zero_cli.attachments import AttachmentError, AttachmentUpload, create_file_upload
from agent_zero_cli.client import DEFAULT_HOST
from agent_zero_cli.computer_use import ComputerUseManager
from agent_zero_cli.config import CLIConfig
from agent_zero_cli.host_browser_manager import HostBrowserManager
from agent_zero_cli.profile_commands import profile_menu_state_from_settings
from agent_zero_cli.session import ConnectorSession, SessionError


_SCOPE_KEYS = ("files", "file_write", "code_execution", "browser", "computer_use")
_GATEWAY_FEATURES = ("computer_use_setup_v1",)
_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9._:-]+")
_TAG_RESULT_MAX_CHARS = 16384
_TAG_CONTEXT_CHUNK_CHARS = 2048
_TAG_CONTEXT_MAX_CHUNKS = 16
_TAG_SCREENSHOT_MAX_BYTES = 16 * 1024 * 1024
_TAG_UPLOAD_MAX_SELECTIONS = 16
_TAG_UPLOAD_MAX_FILES = 128
_TAG_UPLOAD_MAX_FILE_BYTES = 25 * 1024 * 1024
_TAG_UPLOAD_MAX_TOTAL_BYTES = 100 * 1024 * 1024
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _text_chunks(value: object) -> list[str]:
    text = str(value or "")
    return [
        text[index : index + _TAG_CONTEXT_CHUNK_CHARS]
        for index in range(0, min(len(text), _TAG_CONTEXT_CHUNK_CHARS * _TAG_CONTEXT_MAX_CHUNKS), _TAG_CONTEXT_CHUNK_CHARS)
    ]


def _tag_uploads_for_paths(value: object) -> list[AttachmentUpload]:
    if not isinstance(value, list) or not value or len(value) > _TAG_UPLOAD_MAX_SELECTIONS:
        raise SessionError(
            "A0_TAG_ATTACHMENTS_INVALID",
            f"Choose between 1 and {_TAG_UPLOAD_MAX_SELECTIONS} files or folders.",
            exit_code=1,
        )

    files: list[Path] = []
    seen: set[str] = set()
    for raw_path in value:
        raw = str(raw_path or "").strip()
        if not raw or "\0" in raw or len(raw) > 4096:
            raise SessionError("A0_TAG_ATTACHMENTS_INVALID", "A selected attachment path is invalid.", exit_code=1)
        selected = Path(raw).expanduser()
        if not selected.is_absolute():
            raise SessionError("A0_TAG_ATTACHMENTS_INVALID", "Attachment paths must be absolute.", exit_code=1)
        try:
            selected = selected.resolve(strict=True)
        except OSError as exc:
            raise SessionError("A0_TAG_ATTACHMENT_READ_FAILED", f"Could not open {selected.name}.", exit_code=1) from exc

        try:
            candidates = [selected] if selected.is_file() else (
                sorted(
                    (item for item in selected.rglob("*") if item.is_file() and not item.is_symlink()),
                    key=lambda item: str(item).casefold(),
                )
                if selected.is_dir()
                else []
            )
        except OSError as exc:
            raise SessionError(
                "A0_TAG_ATTACHMENT_READ_FAILED",
                f"Could not read files from {selected.name}.",
                exit_code=1,
            ) from exc
        if not candidates:
            raise SessionError("A0_TAG_ATTACHMENT_EMPTY", f"No files were found in {selected.name}.", exit_code=1)
        for candidate in candidates:
            try:
                marker = os.path.normcase(str(candidate.resolve(strict=True)))
            except OSError as exc:
                raise SessionError(
                    "A0_TAG_ATTACHMENT_READ_FAILED",
                    f"Could not open {candidate.name}.",
                    exit_code=1,
                ) from exc
            if marker in seen:
                continue
            seen.add(marker)
            files.append(candidate)
            if len(files) > _TAG_UPLOAD_MAX_FILES:
                raise SessionError(
                    "A0_TAG_ATTACHMENTS_TOO_MANY",
                    f"A0 Tag accepts up to {_TAG_UPLOAD_MAX_FILES} files per request.",
                    exit_code=1,
                )

    uploads: list[AttachmentUpload] = []
    total_bytes = 0
    try:
        for source in files:
            upload = create_file_upload(source, max_bytes=_TAG_UPLOAD_MAX_FILE_BYTES)
            total_bytes += len(upload.content)
            if total_bytes > _TAG_UPLOAD_MAX_TOTAL_BYTES:
                raise SessionError(
                    "A0_TAG_ATTACHMENTS_TOO_LARGE",
                    "A0 Tag attachments may total at most 100 MiB.",
                    exit_code=1,
                )
            uploads.append(upload)
    except AttachmentError as exc:
        raise SessionError("A0_TAG_ATTACHMENT_READ_FAILED", str(exc), exit_code=1) from exc
    return uploads


def sanitize_gateway_id(value: object) -> str:
    return _SAFE_ID_RE.sub("-", str(value or "").strip())[:128].strip("-")


def sanitize_host_label(value: object) -> str:
    label = " ".join(str(value or "").split())
    return label[:128] or platform.node()[:128] or "Launcher host"


def normalize_gateway_host(value: object) -> str:
    raw = str(value or "").strip().rstrip("/") or DEFAULT_HOST
    parsed = urlsplit(raw)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("gateway host must be an HTTP(S) URL without embedded credentials")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


def normalize_scopes(value: object) -> dict[str, bool]:
    if isinstance(value, dict):
        scopes = {
            key: bool(value.get(key, value.get("files") if key == "file_write" else False))
            for key in _SCOPE_KEYS
        }
    else:
        requested = {
            item.strip().lower().replace("-", "_")
            for item in str(value or "").split(",")
            if item.strip()
        }
        legacy_files = "files" in requested
        aliases = {
            "exec": "code_execution",
            "computer": "computer_use",
            "file_read": "files",
        }
        requested = {aliases.get(item, item) for item in requested}
        scopes = {key: key in requested for key in _SCOPE_KEYS}
        if legacy_files:
            scopes["file_write"] = True
    if not scopes["files"]:
        scopes["file_write"] = False
    if not scopes["file_write"]:
        scopes["code_execution"] = False
    return scopes


@dataclass(frozen=True)
class GatewayOptions:
    host: str
    workspace: Path
    gateway_id: str
    host_label: str
    master_enabled: bool
    scopes: dict[str, bool]
    browser_selection: str = ""


class JsonlWriter:
    def __init__(self, stream: TextIO = sys.stdout) -> None:
        self.stream = stream

    def write(self, payload: dict[str, Any]) -> None:
        self.stream.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
        self.stream.flush()


class GatewayObserver:
    def __init__(
        self,
        emit: Callable[[dict[str, Any]], None],
        stop: Callable[[], None],
    ) -> None:
        self._emit = emit
        self._stop = stop

    def on_stage(self, stage: str, message: str, detail: str = "") -> None:
        self._emit({"type": "stage", "stage": stage, "message": message, "detail": detail})

    def on_event(self, event: dict[str, Any]) -> None:
        return None

    def on_snapshot(self, events: list[dict[str, Any]], queue: list[dict[str, Any]]) -> None:
        return None

    def on_complete(self, context_id: str) -> None:
        return None

    def on_error(self, code: str, message: str) -> None:
        self._emit({"type": "error", "code": code, "message": message, "fatal": False})

    def on_disconnect(self) -> None:
        self._emit(
            {
                "type": "error",
                "code": "CONNECTION_LOST",
                "message": "Agent Zero connection recovery was exhausted.",
                "fatal": True,
            }
        )
        self._stop()


class GatewayRunner:
    def __init__(
        self,
        options: GatewayOptions,
        config: CLIConfig,
        *,
        writer: JsonlWriter | None = None,
        input_stream: TextIO = sys.stdin,
        session_factory: Callable[..., ConnectorSession] = ConnectorSession,
        browser_factory: Callable[..., HostBrowserManager] = HostBrowserManager,
        computer_use_factory: Callable[..., ComputerUseManager] = ComputerUseManager,
    ) -> None:
        self.options = options
        self.config = replace(config)
        self.writer = writer or JsonlWriter()
        self.input_stream = input_stream
        self._session_factory = session_factory
        self._browser_factory = browser_factory
        self._computer_use_factory = computer_use_factory
        self.stop_event = asyncio.Event()
        self.session: ConnectorSession | None = None
        self.host_browser: HostBrowserManager | None = None
        self.computer_use: ComputerUseManager | None = None

    async def run(self) -> int:
        workspace = self.options.workspace.expanduser().resolve()
        if not workspace.is_dir():
            self._emit_error("INVALID_WORKSPACE", f"Workspace is not a directory: {workspace}", fatal=True)
            return 2

        scopes = normalize_scopes(self.options.scopes)
        self.config.host_browser_enabled = scopes["browser"]
        self.config.computer_use_enabled = scopes["computer_use"]
        if self.config.computer_use_trust_mode == "allow":
            self.config.computer_use_trust_mode = "persistent"
        self.host_browser = self._browser_factory(self.config, persist_enabled=False)
        self.computer_use = self._computer_use_factory(self.config, persist_enabled=False)
        observer = GatewayObserver(self.writer.write, self.stop_event.set)
        gateway = {
            "version": 1,
            "kind": "launcher",
            "id": self.options.gateway_id,
            "host_label": self.options.host_label,
            "master_enabled": self.options.master_enabled,
            "scopes": scopes,
            "features": [
                *_GATEWAY_FEATURES,
                *(["a0_tag_v1"] if self.computer_use.launcher_tag_supported else []),
            ],
        }
        self.session = self._session_factory(
            self.config,
            observer,
            workspace=workspace,
            remote_file_write_enabled=scopes["file_write"],
            remote_files_enabled=scopes["files"],
            remote_exec_enabled=scopes["code_execution"],
            tools_only=True,
            gateway=gateway,
            host_browser_manager=self.host_browser,
            computer_use_manager=self.computer_use,
            browser_selection=self.options.browser_selection,
            on_gateway_state_change=self._emit_status,
            on_gateway_disconnect=self.stop_event.set,
        )
        self._install_signal_handlers()
        stdin_task: asyncio.Task[None] | None = None
        try:
            await self.session.connect(
                self.options.host,
                username=os.environ.get("A0_USERNAME", ""),
                password=os.environ.get("A0_PASSWORD", ""),
                restore_session=True,
            )
            self._emit_status(self.session._gateway_metadata() or gateway)
            stdin_task = asyncio.create_task(self._read_commands())
            await self.stop_event.wait()
            return 0
        except SessionError as exc:
            self._emit_error(exc.code, exc.message, fatal=True, stage=exc.stage)
            return exc.exit_code
        except Exception as exc:
            self._emit_error("GATEWAY_FAILED", str(exc), fatal=True)
            return 2
        finally:
            if stdin_task is not None:
                stdin_task.cancel()
            if self.session is not None:
                await self.session.close()
            self.writer.write({"type": "stopped"})

    async def _read_commands(self) -> None:
        loop = asyncio.get_running_loop()
        lines: asyncio.Queue[str] = asyncio.Queue()

        def read_lines() -> None:
            while True:
                line = self.input_stream.readline()
                try:
                    loop.call_soon_threadsafe(lines.put_nowait, line)
                except RuntimeError:
                    return
                if line == "":
                    return

        threading.Thread(
            target=read_lines,
            name="a0-gateway-stdin",
            daemon=True,
        ).start()
        while not self.stop_event.is_set():
            line = await lines.get()
            if line == "":
                self.stop_event.set()
                return
            try:
                payload = json.loads(line)
                if not isinstance(payload, dict):
                    raise ValueError("command must be a JSON object")
            except Exception as exc:
                self._emit_error("INVALID_JSON", str(exc), fatal=False)
                continue
            await self._handle_command(payload)

    async def _handle_command(self, payload: dict[str, Any]) -> None:
        request_id = str(payload.get("request_id", "") or "").strip()
        action = str(payload.get("action", "") or "").strip().lower()
        session = self.session
        if session is None:
            self._command_result(request_id, False, error="Gateway is not connected")
            return
        result_sent = False
        refresh_metadata = False
        try:
            result: Any = None
            if action == "status":
                result = session._gateway_metadata()
            elif action == "set_master":
                await session.set_gateway_master(bool(payload.get("enabled")))
                result = session._gateway_metadata()
                refresh_metadata = True
            elif action == "replace_scopes":
                scopes = payload.get("scopes")
                if not isinstance(scopes, dict):
                    raise ValueError("scopes must be an object")
                await session.replace_gateway_scopes(scopes)
                result = session._gateway_metadata()
                refresh_metadata = True
            elif action == "prepare_browser":
                if self.host_browser is None:
                    raise RuntimeError("Browser access is unavailable")
                refresh_metadata = True
                result = await self.host_browser.ensure_available(
                    profile_mode="existing",
                    browser_selection=self.options.browser_selection,
                )
                refresh_metadata = True
            elif action == "rearm_computer_use":
                if self.computer_use is None:
                    raise RuntimeError("Computer Use is unavailable")
                result = await self.computer_use.setup_permissions("launcher", prompt=True)
                refresh_metadata = True
            elif action == "setup_computer_use":
                if self.computer_use is None:
                    raise RuntimeError("Computer Use is unavailable")
                result = await self.computer_use.setup_permissions(
                    "launcher",
                    prompt=bool(payload.get("prompt")),
                )
                refresh_metadata = True
            elif action == "a0_tag_profiles":
                result = await self._tag_profiles()
            elif action == "a0_tag_capture":
                result = await self._tag_capture()
            elif action == "a0_tag_upload":
                result = await self._tag_upload(payload)
            elif action == "a0_tag_apply":
                result = await self._tag_apply(payload)
            elif action == "a0_tag_release":
                result = await self._tag_release(payload)
            elif action in {"shutdown", "stop"}:
                self.stop_event.set()
                result = {"stopping": True}
            else:
                raise ValueError(f"Unknown gateway action: {action}")
            if (
                action in {"rearm_computer_use", "setup_computer_use"}
                and isinstance(result, dict)
                and "ok" in result
            ):
                command_ok = bool(result.get("ok"))
                command_result = result.get("result")
                self._command_result(
                    request_id,
                    command_ok,
                    result=command_result,
                    error=str(result.get("error") or "") if not command_ok else "",
                    code=str(result.get("code") or "") if not command_ok else "",
                )
                result_sent = True
            else:
                self._command_result(request_id, True, result=result)
                result_sent = True
            if refresh_metadata:
                await session.refresh_remote_tool_metadata()
            metadata = session._gateway_metadata()
            if metadata is not None:
                self._emit_status(metadata)
        except Exception as exc:
            code = str(getattr(exc, "code", "") or "GATEWAY_COMMAND_FAILED")
            if result_sent:
                self._emit_error(code, str(exc), fatal=False)
            else:
                self._command_result(
                    request_id,
                    False,
                    error=str(exc),
                    code=code,
                )
            if refresh_metadata:
                try:
                    await session.refresh_remote_tool_metadata()
                    metadata = session._gateway_metadata()
                    if metadata is not None:
                        self._emit_status(metadata)
                except Exception as refresh_exc:
                    self._emit_error("GATEWAY_METADATA_REFRESH_FAILED", str(refresh_exc), fatal=False)

    async def _tag_profiles(self) -> dict[str, Any]:
        session = self._require_session()
        client = session.client
        if client is None:
            raise SessionError("GATEWAY_NOT_CONNECTED", "Gateway is not connected.", exit_code=1)
        default_profile, profiles = profile_menu_state_from_settings(await client.get_settings())
        return {
            "default_profile": default_profile,
            "profiles": [
                {
                    "key": str(item.get("key") or "")[:64],
                    "label": str(item.get("label") or item.get("key") or "")[:128],
                }
                for item in profiles[:64]
                if str(item.get("key") or "").strip()
            ],
        }

    async def _tag_capture(self) -> dict[str, Any]:
        session = self._require_session()
        computer_use = self._require_tag_computer_use(session)
        response = await computer_use.tag_context()
        result = self._computer_result(response)
        target_token = str(result.get("target_token") or "").strip()
        try:
            artifact = result.pop("artifact", None)
            result["focused_text_chunks"] = _text_chunks(result.pop("focused_text", ""))
            tree = result.pop("tree", {})
            result["tree_chunks"] = _text_chunks(
                json.dumps(tree, ensure_ascii=False, separators=(",", ":"))
                if isinstance(tree, dict)
                else ""
            )
            result["attachment_ref"] = ""
            if result.get("screenshot_status") == "attached":
                result["screenshot_status"] = "unavailable"
            if isinstance(artifact, dict):
                encoded = str(artifact.get("data") or "")
                try:
                    content = base64.b64decode(encoded, validate=True)
                except Exception as exc:
                    raise SessionError(
                        "A0_TAG_SCREENSHOT_INVALID",
                        "Computer Use returned an invalid screenshot.",
                        exit_code=1,
                    ) from exc
                if not content.startswith(_PNG_SIGNATURE) or len(content) > _TAG_SCREENSHOT_MAX_BYTES:
                    raise SessionError(
                        "A0_TAG_SCREENSHOT_INVALID",
                        "Computer Use returned an invalid screenshot size.",
                        exit_code=1,
                    )
                client = session.client
                if client is None:
                    raise SessionError("GATEWAY_NOT_CONNECTED", "Gateway is not connected.", exit_code=1)
                refs = await client.upload_attachments(
                    [
                        AttachmentUpload(
                            filename=f"a0-tag-{uuid.uuid4().hex}.png",
                            content=content,
                            mime_type="image/png",
                        )
                    ]
                )
                if refs:
                    result["attachment_ref"] = refs[0].path
                    result["screenshot_status"] = "attached"
            return result
        except Exception:
            try:
                await computer_use.tag_release(target_token)
            except Exception:
                pass
            raise

    async def _tag_apply(self, payload: dict[str, Any]) -> dict[str, Any]:
        session = self._require_session()
        computer_use = self._require_tag_computer_use(session)
        token = str(payload.get("target_token") or "").strip()[:128]
        replacement = str(payload.get("replacement") or "")
        if not token:
            raise ValueError("target_token is required")
        if not replacement or len(replacement) > _TAG_RESULT_MAX_CHARS:
            raise ValueError("replacement must contain 1 to 16384 characters")
        return self._computer_result(await computer_use.tag_replace(token, replacement))

    async def _tag_upload(self, payload: dict[str, Any]) -> dict[str, Any]:
        session = self._require_session()
        self._require_tag_computer_use(session)
        client = session.client
        if client is None:
            raise SessionError("GATEWAY_NOT_CONNECTED", "Gateway is not connected.", exit_code=1)
        uploads = await asyncio.to_thread(_tag_uploads_for_paths, payload.get("paths"))
        refs = await client.upload_attachments(uploads)
        return {"attachment_refs": [ref.path for ref in refs]}

    async def _tag_release(self, payload: dict[str, Any]) -> dict[str, Any]:
        token = str(payload.get("target_token") or "").strip()[:128]
        if not token or self.computer_use is None:
            return {"released": False}
        return self._computer_result(await self.computer_use.tag_release(token))

    def _require_session(self) -> ConnectorSession:
        if self.session is None:
            raise SessionError("GATEWAY_NOT_CONNECTED", "Gateway is not connected.", exit_code=1)
        return self.session

    def _require_tag_computer_use(self, session: ConnectorSession) -> ComputerUseManager:
        if not session._scope_available("computer_use"):
            raise SessionError(
                "COMPUTER_USE_DISABLED",
                "A0 Tag requires the selected Instance's Computer Use permission.",
                exit_code=1,
            )
        if self.computer_use is None or not self.computer_use.launcher_tag_supported:
            raise SessionError(
                "A0_TAG_UNSUPPORTED",
                "A0 Tag is unavailable on this Computer Use backend.",
                exit_code=1,
            )
        return self.computer_use

    def _computer_result(self, response: dict[str, Any]) -> dict[str, Any]:
        if not bool(response.get("ok")):
            raise SessionError(
                str(response.get("code") or "A0_TAG_FAILED"),
                str(response.get("error") or "A0 Tag operation failed."),
                exit_code=1,
            )
        result = response.get("result")
        return dict(result) if isinstance(result, dict) else {}

    def _emit_status(self, gateway: dict[str, Any]) -> None:
        self.writer.write(
            {
                "type": "status",
                "host": self.options.host,
                "workspace": str(self.options.workspace.expanduser().resolve()),
                "gateway": gateway,
            }
        )

    def _emit_error(self, code: str, message: str, *, fatal: bool, stage: str = "") -> None:
        payload = {"type": "error", "code": code, "message": message, "fatal": fatal}
        if stage:
            payload["stage"] = stage
        self.writer.write(payload)

    def _command_result(
        self,
        request_id: str,
        ok: bool,
        *,
        result: Any = None,
        error: str = "",
        code: str = "",
    ) -> None:
        payload: dict[str, Any] = {"type": "result", "request_id": request_id, "ok": ok}
        if ok:
            payload["result"] = result
        else:
            payload["error"] = error
            if code:
                payload["code"] = code
            if result is not None:
                payload["result"] = result
        self.writer.write(payload)

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.stop_event.set)
            except (NotImplementedError, RuntimeError):
                continue


def run_gateway(options: GatewayOptions, config: CLIConfig) -> int:
    return asyncio.run(GatewayRunner(options, config).run())


def gateway_options(
    *,
    host: str,
    workspace: str,
    gateway_id: str,
    host_label: str,
    master_enabled: bool,
    scopes: str,
    browser_selection: str,
) -> GatewayOptions:
    normalized_id = sanitize_gateway_id(gateway_id)
    if not normalized_id:
        raise ValueError("gateway id is required")
    return GatewayOptions(
        host=normalize_gateway_host(host),
        workspace=Path(workspace or "."),
        gateway_id=normalized_id,
        host_label=sanitize_host_label(host_label),
        master_enabled=bool(master_enabled),
        scopes=normalize_scopes(scopes),
        browser_selection=(
            str(browser_selection or "")
            .strip()
            .replace("\r", "")
            .replace("\n", "")
            .replace("\0", "")[:512]
        ),
    )
