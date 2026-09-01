from __future__ import annotations

import asyncio
import base64
import importlib
import json
import os
import sys
import tempfile
import types
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
_PNG_1X1_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/5wAAAABJRU5ErkJggg=="
)


def _write_png_fixture(tmp_path: Path, filename: str = "capture.png") -> Path:
    image_path = tmp_path / filename
    image_path.write_bytes(base64.b64decode(_PNG_1X1_BASE64))
    return image_path


def _resolve_plugin_root() -> Path:
    env_root = os.environ.get("A0_CONNECTOR_PLUGIN_ROOT", "").strip()
    if env_root:
        candidate = Path(env_root)
        if (candidate / "_a0_connector").exists():
            return candidate

    local_root = PROJECT_ROOT / "plugin"
    if (local_root / "_a0_connector").exists():
        return local_root

    sibling_root = PROJECT_ROOT.parent / "agent-zero" / "plugins"
    if (sibling_root / "_a0_connector").exists():
        return sibling_root

    return local_root


PLUGIN_ROOT = _resolve_plugin_root()

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent_zero_cli.remote_files import RemoteFileUtility


def _purge_modules() -> None:
    for name in list(sys.modules):
        if name in {"agent", "api"} or name.startswith(("agent.", "api.", "helpers", "plugins")):
            sys.modules.pop(name, None)


def _make_package(name: str, *, path: Path | None = None) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__path__ = [str(path)] if path is not None else []
    sys.modules[name] = module
    return module


@pytest.fixture(autouse=True)
def _reset_modules() -> None:
    _purge_modules()
    yield
    _purge_modules()


def _install_fake_helpers(
    *,
    auth_required: bool = False,
    code_execution_config: dict[str, object] | None = None,
    shared_ws_manager: object | None = None,
) -> None:
    plugins_pkg = _make_package("plugins", path=PLUGIN_ROOT)
    api_pkg = _make_package("api")
    _make_package("plugins._model_config")
    _make_package("plugins._model_config.helpers")
    _make_package("plugins._chat_compaction")
    _make_package("plugins._chat_compaction.helpers")

    helpers_pkg = _make_package("helpers")
    api_mod = types.ModuleType("helpers.api")
    git_mod = types.ModuleType("helpers.git")
    login_mod = types.ModuleType("helpers.login")
    plugins_mod = types.ModuleType("helpers.plugins")
    print_style_mod = types.ModuleType("helpers.print_style")
    chat_media_mod = types.ModuleType("helpers.chat_media")
    history_mod = types.ModuleType("helpers.history")
    media_artifacts_mod = types.ModuleType("helpers.media_artifacts")
    message_queue_mod = types.ModuleType("helpers.message_queue")
    persist_chat_mod = types.ModuleType("helpers.persist_chat")
    state_monitor_mod = types.ModuleType("helpers.state_monitor_integration")
    tool_mod = types.ModuleType("helpers.tool")
    tool_policy_mod = types.ModuleType("helpers.tool_policy")
    tool_policy_mod.resolve_tool = lambda *args, **kwargs: types.SimpleNamespace(allowed=True)
    ws_mod = types.ModuleType("helpers.ws")
    ws_manager_mod = types.ModuleType("helpers.ws_manager")

    class ApiHandler:
        def __init__(self, app=None, thread_lock=None) -> None:
            self.app = app
            self.thread_lock = thread_lock

    class Request:
        pass

    class Response:
        def __init__(self, response: str = "", status: int = 200, mimetype: str = "application/json") -> None:
            self.response = response
            self.status = status
            self.mimetype = mimetype

    class ToolResponse:
        def __init__(
            self,
            message: str = "",
            break_loop: bool = False,
            additional: dict[str, object] | None = None,
        ) -> None:
            self.message = message
            self.break_loop = break_loop
            self.additional = additional

    class PrintStyle:
        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs

        @staticmethod
        def error(*args, **kwargs) -> None:
            return None

        @staticmethod
        def debug(*args, **kwargs) -> None:
            return None

        def print(self, *args, **kwargs) -> None:
            del args, kwargs
            return None

        def stream(self, *args, **kwargs) -> None:
            del args, kwargs
            return None

    class Tool:
        def __init__(self, agent=None, args=None, method: str = "", name: str = "") -> None:
            self.agent = agent
            self.args = args or {}
            self.method = method
            self.name = name or self.__class__.__name__.lower()

    class WsHandler:
        def __init__(self, app=None, thread_lock=None) -> None:
            self.app = app
            self.thread_lock = thread_lock

        async def emit_to(self, sid: str, event: str, payload: dict, correlation_id: str | None = None) -> None:
            del sid, event, payload, correlation_id
            return None

    class WsResult(dict):
        @classmethod
        def error(
            cls,
            *,
            code: str,
            message: str,
            correlation_id: str | None = None,
        ) -> "WsResult":
            payload: dict[str, object] = {
                "ok": False,
                "error": {
                    "code": code,
                    "message": message,
                },
            }
            if correlation_id is not None:
                payload["correlationId"] = correlation_id
            return cls(payload)

    class ConnectionNotFoundError(Exception):
        pass

    class SharedWsManager:
        async def emit_to(self, namespace: str, sid: str, event: str, payload: dict, handler_id: str | None = None) -> None:
            del namespace, sid, event, payload, handler_id
            return None

    def raw_message(*, raw_content, preview=None):
        return {"raw_content": raw_content, "preview": preview}

    def _save_image_file(
        *,
        context_id: str,
        path: object,
        category: str,
        source: str,
        preferred_name: str | None = None,
        max_bytes: int | None = None,
    ) -> object:
        del context_id, category, source, preferred_name, max_bytes
        return types.SimpleNamespace(path=str(path), a0_path=str(path))

    def _save_image_base64(
        *,
        context_id: str,
        data: str,
        mime_type: str,
        category: str,
        source: str,
        preferred_name: str | None = None,
        max_bytes: int | None = None,
    ) -> object:
        del context_id, category, source, max_bytes
        filename = preferred_name or f"inline.{mime_type.removeprefix('image/') or 'png'}"
        return types.SimpleNamespace(
            path=filename,
            a0_path=f"data:{mime_type};base64,{data}",
        )

    def _safe_filename(
        value: str,
        *,
        default: str = "artifact.bin",
        default_extension: str = ".bin",
    ) -> str:
        source = str(value or "").strip() or default
        cleaned = "".join(
            char if char.isalnum() or char in {"-", "_", "."} else "_"
            for char in source
        ).strip("._")
        if not cleaned:
            cleaned = default
        if "." not in cleaned:
            extension = default_extension if default_extension.startswith(".") else f".{default_extension}"
            cleaned = f"{cleaned}{extension}"
        return cleaned

    def _ctx_get_data(context: object, key: str, default: object = None) -> object:
        getter = getattr(context, "get_data", None)
        if callable(getter):
            return getter(key) or default
        return getattr(context, "_data", {}).get(key, default)

    def _ctx_set_data(context: object, key: str, value: object) -> None:
        setter = getattr(context, "set_data", None)
        if callable(setter):
            setter(key, value)
            return
        getattr(context, "_data", {})[key] = value

    def _ctx_set_output_data(context: object, key: str, value: object) -> None:
        setter = getattr(context, "set_output_data", None)
        if callable(setter):
            setter(key, value)
            return
        getattr(context, "_output_data", {})[key] = value

    def _queue_get(context: object) -> list[dict[str, object]]:
        queue = _ctx_get_data(context, "message_queue", [])
        return queue if isinstance(queue, list) else []

    def _queue_sync(context: object) -> None:
        output_items = []
        for item in _queue_get(context):
            attachments = [str(path).split("/")[-1] for path in item.get("attachments", [])]
            text = str(item.get("text", "") or "")
            output_items.append(
                {
                    "id": item.get("id", ""),
                    "seq": item.get("seq", 0),
                    "text": text[:100] + "..." if len(text) > 100 else text,
                    "attachments": attachments,
                    "attachment_count": len(item.get("attachments", []) or []),
                }
            )
        _ctx_set_output_data(context, "message_queue", output_items)

    def _queue_add(
        context: object,
        text: str,
        attachments: list[str] | None = None,
        item_id: str | None = None,
    ) -> dict[str, object]:
        queue = list(_queue_get(context))
        seq = int(_ctx_get_data(context, "message_queue_seq", 0) or 0) + 1
        _ctx_set_data(context, "message_queue_seq", seq)
        item = {
            "id": item_id or f"item-{seq}",
            "seq": seq,
            "text": text,
            "attachments": list(attachments or []),
        }
        queue.append(item)
        _ctx_set_data(context, "message_queue", queue)
        _queue_sync(context)
        return item

    def _queue_remove(context: object, item_id: str | None = None) -> int:
        if not item_id:
            _ctx_set_data(context, "message_queue", [])
            _ctx_set_output_data(context, "message_queue", [])
            return 0
        queue = [item for item in _queue_get(context) if item.get("id") != item_id]
        _ctx_set_data(context, "message_queue", queue)
        _queue_sync(context)
        return len(queue)

    def _queue_pop_first(context: object) -> dict[str, object] | None:
        queue = list(_queue_get(context))
        if not queue:
            return None
        item = queue.pop(0)
        _ctx_set_data(context, "message_queue", queue)
        _queue_sync(context)
        return item

    def _queue_pop_item(context: object, item_id: str) -> dict[str, object] | None:
        queue = list(_queue_get(context))
        for index, item in enumerate(queue):
            if item.get("id") == item_id:
                queue.pop(index)
                _ctx_set_data(context, "message_queue", queue)
                _queue_sync(context)
                return item
        return None

    def _queue_send_message(context: object, item: dict[str, object], source: str = " (from queue)") -> None:
        del source
        sender = getattr(context, "communicate", None)
        if callable(sender):
            sender(item)

    def _queue_send_all_aggregated(context: object) -> int:
        count = 0
        while _queue_get(context):
            item = _queue_pop_first(context)
            if item is not None:
                _queue_send_message(context, item)
                count += 1
        return count

    api_mod.ApiHandler = ApiHandler
    api_mod.Request = Request
    api_mod.Response = Response
    chat_media_mod.save_image_file = _save_image_file
    chat_media_mod.save_image_base64 = _save_image_base64
    history_mod.RawMessage = raw_message
    media_artifacts_mod.estimated_base64_decoded_size = lambda data: (len(str(data or "")) * 3) // 4
    media_artifacts_mod.safe_filename = _safe_filename
    message_queue_mod.get_queue = _queue_get
    message_queue_mod.add = _queue_add
    message_queue_mod.remove = _queue_remove
    message_queue_mod.pop_first = _queue_pop_first
    message_queue_mod.pop_item = _queue_pop_item
    message_queue_mod.has_queue = lambda context: bool(_queue_get(context))
    message_queue_mod.send_message = _queue_send_message
    message_queue_mod.send_all_aggregated = _queue_send_all_aggregated
    persist_chat_mod.save_tmp_chat = lambda context: None
    state_monitor_mod.mark_dirty_for_context = lambda *args, **kwargs: None
    git_mod.get_version = lambda: "v1.18"
    login_mod.is_login_required = lambda: auth_required
    plugins_mod.get_plugin_config = lambda plugin_name, **kwargs: (
        code_execution_config if plugin_name == "_code_execution" else {}
    )
    print_style_mod.PrintStyle = PrintStyle
    tool_mod.Response = ToolResponse
    tool_mod.Tool = Tool
    ws_mod.NAMESPACE = "/ws"
    ws_mod.WsHandler = WsHandler
    ws_manager_mod.ConnectionNotFoundError = ConnectionNotFoundError
    ws_manager_mod.WsResult = WsResult
    ws_manager_mod.get_shared_ws_manager = lambda: (
        shared_ws_manager if shared_ws_manager is not None else SharedWsManager()
    )

    sys.modules["helpers.api"] = api_mod
    sys.modules["helpers.git"] = git_mod
    sys.modules["helpers.chat_media"] = chat_media_mod
    sys.modules["helpers.history"] = history_mod
    sys.modules["helpers.media_artifacts"] = media_artifacts_mod
    sys.modules["helpers.message_queue"] = message_queue_mod
    sys.modules["helpers.persist_chat"] = persist_chat_mod
    sys.modules["helpers.state_monitor_integration"] = state_monitor_mod
    sys.modules["helpers.login"] = login_mod
    sys.modules["helpers.plugins"] = plugins_mod
    sys.modules["helpers.print_style"] = print_style_mod
    sys.modules["helpers.tool"] = tool_mod
    sys.modules["helpers.tool_policy"] = tool_policy_mod
    sys.modules["helpers.ws"] = ws_mod
    sys.modules["helpers.ws_manager"] = ws_manager_mod
    helpers_pkg.git = git_mod

    for module_name in (
        "helpers.settings",
        "helpers.subagents",
        "helpers.skills",
        "helpers.files",
        "helpers.projects",
        "helpers.runtime",
        "api.agent_profile_set",
        "plugins._model_config.helpers.model_config",
        "plugins._chat_compaction.helpers.compactor",
    ):
        sys.modules[module_name] = types.ModuleType(module_name)

    files_mod = sys.modules["helpers.files"]
    fake_base_dir = Path(tempfile.gettempdir()) / "a0-connector-plugin-tests"
    files_mod.get_abs_path = lambda *parts: str(fake_base_dir.joinpath(*map(str, parts)))
    files_mod.normalize_a0_path = lambda path: str(path)

    projects_mod = sys.modules["helpers.projects"]
    projects_mod.get_context_project_name = lambda context: getattr(context, "project_name", "")
    projects_mod.get_project_meta = lambda project_name, *parts: str(
        fake_base_dir.joinpath("projects", str(project_name), ".a0proj", *map(str, parts))
    )

    def _write_file_base64(relative_path: str, content: str) -> None:
        target = Path(files_mod.get_abs_path(relative_path))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(base64.b64decode(content))

    files_mod.write_file_base64 = _write_file_base64

    helpers_pkg.api = api_mod
    helpers_pkg.chat_media = chat_media_mod
    helpers_pkg.files = files_mod
    helpers_pkg.history = history_mod
    helpers_pkg.media_artifacts = media_artifacts_mod
    helpers_pkg.message_queue = message_queue_mod
    helpers_pkg.persist_chat = persist_chat_mod
    helpers_pkg.state_monitor_integration = state_monitor_mod
    helpers_pkg.login = login_mod
    helpers_pkg.plugins = plugins_mod
    helpers_pkg.print_style = print_style_mod
    helpers_pkg.tool = tool_mod
    helpers_pkg.tool_policy = tool_policy_mod
    helpers_pkg.ws = ws_mod
    helpers_pkg.ws_manager = ws_manager_mod

    plugins_pkg._model_config = sys.modules["plugins._model_config"]
    plugins_pkg._chat_compaction = sys.modules["plugins._chat_compaction"]
    api_pkg.agent_profile_set = sys.modules["api.agent_profile_set"]


def _reload(module_name: str):
    sys.modules.pop(module_name, None)
    return importlib.import_module(module_name)


def _reset_ws_runtime_state(ws_runtime_mod) -> None:
    with ws_runtime_mod._state_lock:
        ws_runtime_mod._context_subscriptions.clear()
        ws_runtime_mod._sid_contexts.clear()
        ws_runtime_mod._pending_file_ops.clear()
        ws_runtime_mod._pending_exec_ops.clear()
        ws_runtime_mod._pending_computer_use_ops.clear()
        ws_runtime_mod._remote_tree_snapshots.clear()
        ws_runtime_mod._sid_computer_use_metadata.clear()
        ws_runtime_mod._sid_remote_file_metadata.clear()
        ws_runtime_mod._sid_remote_exec_metadata.clear()


class _FakeCliWsManager:
    def __init__(self, *, file_op_handler) -> None:
        self.file_op_handler = file_op_handler
        self.ws_runtime_mod = None
        self.calls: list[dict[str, object]] = []

    async def emit_to(
        self,
        namespace: str,
        sid: str,
        event: str,
        payload: dict,
        handler_id: str | None = None,
    ) -> None:
        del namespace, event, handler_id
        self.calls.append({"sid": sid, "payload": dict(payload)})

        result = self.file_op_handler(dict(payload))
        if asyncio.iscoroutine(result):
            result = await result

        assert self.ws_runtime_mod is not None
        self.ws_runtime_mod.resolve_pending_file_op(
            payload["op_id"],
            sid=sid,
            payload=result,
        )

    @property
    def ops(self) -> list[str]:
        return [
            str(call["payload"].get("op"))
            for call in self.calls
            if isinstance(call.get("payload"), dict)
        ]


class _FakeExecWsManager:
    def __init__(self, *, exec_handler) -> None:
        self.exec_handler = exec_handler
        self.ws_runtime_mod = None
        self.calls: list[dict[str, object]] = []

    async def emit_to(
        self,
        namespace: str,
        sid: str,
        event: str,
        payload: dict,
        handler_id: str | None = None,
    ) -> None:
        del namespace, event, handler_id
        self.calls.append({"sid": sid, "payload": dict(payload)})

        result = self.exec_handler(dict(payload))
        if asyncio.iscoroutine(result):
            result = await result

        assert self.ws_runtime_mod is not None
        self.ws_runtime_mod.resolve_pending_exec_op(
            payload["op_id"],
            sid=sid,
            payload=result,
        )


class _FakeComputerUseWsManager:
    def __init__(self, *, computer_use_handler) -> None:
        self.computer_use_handler = computer_use_handler
        self.ws_runtime_mod = None
        self.calls: list[dict[str, object]] = []

    async def emit_to(
        self,
        namespace: str,
        sid: str,
        event: str,
        payload: dict,
        handler_id: str | None = None,
    ) -> None:
        del namespace, event, handler_id
        self.calls.append({"sid": sid, "payload": dict(payload)})

        result = self.computer_use_handler(dict(payload))
        if asyncio.iscoroutine(result):
            result = await result

        assert self.ws_runtime_mod is not None
        self.ws_runtime_mod.resolve_pending_computer_use_op(
            payload["op_id"],
            sid=sid,
            payload=result,
        )


class _FakeRemoteAgent:
    def __init__(self, *, context_id: str = "ctx-1") -> None:
        self.context = types.SimpleNamespace(id=context_id)
        self.data: dict[str, object] = {}
        self.history_messages: list[dict[str, object]] = []
        self.tool_results: list[dict[str, object]] = []

    def read_prompt(self, file: str, **kwargs) -> str:
        path = kwargs.get("path", "")
        return f"{file}::{path}"

    def hist_add_message(self, ai: bool, content: object, tokens: int = 0, id: str = "") -> dict[str, object]:
        payload = {"ai": ai, "content": content, "tokens": tokens, "id": id}
        self.history_messages.append(payload)
        return payload

    def hist_add_tool_result(self, tool_name: str, tool_result: str, **kwargs) -> dict[str, object]:
        payload = {"tool_name": tool_name, "tool_result": tool_result, **kwargs}
        self.tool_results.append(payload)
        return payload


def _load_text_editor_remote_tool(*, file_op_handler):
    shared_ws_manager = _FakeCliWsManager(file_op_handler=file_op_handler)
    _install_fake_helpers(shared_ws_manager=shared_ws_manager)
    ws_runtime_mod = _reload("plugins._a0_connector.helpers.ws_runtime")
    _reset_ws_runtime_state(ws_runtime_mod)
    shared_ws_manager.ws_runtime_mod = ws_runtime_mod
    tool_mod = _reload("plugins._a0_connector.tools.text_editor_remote")
    return shared_ws_manager, ws_runtime_mod, tool_mod


def _load_code_execution_remote_tool(
    *,
    exec_handler,
    code_execution_config: dict[str, object] | None = None,
):
    shared_ws_manager = _FakeExecWsManager(exec_handler=exec_handler)
    _install_fake_helpers(
        code_execution_config=code_execution_config,
        shared_ws_manager=shared_ws_manager,
    )
    ws_runtime_mod = _reload("plugins._a0_connector.helpers.ws_runtime")
    _reset_ws_runtime_state(ws_runtime_mod)
    shared_ws_manager.ws_runtime_mod = ws_runtime_mod
    tool_mod = _reload("plugins._a0_connector.tools.code_execution_remote")
    return shared_ws_manager, ws_runtime_mod, tool_mod


def _load_computer_use_remote_tool(*, computer_use_handler):
    shared_ws_manager = _FakeComputerUseWsManager(computer_use_handler=computer_use_handler)
    _install_fake_helpers(shared_ws_manager=shared_ws_manager)
    ws_runtime_mod = _reload("plugins._a0_connector.helpers.ws_runtime")
    _reset_ws_runtime_state(ws_runtime_mod)
    shared_ws_manager.ws_runtime_mod = ws_runtime_mod
    tool_mod = _reload("plugins._a0_connector.tools.computer_use_remote")
    return shared_ws_manager, ws_runtime_mod, tool_mod


def _create_text_editor_remote(
    tool_mod,
    agent: _FakeRemoteAgent,
    **args,
):
    return tool_mod.TextEditorRemote(agent=agent, args=args)


def _create_code_execution_remote(
    tool_mod,
    agent: _FakeRemoteAgent,
    **args,
):
    return tool_mod.CodeExecutionRemote(agent=agent, args=args)


def _create_computer_use_remote(
    tool_mod,
    agent: _FakeRemoteAgent,
    **args,
):
    return tool_mod.ComputerUseRemote(agent=agent, args=args)


def _register_remote_file_cli(
    ws_runtime_mod,
    sid: str,
    context_id: str,
    *,
    write_enabled: bool = True,
) -> None:
    ws_runtime_mod.register_sid(sid)
    ws_runtime_mod.subscribe_sid_to_context(sid, context_id)
    ws_runtime_mod.store_sid_remote_file_metadata(
        sid,
        {
            "enabled": True,
            "write_enabled": write_enabled,
            "mode": "read_write" if write_enabled else "read_only",
        },
    )


def test_ws_runtime_reassembles_chunked_file_op_results() -> None:
    _install_fake_helpers()
    ws_runtime_mod = _reload("plugins._a0_connector.helpers.ws_runtime")
    _reset_ws_runtime_state(ws_runtime_mod)

    async def run_scenario() -> None:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, object]] = loop.create_future()
        result = {
            "op_id": "op-large",
            "ok": True,
            "result": {
                "content": "0123456789abcdef\n" * 5000,
                "total_lines": 5000,
            },
        }
        raw = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        chunks = [raw[index : index + 4096] for index in range(0, len(raw), 4096)]
        ws_runtime_mod.store_pending_file_op(
            "op-large",
            sid="sid-cli",
            future=future,
            loop=loop,
            context_id="ctx-1",
        )

        for chunk_index in [1, 0, *range(2, len(chunks))]:
            accepted = ws_runtime_mod.resolve_pending_file_op(
                "op-large",
                sid="sid-cli",
                payload={
                    "op_id": "op-large",
                    "chunked": True,
                    "chunk_index": chunk_index,
                    "chunk_count": len(chunks),
                    "encoding": "json+base64",
                    "data": base64.b64encode(chunks[chunk_index]).decode("ascii"),
                },
            )
            assert accepted is True
            if chunk_index != len(chunks) - 1:
                assert not future.done()

        assert await asyncio.wait_for(future, timeout=1.0) == result

    asyncio.run(run_scenario())


async def _no_sleep(_delay: float) -> None:
    return None


def _assert_fresh_auto_capture(tool_mod, payload: dict[str, object]) -> None:
    assert payload["action"] == "capture"
    assert payload["fresh"] is True
    assert payload["fresh_timeout_seconds"] == tool_mod._FRESH_CAPTURE_TIMEOUT


def _expected_capture_summary(
    capture_id: str = "capture",
    *,
    fresh: bool = False,
    fresh_confirmed: bool = True,
) -> str:
    summary = (
        f"Computer-use capture id={capture_id} 1x1, "
        "coordinates=normalized_global_screen [0,1]."
    )
    if fresh:
        state = "confirmed" if fresh_confirmed else "not confirmed"
        summary = f"{summary} Fresh frame {state}."
    return summary


def _capture_verification_note(tool_mod) -> str:
    return tool_mod.CAPTURE_VERIFICATION_NOTE


def _assert_capture_response_content(
    response: object,
    *,
    message: str,
    preview: str,
    image_url: str,
) -> None:
    additional = getattr(response, "additional", None)
    assert isinstance(additional, dict)
    assert additional.get("preview") == preview
    assert additional.get("_tokens") == 1500
    raw_content = additional.get("raw_content")
    assert isinstance(raw_content, list)
    assert raw_content[0]["type"] == "text"
    assert raw_content[0]["text"] == message
    assert raw_content[1]["type"] == "image_url"
    assert raw_content[1]["image_url"]["url"] == image_url


def _assert_capture_history_message(
    agent: _FakeRemoteAgent,
    *,
    message: str,
    preview: str,
    image_url: str,
) -> None:
    assert len(agent.tool_results) == 1
    tool_result = agent.tool_results[0]
    assert tool_result["tool_name"] == "computer_use_remote"
    assert tool_result["tool_result"] == message
    assert "raw_content" not in tool_result
    assert "preview" not in tool_result

    assert len(agent.history_messages) == 1
    history_message = agent.history_messages[0]
    assert history_message["ai"] is False
    assert history_message["tokens"] == 1500
    content = history_message["content"]
    assert isinstance(content, dict)
    assert content["preview"] == preview
    raw_content = content["raw_content"]
    assert raw_content[0]["type"] == "text"
    assert raw_content[0]["text"] == message
    assert raw_content[1]["type"] == "image_url"
    assert raw_content[1]["image_url"]["url"] == image_url


def test_capabilities_advertise_current_ws_contract() -> None:
    _install_fake_helpers()
    _reload("plugins._a0_connector.api.v1.base")
    capabilities_mod = _reload("plugins._a0_connector.api.v1.capabilities")

    payload = asyncio.run(capabilities_mod.Capabilities(None, None).process({}, object()))

    assert payload["protocol"] == "a0-connector.v1"
    assert payload["agent_zero_version"] == "v1.18"
    assert payload["auth"] == ["session"]
    assert payload["auth_required"] is False
    assert payload["websocket_namespace"] == "/ws"
    assert payload["websocket_handlers"] == ["plugins/_a0_connector/ws_connector"]
    assert {
        "pause",
        "nudge",
        "message_queue",
        "remote_file_tree",
        "code_execution_remote",
        "computer_use_remote",
    } <= set(payload["features"])
    assert {
        "settings_get",
        "settings_set",
        "agent_profile_set",
        "agents_list",
        "skills_list",
        "skills_activate",
        "skills_delete",
        "installed_plugins",
        "model_switcher",
        "browser_runtime_config",
        "compact_chat",
    } <= set(payload["features"])


class _FakePluginItem:
    def __init__(self, **data: object) -> None:
        self._data = data

    def model_dump(self, mode: str = "json") -> dict[str, object]:
        del mode
        return dict(self._data)


def test_installed_plugins_endpoint_lists_installed_only_metadata() -> None:
    _install_fake_helpers()
    plugins_mod = sys.modules["helpers.plugins"]
    plugins_mod.get_enhanced_plugins_list = lambda custom=True, builtin=True: [
        _FakePluginItem(
            name="_documents",
            display_name="Documents",
            description="Create documents",
            version="1.0",
            is_custom=False,
            toggle_state="enabled",
        ),
        _FakePluginItem(
            name="todo_list",
            display_name="Todo List",
            description="Manage todos",
            version="0.2",
            is_custom=True,
            toggle_state="disabled",
        ),
    ]

    _reload("plugins._a0_connector.api.v1.base")
    installed_mod = _reload("plugins._a0_connector.api.v1.installed_plugins")

    payload = asyncio.run(installed_mod.InstalledPlugins(None, None).process({"action": "list"}, object()))

    assert payload["ok"] is True
    assert payload["installed_count"] == 2
    assert payload["enabled_count"] == 1
    assert payload["plugins"][0]["name"] == "_documents"
    assert payload["plugins"][0]["source"] == "builtin"
    assert payload["plugins"][0]["toggleable"] is True
    assert payload["plugins"][1]["source"] == "custom"


def test_installed_plugins_endpoint_toggles_installed_plugin() -> None:
    _install_fake_helpers()
    plugins_mod = sys.modules["helpers.plugins"]
    enabled_state = {"_browser": False}
    calls: list[tuple[str, bool, str, str, bool]] = []

    def fake_plugins_list(custom=True, builtin=True):
        del custom, builtin
        return [
            _FakePluginItem(
                name="_browser",
                display_name="Browser",
                toggle_state="enabled" if enabled_state["_browser"] else "disabled",
            )
        ]

    def fake_toggle_plugin(
        plugin_name: str,
        enabled: bool,
        project_name: str = "",
        agent_profile: str = "",
        clear_overrides: bool = False,
    ) -> None:
        calls.append((plugin_name, enabled, project_name, agent_profile, clear_overrides))
        enabled_state[plugin_name] = enabled

    plugins_mod.get_enhanced_plugins_list = fake_plugins_list
    plugins_mod.toggle_plugin = fake_toggle_plugin

    _reload("plugins._a0_connector.api.v1.base")
    installed_mod = _reload("plugins._a0_connector.api.v1.installed_plugins")

    payload = asyncio.run(
        installed_mod.InstalledPlugins(None, None).process(
            {"action": "set_enabled", "plugin_name": "_browser", "enabled": True},
            object(),
        )
    )

    assert payload["ok"] is True
    assert payload["plugin"]["enabled"] is True
    assert calls == [("_browser", True, "", "", False)]


def test_installed_plugins_endpoint_rejects_unknown_plugin() -> None:
    _install_fake_helpers()
    plugins_mod = sys.modules["helpers.plugins"]
    plugins_mod.get_enhanced_plugins_list = lambda custom=True, builtin=True: []

    _reload("plugins._a0_connector.api.v1.base")
    installed_mod = _reload("plugins._a0_connector.api.v1.installed_plugins")

    response = asyncio.run(
        installed_mod.InstalledPlugins(None, None).process(
            {"action": "set_enabled", "plugin_name": "missing", "enabled": True},
            object(),
        )
    )

    assert response.status == 404
    assert response.response == "Plugin not found"


def test_installed_plugins_endpoint_rejects_protected_plugins() -> None:
    _install_fake_helpers()
    plugins_mod = sys.modules["helpers.plugins"]
    calls: list[tuple[str, bool]] = []
    plugins_mod.get_enhanced_plugins_list = lambda custom=True, builtin=True: [
        _FakePluginItem(
            name="_a0_connector",
            display_name="A0 Connector",
            toggle_state="enabled",
        ),
        _FakePluginItem(
            name="_plugin_installer",
            display_name="Plugin Installer",
            always_enabled=True,
            toggle_state="enabled",
        ),
    ]
    plugins_mod.toggle_plugin = lambda plugin_name, enabled, **kwargs: calls.append((plugin_name, enabled))

    _reload("plugins._a0_connector.api.v1.base")
    installed_mod = _reload("plugins._a0_connector.api.v1.installed_plugins")

    connector_response = asyncio.run(
        installed_mod.InstalledPlugins(None, None).process(
            {"action": "set_enabled", "plugin_name": "_a0_connector", "enabled": False},
            object(),
        )
    )
    installer_response = asyncio.run(
        installed_mod.InstalledPlugins(None, None).process(
            {"action": "set_enabled", "plugin_name": "_plugin_installer", "enabled": False},
            object(),
        )
    )

    assert connector_response.status == 400
    assert "keeps this CLI session connected" in connector_response.response
    assert installer_response.status == 400
    assert "always enabled" in installer_response.response
    assert calls == []


def test_browser_runtime_endpoint_updates_browser_plugin_config() -> None:
    _install_fake_helpers()
    plugins_mod = sys.modules["helpers.plugins"]
    saved: list[tuple[str, str, str, dict[str, object]]] = []

    plugins_mod.get_plugin_config = lambda plugin_name, **kwargs: {
        "extension_paths": ["/tmp/ext"],
        "default_homepage": "https://example.com",
        "autofocus_active_page": False,
        "runtime_backend": "container",
        "host_browser_privacy_policy": "warn",
        "host_browser_profile_mode": "existing",
        "model_preset": "Research",
    }
    plugins_mod.save_plugin_config = (
        lambda plugin_name, project_name, agent_profile, settings: saved.append(
            (plugin_name, project_name, agent_profile, dict(settings))
        )
    )
    _reload("plugins._a0_connector.api.v1.base")
    browser_runtime_mod = _reload("plugins._a0_connector.api.v1.browser_runtime")

    payload = asyncio.run(
        browser_runtime_mod.BrowserRuntime(None, None).process(
            {"action": "set", "runtime_backend": "host"},
            object(),
        )
    )

    assert payload["ok"] is True
    assert payload["runtime_backend"] == "host_required"
    assert payload["host_browser_profile_mode"] == "existing"
    assert "Browser model-use settings" in payload["privacy_notice"]
    assert saved == [
        (
            "_browser",
            "",
            "",
            {
                "extension_paths": [os.path.normpath("/tmp/ext")],
                "default_homepage": "https://example.com",
                "autofocus_active_page": False,
                "browser_tab_scope": "per_context",
                "max_open_tabs": 32,
                "runtime_backend": "host_required",
                "host_browser_privacy_policy": "warn",
                "host_browser_profile_mode": "existing",
                "host_browser_selection": "",
                "proxy_server": "",
                "proxy_bypass": "",
                "proxy_username": "",
                "proxy_password": "",
                "keyboard_layout": "",
                "keyboard_variant": "",
                "model_preset": "Research",
            },
        )
    ]


def test_browser_runtime_endpoint_defaults_missing_profile_mode() -> None:
    _install_fake_helpers()
    plugins_mod = sys.modules["helpers.plugins"]

    plugins_mod.get_plugin_config = lambda plugin_name, **kwargs: {
        "extension_paths": ["/tmp/ext"],
        "default_homepage": "https://example.com",
        "autofocus_active_page": False,
        "runtime_backend": "host_required",
        "host_browser_privacy_policy": "warn",
        "model_preset": "Research",
    }
    _reload("plugins._a0_connector.api.v1.base")
    browser_runtime_mod = _reload("plugins._a0_connector.api.v1.browser_runtime")

    payload = asyncio.run(
        browser_runtime_mod.BrowserRuntime(None, None).process(
            {"action": "get"},
            object(),
        )
    )

    assert payload["ok"] is True
    assert payload["runtime_backend"] == "host_required"
    assert payload["host_browser_profile_mode"] == "existing"
    assert payload["host_browser_selection"] == ""


def test_capabilities_reflect_core_login_requirement() -> None:
    _install_fake_helpers(auth_required=True)
    _reload("plugins._a0_connector.api.v1.base")
    capabilities_mod = _reload("plugins._a0_connector.api.v1.capabilities")

    payload = asyncio.run(capabilities_mod.Capabilities(None, None).process({}, object()))

    assert payload["auth_required"] is True


def test_skills_activate_endpoint_activates_chat_skill() -> None:
    _install_fake_helpers()

    skills_mod = sys.modules["helpers.skills"]
    persist_chat_mod = sys.modules["helpers.persist_chat"]
    activated: list[tuple[object, dict[str, object]]] = []
    saved: list[str] = []

    skills_mod.normalize_active_skills = lambda raw: [
        {"name": raw[0]["name"], "path": raw[0]["path"]}
    ]
    skills_mod.activate_chat_skill = lambda agent, entry: activated.append((agent, dict(entry))) or [dict(entry)]
    persist_chat_mod.save_tmp_chat = lambda context: saved.append(context.id)

    class FakeContext:
        id = "ctx-1"
        agent = object()

        def get_agent(self) -> object:
            return self.agent

    context = FakeContext()
    agent_mod = types.ModuleType("agent")
    agent_mod.AgentContext = types.SimpleNamespace(get=lambda context_id: context if context_id == context.id else None)
    sys.modules["agent"] = agent_mod

    _reload("plugins._a0_connector.api.v1.base")
    skills_activate_mod = _reload("plugins._a0_connector.api.v1.skills_activate")

    payload = asyncio.run(
        skills_activate_mod.SkillsActivate(None, None).process(
            {
                "context_id": "ctx-1",
                "skill": {
                    "name": "a0-live-e2e-tester",
                    "path": "/a0/skills/a0-live-e2e-tester",
                },
            },
            object(),
        )
    )

    assert payload["ok"] is True
    assert payload["skill"] == {
        "name": "a0-live-e2e-tester",
        "path": "/a0/skills/a0-live-e2e-tester",
    }
    assert activated == [
        (
            context.agent,
            {
                "name": "a0-live-e2e-tester",
                "path": "/a0/skills/a0-live-e2e-tester",
            },
        )
    ]
    assert saved == ["ctx-1"]


def test_event_bridge_uses_log_output_cursor() -> None:
    _install_fake_helpers()

    class FakeLog:
        def output(self, start=None, end=None):
            del end
            assert start == 5
            return types.SimpleNamespace(
                items=[
                    {
                        "no": 2,
                        "type": "response",
                        "heading": "Assistant",
                        "content": "Hello",
                        "kvps": {"source": "test"},
                        "timestamp": "2026-04-01T00:00:00Z",
                    }
                ],
                end=7,
            )

    class FakeContext:
        log = FakeLog()

    agent_mod = types.ModuleType("agent")
    agent_mod.AgentContext = types.SimpleNamespace(get=lambda context_id: FakeContext())
    sys.modules["agent"] = agent_mod

    bridge_mod = _reload("plugins._a0_connector.helpers.event_bridge")
    events, cursor = bridge_mod.get_context_log_entries("ctx-1", after=5)

    assert cursor == 7
    assert events == [
        {
            "context_id": "ctx-1",
            "sequence": 3,
            "event": "assistant_message",
            "timestamp": "2026-04-01T00:00:00Z",
            "data": {
                "text": "Hello",
                "heading": "Assistant",
                "meta": {"source": "test"},
            },
        }
    ]


def test_event_bridge_limits_log_output_without_skipping_future_cursor() -> None:
    _install_fake_helpers()

    class FakeLog:
        def output(self, start=None, end=None):
            assert start == 10
            assert end == 13
            return types.SimpleNamespace(
                items=[
                    {
                        "no": no,
                        "type": "response",
                        "heading": "Assistant",
                        "content": f"chunk {no}",
                        "kvps": {},
                        "timestamp": "2026-04-01T00:00:00Z",
                    }
                    for no in range(start, end)
                ],
                end=end,
            )

    class FakeContext:
        log = FakeLog()

    agent_mod = types.ModuleType("agent")
    agent_mod.AgentContext = types.SimpleNamespace(get=lambda context_id: FakeContext())
    sys.modules["agent"] = agent_mod

    bridge_mod = _reload("plugins._a0_connector.helpers.event_bridge")
    events, cursor = bridge_mod.get_context_log_entries("ctx-1", after=10, limit=3)

    assert cursor == 13
    assert [event["sequence"] for event in events] == [11, 12, 13]


def test_ws_connector_replays_large_history_in_snapshot_pages() -> None:
    _install_fake_helpers()
    ws_runtime_mod = _reload("plugins._a0_connector.helpers.ws_runtime")
    _reset_ws_runtime_state(ws_runtime_mod)
    ws_connector_mod = _reload("plugins._a0_connector.api.ws_connector")

    class FakeLog:
        updates = list(range(120))

        def output(self, start=None, end=None):
            normalized_start = int(start or 0)
            normalized_end = (
                len(self.updates)
                if end is None
                else min(int(end), len(self.updates))
            )
            return types.SimpleNamespace(
                items=[
                    {
                        "no": no,
                        "type": "response",
                        "heading": "Assistant",
                        "content": f"history chunk {no}",
                        "kvps": {},
                        "timestamp": "2026-04-01T00:00:00Z",
                    }
                    for no in range(normalized_start, normalized_end)
                ],
                end=normalized_end,
            )

    class FakeContext:
        id = "ctx-long"
        log = FakeLog()
        agent0 = types.SimpleNamespace(config=types.SimpleNamespace(profile="agent0"))

        def is_running(self) -> bool:
            return False

        def get_data(self, key: str) -> object:
            del key
            return None

    context = FakeContext()
    agent_mod = types.ModuleType("agent")
    agent_mod.AgentContext = types.SimpleNamespace(
        get=lambda context_id: context if context_id == context.id else None
    )
    sys.modules["agent"] = agent_mod

    class CapturingConnector(ws_connector_mod.WsConnector):
        def __init__(self) -> None:
            super().__init__(None, None)
            self.emitted: list[tuple[str, str, dict[str, object]]] = []

        async def emit_to(
            self,
            sid: str,
            event: str,
            payload: dict,
            correlation_id: str | None = None,
        ) -> None:
            del correlation_id
            self.emitted.append((sid, event, dict(payload)))

    async def _scenario() -> None:
        ws_runtime_mod.register_sid("sid-cli")
        handler = CapturingConnector()

        result = await handler.process(
            "connector_subscribe_context",
            {"context_id": context.id},
            "sid-cli",
        )

        for _ in range(20):
            snapshot_count = sum(
                1
                for _, event, _ in handler.emitted
                if event == "connector_context_snapshot"
            )
            if snapshot_count >= 3:
                break
            await asyncio.sleep(0)

        handler._cancel_streaming("sid-cli", context.id)
        await asyncio.sleep(0)

        snapshots = [
            payload
            for _, event, payload in handler.emitted
            if event == "connector_context_snapshot"
        ]
        assert result["last_sequence"] == 50
        assert [len(snapshot["events"]) for snapshot in snapshots] == [50, 50, 20]
        assert [snapshot["last_sequence"] for snapshot in snapshots] == [50, 100, 120]
        assert not [
            event
            for _, event, _ in handler.emitted
            if event == "connector_context_event"
        ]

    asyncio.run(_scenario())


def test_ws_connector_loads_tail_history_and_older_pages_on_demand() -> None:
    _install_fake_helpers()
    ws_runtime_mod = _reload("plugins._a0_connector.helpers.ws_runtime")
    _reset_ws_runtime_state(ws_runtime_mod)
    ws_connector_mod = _reload("plugins._a0_connector.api.ws_connector")

    class FakeLog:
        updates = list(range(250))

        def output(self, start=None, end=None):
            normalized_start = int(start or 0)
            normalized_end = (
                len(self.updates)
                if end is None
                else min(int(end), len(self.updates))
            )
            return types.SimpleNamespace(
                items=[
                    {
                        "no": no,
                        "type": "response",
                        "heading": "Assistant",
                        "content": f"history chunk {no}",
                        "kvps": {},
                        "timestamp": "2026-04-01T00:00:00Z",
                    }
                    for no in range(normalized_start, normalized_end)
                ],
                end=normalized_end,
            )

    class FakeContext:
        id = "ctx-long"
        log = FakeLog()
        agent0 = types.SimpleNamespace(config=types.SimpleNamespace(profile="agent0"))

        def is_running(self) -> bool:
            return False

        def get_data(self, key: str) -> object:
            del key
            return None

    context = FakeContext()
    agent_mod = types.ModuleType("agent")
    agent_mod.AgentContext = types.SimpleNamespace(
        get=lambda context_id: context if context_id == context.id else None
    )
    sys.modules["agent"] = agent_mod

    class CapturingConnector(ws_connector_mod.WsConnector):
        def __init__(self) -> None:
            super().__init__(None, None)
            self.emitted: list[tuple[str, str, dict[str, object]]] = []

        async def emit_to(
            self,
            sid: str,
            event: str,
            payload: dict,
            correlation_id: str | None = None,
        ) -> None:
            del correlation_id
            self.emitted.append((sid, event, dict(payload)))

    async def _scenario() -> None:
        ws_runtime_mod.register_sid("sid-cli")
        handler = CapturingConnector()

        result = await handler.process(
            "connector_subscribe_context",
            {"context_id": context.id, "history": "tail"},
            "sid-cli",
        )
        assert result["last_sequence"] == 250
        assert result["history_before"] == 150
        assert result["has_more_history"] is True

        snapshots = [
            payload
            for _, event, payload in handler.emitted
            if event == "connector_context_snapshot"
        ]
        assert [len(snapshot["events"]) for snapshot in snapshots] == [100]
        assert snapshots[0]["history_before"] == 150
        assert snapshots[0]["has_more_history"] is True

        second = await handler.process(
            "connector_subscribe_context",
            {"context_id": context.id, "history_before": 150},
            "sid-cli",
        )
        assert second["history_before"] == 50
        assert second["has_more_history"] is True

        third = await handler.process(
            "connector_subscribe_context",
            {"context_id": context.id, "history_before": 50},
            "sid-cli",
        )
        assert third["history_before"] == 0
        assert third["has_more_history"] is False

        snapshots = [
            payload
            for _, event, payload in handler.emitted
            if event == "connector_context_snapshot"
        ]
        assert [len(snapshot["events"]) for snapshot in snapshots] == [100, 100, 50]
        assert [snapshot["history_before"] for snapshot in snapshots] == [150, 50, 0]

        handler._cancel_streaming("sid-cli", context.id)
        await asyncio.sleep(0)

    asyncio.run(_scenario())


def test_ws_connector_hello_advertises_remote_exec_and_tree_features() -> None:
    _install_fake_helpers(
        code_execution_config={
            "code_exec_first_output_timeout": 12,
            "code_exec_between_output_timeout": 8,
            "code_exec_max_exec_timeout": 60,
            "code_exec_dialog_timeout": 2,
            "output_first_output_timeout": 24,
            "output_between_output_timeout": 12,
            "output_max_exec_timeout": 120,
            "output_dialog_timeout": 3,
            "prompt_patterns": "PS .+> ?$",
            "dialog_patterns": "yes/no",
        }
    )
    ws_connector_mod = _reload("plugins._a0_connector.api.ws_connector")

    payload = asyncio.run(ws_connector_mod.WsConnector(None, None).process("connector_hello", {}, "sid-1"))

    assert payload["protocol"] == "a0-connector.v1"
    assert payload["agent_zero_version"] == "v1.18"
    assert "remote_file_tree" in payload["features"]
    assert "message_queue" in payload["features"]
    assert "code_execution_remote" in payload["features"]
    assert "computer_use_remote" in payload["features"]
    assert payload["exec_config"]["version"] == 1
    assert payload["exec_config"]["code_exec_timeouts"]["first_output_timeout"] == 12
    assert payload["exec_config"]["output_timeouts"]["max_exec_timeout"] == 120
    assert payload["exec_config"]["prompt_patterns"] == ["PS .+> ?$"]
    assert payload["exec_config"]["dialog_patterns"] == ["yes/no"]


def test_plugin_root_resolution_prefers_a0_connector_plugin_root_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugins_root = tmp_path / "plugins"
    (plugins_root / "_a0_connector").mkdir(parents=True)
    monkeypatch.setenv("A0_CONNECTOR_PLUGIN_ROOT", str(plugins_root))

    assert _resolve_plugin_root() == plugins_root


def test_ws_connector_normalizes_attachment_refs_without_base64_payloads() -> None:
    _install_fake_helpers()
    ws_connector_mod = _reload("plugins._a0_connector.api.ws_connector")
    handler = ws_connector_mod.WsConnector(None, None)

    refs, error = handler._normalize_attachment_refs(
        [
            "/a0/usr/uploads/chart.png",
            {"path": "/a0/usr/uploads/diagram.png"},
            {"url": "https://example.test/photo.png"},
        ]
    )

    assert error == ""
    assert refs == [
        "/a0/usr/uploads/chart.png",
        "/a0/usr/uploads/diagram.png",
        "https://example.test/photo.png",
    ]


def test_ws_connector_rejects_base64_attachment_refs() -> None:
    _install_fake_helpers()
    ws_connector_mod = _reload("plugins._a0_connector.api.ws_connector")
    handler = ws_connector_mod.WsConnector(None, None)

    refs, error = handler._normalize_attachment_refs(
        [{"filename": "chart.png", "base64": _PNG_1X1_BASE64}]
    )

    assert refs == []
    assert "file paths or URLs" in error


def test_ws_connector_queue_add_uses_core_message_queue() -> None:
    _install_fake_helpers()
    ws_runtime_mod = _reload("plugins._a0_connector.helpers.ws_runtime")
    _reset_ws_runtime_state(ws_runtime_mod)
    ws_connector_mod = _reload("plugins._a0_connector.api.ws_connector")

    class FakeContext:
        def __init__(self) -> None:
            self.id = "ctx-queue"
            self._data: dict[str, object] = {}
            self._output_data: dict[str, object] = {}

        def get_data(self, key: str) -> object:
            return self._data.get(key)

        def set_data(self, key: str, value: object) -> None:
            self._data[key] = value

        def set_output_data(self, key: str, value: object) -> None:
            self._output_data[key] = value

        def is_running(self) -> bool:
            return False

    context = FakeContext()
    agent_mod = types.ModuleType("agent")
    agent_mod.AgentContext = types.SimpleNamespace(get=lambda context_id: context if context_id == context.id else None)
    sys.modules["agent"] = agent_mod

    class CapturingConnector(ws_connector_mod.WsConnector):
        def __init__(self) -> None:
            super().__init__(None, None)
            self.emitted: list[tuple[str, str, dict[str, object]]] = []

        async def emit_to(
            self,
            sid: str,
            event: str,
            payload: dict,
            correlation_id: str | None = None,
        ) -> None:
            del correlation_id
            self.emitted.append((sid, event, dict(payload)))

    async def _scenario() -> None:
        ws_runtime_mod.register_sid("sid-cli")
        ws_runtime_mod.subscribe_sid_to_context("sid-cli", context.id)
        handler = CapturingConnector()

        result = await handler.process(
            "connector_message_queue_add",
            {
                "context_id": context.id,
                "message": "queued from cli",
                "attachments": ["/a0/usr/uploads/capture.png"],
                "client_message_id": "msg-1",
            },
            "sid-cli",
        )

        assert result["status"] == "queued"
        assert result["message_queue"] == [
            {
                "id": "msg-1",
                "seq": 1,
                "text": "queued from cli",
                "attachments": ["capture.png"],
                "attachment_count": 1,
            }
        ]
        assert context._data["message_queue"][0]["text"] == "queued from cli"
        assert handler.emitted[-1] == (
            "sid-cli",
            "connector_message_queue_updated",
            {
                "context_id": context.id,
                "message_queue": result["message_queue"],
            },
        )

    asyncio.run(_scenario())


def test_ws_connector_queue_send_flushes_all_items() -> None:
    _install_fake_helpers()
    ws_connector_mod = _reload("plugins._a0_connector.api.ws_connector")

    class FakeContext:
        def __init__(self) -> None:
            self.id = "ctx-queue"
            self._data: dict[str, object] = {
                "message_queue": [
                    {"id": "item-1", "seq": 1, "text": "one", "attachments": []},
                    {"id": "item-2", "seq": 2, "text": "two", "attachments": []},
                ]
            }
            self._output_data: dict[str, object] = {}
            self.sent: list[dict[str, object]] = []

        def get_data(self, key: str) -> object:
            return self._data.get(key)

        def set_data(self, key: str, value: object) -> None:
            self._data[key] = value

        def set_output_data(self, key: str, value: object) -> None:
            self._output_data[key] = value

        def communicate(self, item: dict[str, object]) -> None:
            self.sent.append(item)

        def is_running(self) -> bool:
            return False

    context = FakeContext()
    agent_mod = types.ModuleType("agent")
    agent_mod.AgentContext = types.SimpleNamespace(get=lambda context_id: context if context_id == context.id else None)
    sys.modules["agent"] = agent_mod

    async def _scenario() -> None:
        result = await ws_connector_mod.WsConnector(None, None).process(
            "connector_message_queue_send",
            {"context_id": context.id, "send_all": True},
            "sid-cli",
        )

        assert result["status"] == "sent"
        assert result["sent_count"] == 2
        assert result["message_queue"] == []
        assert [item["text"] for item in context.sent] == ["one", "two"]

    asyncio.run(_scenario())


def test_ws_connector_stores_computer_use_metadata_from_hello() -> None:
    _install_fake_helpers()
    ws_runtime_mod = _reload("plugins._a0_connector.helpers.ws_runtime")
    _reset_ws_runtime_state(ws_runtime_mod)
    ws_connector_mod = _reload("plugins._a0_connector.api.ws_connector")

    ws_runtime_mod.register_sid("sid-cli")
    payload = asyncio.run(
        ws_connector_mod.WsConnector(None, None).process(
            "connector_hello",
            {
                "computer_use": {
                    "supported": True,
                    "enabled": True,
                    "trust_mode": "allow",
                    "status": "active",
                    "last_error": "",
                    "restore_token_present": True,
                    "artifact_root": "/a0/tmp/_a0_connector/computer_use",
                    "backend_id": "wayland",
                    "backend_family": "linux",
                    "features": ["inline-png-capture", "pointer-injection"],
                    "support_reason": "Wayland portal backend is available.",
                }
            },
            "sid-cli",
        )
    )

    stored = ws_runtime_mod.computer_use_metadata_for_sid("sid-cli")
    assert payload["exec_config"]["version"] == 1
    assert stored == {
        "supported": True,
        "enabled": True,
        "trust_mode": "allow",
        "status": "active",
        "last_error": "",
        "restore_token_present": True,
        "artifact_root": "/a0/tmp/_a0_connector/computer_use",
        "backend_id": "wayland",
        "backend_family": "linux",
        "features": ["inline-png-capture", "pointer-injection"],
        "capabilities": {},
        "contract_version": 0,
        "support_reason": "Wayland portal backend is available.",
        "updated_at": stored["updated_at"],
    }


def test_ws_connector_stores_remote_tool_metadata_from_hello() -> None:
    _install_fake_helpers()
    ws_runtime_mod = _reload("plugins._a0_connector.helpers.ws_runtime")
    _reset_ws_runtime_state(ws_runtime_mod)
    ws_connector_mod = _reload("plugins._a0_connector.api.ws_connector")

    ws_runtime_mod.register_sid("sid-cli")
    asyncio.run(
        ws_connector_mod.WsConnector(None, None).process(
            "connector_hello",
            {
                "remote_files": {
                    "enabled": True,
                    "write_enabled": False,
                    "mode": "read_only",
                },
                "remote_exec": {
                    "enabled": True,
                },
            },
            "sid-cli",
        )
    )

    remote_files = ws_runtime_mod.remote_file_metadata_for_sid("sid-cli")
    remote_exec = ws_runtime_mod.remote_exec_metadata_for_sid("sid-cli")
    assert remote_files == {
        "enabled": True,
        "write_enabled": False,
        "mode": "read_only",
        "updated_at": remote_files["updated_at"],
    }
    assert remote_exec == {
        "enabled": True,
        "updated_at": remote_exec["updated_at"],
    }


def test_ws_connector_hello_with_context_id_associates_remote_tool_metadata() -> None:
    _install_fake_helpers()
    ws_runtime_mod = _reload("plugins._a0_connector.helpers.ws_runtime")
    _reset_ws_runtime_state(ws_runtime_mod)
    ws_connector_mod = _reload("plugins._a0_connector.api.ws_connector")

    agent_mod = types.ModuleType("agent")

    class FakeAgentContext:
        @staticmethod
        def get(context_id: str) -> object | None:
            return object() if context_id == "ctx-remote" else None

    agent_mod.AgentContext = FakeAgentContext
    sys.modules["agent"] = agent_mod

    ws_runtime_mod.register_sid("sid-cli")
    payload = asyncio.run(
        ws_connector_mod.WsConnector(None, None).process(
            "connector_hello",
            {
                "context_id": "ctx-remote",
                "remote_files": {
                    "enabled": True,
                    "write_enabled": True,
                    "mode": "read_write",
                },
                "remote_exec": {
                    "enabled": True,
                },
            },
            "sid-cli",
        )
    )

    assert ws_runtime_mod.subscribed_sids_for_context("ctx-remote") == {"sid-cli"}
    assert payload["remote_tools"] == {
        "contexts": ["ctx-remote"],
        "computer_use": False,
        "host_browser": False,
        "host_browser_status": {},
        "remote_files": True,
        "remote_file_writes": True,
        "remote_exec": True,
    }


def test_remote_file_structure_is_injected_as_extras_not_system_prompt() -> None:
    _install_fake_helpers()
    ws_runtime_mod = _reload("plugins._a0_connector.helpers.ws_runtime")
    _reset_ws_runtime_state(ws_runtime_mod)

    class FakeLoopData:
        def __init__(self) -> None:
            self.system = []
            self.extras_temporary = {}
            self.extras_persistent = {}

    agent_mod = types.ModuleType("agent")
    agent_mod.LoopData = FakeLoopData
    sys.modules["agent"] = agent_mod

    extension_mod = types.ModuleType("helpers.extension")

    class Extension:
        def __init__(self, agent=None, **kwargs) -> None:
            self.agent = agent
            self.kwargs = kwargs

    extension_mod.Extension = Extension
    sys.modules["helpers.extension"] = extension_mod
    sys.modules["helpers"].extension = extension_mod

    include_mod = _reload(
        "plugins._a0_connector.extensions.python.message_loop_prompts_after."
        "_76_include_remote_file_structure"
    )

    sid = "sid-tree"
    context_id = "ctx-tree"
    ws_runtime_mod.register_sid(sid)
    ws_runtime_mod.subscribe_sid_to_context(sid, context_id)
    ws_runtime_mod.store_remote_tree_snapshot(
        sid,
        {
            "root_path": r"C:\workspace\a0-connector",
            "tree": "C:/workspace/a0-connector/\n|-- src/\n`-- pyproject.toml",
            "tree_hash": "tree-hash-1",
            "generated_at": "2026-04-14T12:00:00+00:00",
        },
    )

    class FakeContext:
        id = context_id

    class FakeAgent:
        context = FakeContext()

        def read_prompt(self, file: str, **kwargs) -> str:
            assert file == "agent.extras.remote_file_structure.md"
            return f"REMOTE_TREE_EXTRAS\n{kwargs['folder']}\n{kwargs['file_structure']}"

    loop_data = FakeLoopData()
    loop_data.system.append("static system prompt")

    asyncio.run(
        include_mod.IncludeRemoteFileStructure(agent=FakeAgent()).execute(
            loop_data=loop_data
        )
    )

    assert loop_data.system == ["static system prompt"]
    assert set(loop_data.extras_temporary) == {"remote_file_structure"}
    remote_tree_prompt = loop_data.extras_temporary["remote_file_structure"]
    assert "REMOTE_TREE_EXTRAS" in remote_tree_prompt
    assert r"C:\workspace\a0-connector" in remote_tree_prompt
    assert "pyproject.toml" in remote_tree_prompt


def _install_fake_extension_helper() -> None:
    extension_mod = types.ModuleType("helpers.extension")

    class Extension:
        def __init__(self, agent=None, **kwargs) -> None:
            self.agent = agent
            self.kwargs = kwargs

    extension_mod.Extension = Extension
    sys.modules["helpers.extension"] = extension_mod
    sys.modules["helpers"].extension = extension_mod


def _load_remote_tool_stubs_extension():
    _install_fake_extension_helper()
    return _reload(
        "plugins._a0_connector.extensions.python._functions._11_tools_prompt."
        "build_prompt.end._70_include_remote_tool_stubs"
    )


def test_legacy_remote_tool_stubs_gate_is_noop_when_cli_capabilities_are_available() -> None:
    _install_fake_helpers()
    ws_runtime_mod = _reload("plugins._a0_connector.helpers.ws_runtime")
    _reset_ws_runtime_state(ws_runtime_mod)
    include_mod = _load_remote_tool_stubs_extension()

    sid = "sid-all-remote-tools"
    context_id = "ctx-all-remote-tools"
    ws_runtime_mod.register_sid(sid)
    ws_runtime_mod.subscribe_sid_to_context(sid, context_id)
    ws_runtime_mod.store_sid_remote_file_metadata(
        sid,
        {"enabled": True, "write_enabled": True, "mode": "read_write"},
    )
    ws_runtime_mod.store_sid_remote_exec_metadata(sid, {"enabled": True})
    ws_runtime_mod.store_sid_computer_use_metadata(
        sid,
        {
            "supported": True,
            "enabled": True,
            "trust_mode": "allow",
            "backend_id": "wayland",
            "backend_family": "linux",
            "features": ["inline-png-capture", "pointer-injection"],
        },
    )

    class FakeContext:
        id = context_id

    class FakeAgent:
        context = FakeContext()

        def read_prompt(self, file: str, **kwargs) -> str:
            raise AssertionError(f"read_prompt should not be called, got {file!r}")

    data = {"result": "## available tools\nbase_tool"}
    include_mod.IncludeRemoteToolStubs(agent=FakeAgent()).execute(data=data)

    assert data["result"] == "## available tools\nbase_tool"


def test_legacy_remote_tool_stubs_gate_is_noop_for_read_only_file_access() -> None:
    _install_fake_helpers()
    ws_runtime_mod = _reload("plugins._a0_connector.helpers.ws_runtime")
    _reset_ws_runtime_state(ws_runtime_mod)
    include_mod = _load_remote_tool_stubs_extension()

    sid = "sid-read-only"
    context_id = "ctx-read-only"
    ws_runtime_mod.register_sid(sid)
    ws_runtime_mod.subscribe_sid_to_context(sid, context_id)
    ws_runtime_mod.store_sid_remote_file_metadata(
        sid,
        {"enabled": True, "write_enabled": False, "mode": "read_only"},
    )
    ws_runtime_mod.store_sid_remote_exec_metadata(sid, {"enabled": True})

    class FakeContext:
        id = context_id

    class FakeAgent:
        context = FakeContext()

        def read_prompt(self, file: str, **kwargs) -> str:
            raise AssertionError(f"read_prompt should not be called, got {file!r}")

    data = {"result": "## available tools\nbase_tool"}
    include_mod.IncludeRemoteToolStubs(agent=FakeAgent()).execute(data=data)

    assert data["result"] == "## available tools\nbase_tool"


def test_remote_tool_stubs_are_not_injected_without_enabled_cli_capabilities() -> None:
    _install_fake_helpers()
    ws_runtime_mod = _reload("plugins._a0_connector.helpers.ws_runtime")
    _reset_ws_runtime_state(ws_runtime_mod)
    include_mod = _load_remote_tool_stubs_extension()

    sid = "sid-disabled"
    context_id = "ctx-disabled"
    ws_runtime_mod.register_sid(sid)
    ws_runtime_mod.subscribe_sid_to_context(sid, context_id)
    ws_runtime_mod.store_sid_remote_file_metadata(sid, {"enabled": False})
    ws_runtime_mod.store_sid_remote_exec_metadata(sid, {"enabled": False})
    ws_runtime_mod.store_sid_computer_use_metadata(
        sid,
        {
            "supported": True,
            "enabled": False,
            "trust_mode": "allow",
        },
    )

    class FakeContext:
        id = context_id

    class FakeAgent:
        context = FakeContext()

        def read_prompt(self, file: str, **kwargs) -> str:
            raise AssertionError(f"read_prompt should not be called, got {file!r}")

    data = {"result": "## available tools\nbase_tool"}
    include_mod.IncludeRemoteToolStubs(agent=FakeAgent()).execute(data=data)

    assert data["result"] == "## available tools\nbase_tool"


def test_select_remote_exec_target_sid_ignores_disabled_clients() -> None:
    _install_fake_helpers()
    ws_runtime_mod = _reload("plugins._a0_connector.helpers.ws_runtime")
    _reset_ws_runtime_state(ws_runtime_mod)

    for sid in ("sid-disabled", "sid-enabled"):
        ws_runtime_mod.register_sid(sid)
        ws_runtime_mod.subscribe_sid_to_context(sid, "ctx-1")

    ws_runtime_mod.store_sid_remote_exec_metadata("sid-disabled", {"enabled": False})
    ws_runtime_mod.store_sid_remote_exec_metadata("sid-enabled", {"enabled": True})

    assert ws_runtime_mod.select_remote_exec_target_sid("ctx-1") == "sid-enabled"


def test_select_remote_exec_target_sid_requires_write_enabled_for_mutating_runtimes() -> None:
    _install_fake_helpers()
    ws_runtime_mod = _reload("plugins._a0_connector.helpers.ws_runtime")
    _reset_ws_runtime_state(ws_runtime_mod)

    for sid in ("sid-read-only", "sid-read-write"):
        ws_runtime_mod.register_sid(sid)
        ws_runtime_mod.subscribe_sid_to_context(sid, "ctx-1")
        ws_runtime_mod.store_sid_remote_exec_metadata(sid, {"enabled": True})

    ws_runtime_mod.store_sid_remote_file_metadata(
        "sid-read-only",
        {"enabled": True, "write_enabled": False, "mode": "read_only"},
    )
    ws_runtime_mod.store_sid_remote_file_metadata(
        "sid-read-write",
        {"enabled": True, "write_enabled": True, "mode": "read_write"},
    )

    assert ws_runtime_mod.select_remote_exec_target_sid("ctx-1") == "sid-read-only"
    assert (
        ws_runtime_mod.select_remote_exec_target_sid("ctx-1", require_writes=True)
        == "sid-read-write"
    )


def test_code_execution_remote_rejects_mutating_runtime_when_only_read_only_cli_is_subscribed() -> None:
    _install_fake_helpers()
    ws_runtime_mod = _reload("plugins._a0_connector.helpers.ws_runtime")
    _reset_ws_runtime_state(ws_runtime_mod)
    tool_mod = _reload("plugins._a0_connector.tools.code_execution_remote")
    agent = _FakeRemoteAgent()

    ws_runtime_mod.register_sid("sid-cli")
    ws_runtime_mod.subscribe_sid_to_context("sid-cli", agent.context.id)
    ws_runtime_mod.store_sid_remote_exec_metadata("sid-cli", {"enabled": True})
    ws_runtime_mod.store_sid_remote_file_metadata(
        "sid-cli",
        {"enabled": True, "write_enabled": False, "mode": "read_only"},
    )

    response = asyncio.run(
        _create_code_execution_remote(
            tool_mod,
            agent,
            runtime="terminal",
            session=0,
            code="pwd",
        ).execute()
    )

    assert "Press F3" in response.message
    assert "runtime=output" in response.message


def test_code_execution_remote_allows_output_runtime_while_cli_is_read_only() -> None:
    def handler(payload: dict[str, object]) -> dict[str, object]:
        return {
            "op_id": payload["op_id"],
            "ok": True,
            "result": {
                "message": "Session 8 completed.",
                "output": "tick:1\ntick:2\ntick:3",
                "running": False,
            },
        }

    shared_ws_manager, ws_runtime_mod, tool_mod = _load_code_execution_remote_tool(
        exec_handler=handler
    )
    agent = _FakeRemoteAgent()
    ws_runtime_mod.register_sid("sid-cli")
    ws_runtime_mod.subscribe_sid_to_context("sid-cli", agent.context.id)
    ws_runtime_mod.store_sid_remote_exec_metadata("sid-cli", {"enabled": True})
    ws_runtime_mod.store_sid_remote_file_metadata(
        "sid-cli",
        {"enabled": True, "write_enabled": False, "mode": "read_only"},
    )

    response = asyncio.run(
        _create_code_execution_remote(
            tool_mod,
            agent,
            runtime="output",
            session=8,
        ).execute()
    )

    assert response.message == "Session 8 completed.\n\ntick:1\ntick:2\ntick:3"
    assert shared_ws_manager.calls[0]["payload"]["runtime"] == "output"


def test_code_execution_remote_forwards_code_execution_timeouts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code_timeouts = {
        "first_output_timeout": 12,
        "between_output_timeout": 8,
        "max_exec_timeout": 345,
        "dialog_timeout": 2,
    }
    output_timeouts = {
        "first_output_timeout": 24,
        "between_output_timeout": 12,
        "max_exec_timeout": 600,
        "dialog_timeout": 3,
    }
    config = {
        "code_exec_first_output_timeout": code_timeouts["first_output_timeout"],
        "code_exec_between_output_timeout": code_timeouts["between_output_timeout"],
        "code_exec_max_exec_timeout": code_timeouts["max_exec_timeout"],
        "code_exec_dialog_timeout": code_timeouts["dialog_timeout"],
        "output_first_output_timeout": output_timeouts["first_output_timeout"],
        "output_between_output_timeout": output_timeouts["between_output_timeout"],
        "output_max_exec_timeout": output_timeouts["max_exec_timeout"],
        "output_dialog_timeout": output_timeouts["dialog_timeout"],
    }

    def handler(payload: dict[str, object]) -> dict[str, object]:
        return {
            "op_id": payload["op_id"],
            "ok": True,
            "result": {
                "message": f"Session {payload['session']} completed.",
                "output": str(payload["runtime"]),
                "running": False,
            },
        }

    shared_ws_manager, ws_runtime_mod, tool_mod = _load_code_execution_remote_tool(
        exec_handler=handler,
        code_execution_config=config,
    )
    original_wait_for = asyncio.wait_for
    wait_timeouts: list[float] = []

    async def recording_wait_for(future, timeout):
        wait_timeouts.append(timeout)
        return await original_wait_for(future, timeout=0.25)

    monkeypatch.setattr(tool_mod.asyncio, "wait_for", recording_wait_for)

    agent = _FakeRemoteAgent()
    ws_runtime_mod.register_sid("sid-cli")
    ws_runtime_mod.subscribe_sid_to_context("sid-cli", agent.context.id)
    ws_runtime_mod.store_sid_remote_exec_metadata("sid-cli", {"enabled": True})
    ws_runtime_mod.store_sid_remote_file_metadata(
        "sid-cli",
        {"enabled": True, "write_enabled": True, "mode": "read_write"},
    )

    terminal_response = asyncio.run(
        _create_code_execution_remote(
            tool_mod,
            agent,
            runtime="terminal",
            session=0,
            code="pwd",
        ).execute()
    )
    output_response = asyncio.run(
        _create_code_execution_remote(
            tool_mod,
            agent,
            runtime="output",
            session=0,
        ).execute()
    )

    assert terminal_response.message == "Session 0 completed.\n\nterminal"
    assert output_response.message == "Session 0 completed.\n\noutput"
    assert shared_ws_manager.calls[0]["payload"]["timeouts"] == code_timeouts
    assert shared_ws_manager.calls[1]["payload"]["timeouts"] == output_timeouts
    assert wait_timeouts == [360.0, 615.0]


def test_code_execution_remote_forwards_reset_true_with_replacement_command() -> None:
    def handler(payload: dict[str, object]) -> dict[str, object]:
        return {
            "op_id": payload["op_id"],
            "ok": True,
            "result": {
                "message": "Session 0 completed.",
                "output": "world",
                "running": False,
            },
        }

    shared_ws_manager, ws_runtime_mod, tool_mod = _load_code_execution_remote_tool(
        exec_handler=handler
    )
    agent = _FakeRemoteAgent()
    ws_runtime_mod.register_sid("sid-cli")
    ws_runtime_mod.subscribe_sid_to_context("sid-cli", agent.context.id)
    ws_runtime_mod.store_sid_remote_exec_metadata("sid-cli", {"enabled": True})
    ws_runtime_mod.store_sid_remote_file_metadata(
        "sid-cli",
        {"enabled": True, "write_enabled": True, "mode": "read_write"},
    )

    response = asyncio.run(
        _create_code_execution_remote(
            tool_mod,
            agent,
            runtime="terminal",
            session=0,
            code="echo world",
            reset=True,
        ).execute()
    )

    payload = shared_ws_manager.calls[0]["payload"]
    assert response.message == "Session 0 completed.\n\nworld"
    assert payload["runtime"] == "terminal"
    assert payload["code"] == "echo world"
    assert payload["reset"] is True


def test_select_remote_file_target_sid_requires_write_enabled_for_writes() -> None:
    _install_fake_helpers()
    ws_runtime_mod = _reload("plugins._a0_connector.helpers.ws_runtime")
    _reset_ws_runtime_state(ws_runtime_mod)

    for sid in ("sid-read-only", "sid-read-write"):
        ws_runtime_mod.register_sid(sid)
        ws_runtime_mod.subscribe_sid_to_context(sid, "ctx-1")

    ws_runtime_mod.store_sid_remote_file_metadata(
        "sid-read-only",
        {"enabled": True, "write_enabled": False, "mode": "read_only"},
    )
    ws_runtime_mod.store_sid_remote_file_metadata(
        "sid-read-write",
        {"enabled": True, "write_enabled": True, "mode": "read_write"},
    )

    assert ws_runtime_mod.select_remote_file_target_sid("ctx-1") == "sid-read-only"
    assert (
        ws_runtime_mod.select_remote_file_target_sid("ctx-1", require_writes=True)
        == "sid-read-write"
    )


def test_ws_connector_chunked_file_result_resolves_pending_future() -> None:
    _install_fake_helpers()
    ws_runtime_mod = _reload("plugins._a0_connector.helpers.ws_runtime")
    _reset_ws_runtime_state(ws_runtime_mod)
    ws_connector_mod = _reload("plugins._a0_connector.api.ws_connector")
    handler = ws_connector_mod.WsConnector(None, None)

    async def _scenario() -> None:
        sid = "sid-file"
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        result = {
            "op_id": "file-1",
            "ok": True,
            "result": {"content": "large\n" * 5000, "total_lines": 5000},
        }
        raw = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        chunks = [raw[index : index + 4096] for index in range(0, len(raw), 4096)]

        ws_runtime_mod.register_sid(sid)
        ws_runtime_mod.store_pending_file_op(
            "file-1",
            sid=sid,
            future=future,
            loop=loop,
            context_id="ctx-1",
        )

        for chunk_index, chunk in enumerate(chunks):
            response = handler._handle_file_op_result(
                {
                    "op_id": "file-1",
                    "chunked": True,
                    "chunk_index": chunk_index,
                    "chunk_count": len(chunks),
                    "encoding": "json+base64",
                    "data": base64.b64encode(chunk).decode("ascii"),
                },
                sid,
            )
            assert response == {"op_id": "file-1", "accepted": True}
            if chunk_index != len(chunks) - 1:
                assert not future.done()

        assert await asyncio.wait_for(future, timeout=0.25) == result

    asyncio.run(_scenario())


def test_ws_connector_exec_result_resolves_pending_future() -> None:
    _install_fake_helpers()
    ws_runtime_mod = _reload("plugins._a0_connector.helpers.ws_runtime")
    _reset_ws_runtime_state(ws_runtime_mod)
    ws_connector_mod = _reload("plugins._a0_connector.api.ws_connector")
    handler = ws_connector_mod.WsConnector(None, None)

    async def _scenario() -> None:
        sid = "sid-exec"
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        ws_runtime_mod.register_sid(sid)
        ws_runtime_mod.store_pending_exec_op(
            "exec-1",
            sid=sid,
            future=future,
            loop=loop,
            context_id="ctx-1",
        )

        result = handler._handle_exec_op_result(
            {
                "op_id": "exec-1",
                "ok": True,
                "result": {"message": "Session 0 completed.", "output": "42", "running": False},
            },
            sid,
        )

        assert result == {"op_id": "exec-1", "accepted": True}
        resolved = await asyncio.wait_for(future, timeout=0.25)
        assert resolved["result"]["output"] == "42"

    asyncio.run(_scenario())


def test_ws_connector_computer_use_result_resolves_pending_future() -> None:
    _install_fake_helpers()
    ws_runtime_mod = _reload("plugins._a0_connector.helpers.ws_runtime")
    _reset_ws_runtime_state(ws_runtime_mod)
    ws_connector_mod = _reload("plugins._a0_connector.api.ws_connector")
    handler = ws_connector_mod.WsConnector(None, None)

    async def _scenario() -> None:
        sid = "sid-cu"
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        ws_runtime_mod.register_sid(sid)
        ws_runtime_mod.store_pending_computer_use_op(
            "cu-1",
            sid=sid,
            future=future,
            loop=loop,
            context_id="ctx-1",
        )

        result = handler._handle_computer_use_op_result(
            {
                "op_id": "cu-1",
                "ok": True,
                "result": {"status": "active", "session_id": "sess-1"},
            },
            sid,
        )

        assert result == {"op_id": "cu-1", "accepted": True}
        resolved = await asyncio.wait_for(future, timeout=0.25)
        assert resolved["result"]["session_id"] == "sess-1"

    asyncio.run(_scenario())


def test_select_computer_use_target_sid_ignores_disabled_or_unsupported_clients() -> None:
    _install_fake_helpers()
    ws_runtime_mod = _reload("plugins._a0_connector.helpers.ws_runtime")
    _reset_ws_runtime_state(ws_runtime_mod)

    for sid in ("sid-disabled", "sid-unsupported", "sid-enabled"):
        ws_runtime_mod.register_sid(sid)
        ws_runtime_mod.subscribe_sid_to_context(sid, "ctx-1")

    ws_runtime_mod.store_sid_computer_use_metadata(
        "sid-disabled",
        {"supported": True, "enabled": False, "trust_mode": "allow", "artifact_root": "/a0/tmp"},
    )
    ws_runtime_mod.store_sid_computer_use_metadata(
        "sid-unsupported",
        {"supported": False, "enabled": True, "trust_mode": "allow", "artifact_root": "/a0/tmp"},
    )
    ws_runtime_mod.store_sid_computer_use_metadata(
        "sid-enabled",
        {"supported": True, "enabled": True, "trust_mode": "allow", "artifact_root": "/a0/tmp"},
    )

    assert ws_runtime_mod.select_computer_use_target_sid("ctx-1") == "sid-enabled"


def test_computer_use_remote_rejects_when_no_enabled_cli_is_subscribed() -> None:
    shared_ws_manager, ws_runtime_mod, tool_mod = _load_computer_use_remote_tool(
        computer_use_handler=lambda payload: {"op_id": payload["op_id"], "ok": True, "result": {"status": "active"}}
    )
    del shared_ws_manager
    agent = _FakeRemoteAgent()

    ws_runtime_mod.register_sid("sid-disabled")
    ws_runtime_mod.subscribe_sid_to_context("sid-disabled", agent.context.id)
    ws_runtime_mod.store_sid_computer_use_metadata(
        "sid-disabled",
        {
            "supported": True,
            "enabled": False,
            "trust_mode": "allow",
            "artifact_root": "/a0/tmp/_a0_connector/computer_use",
        },
    )

    response = asyncio.run(
        _create_computer_use_remote(tool_mod, agent, action="status").execute()
    )

    assert "no connected CLI currently advertises enabled local computer use" in response.message


def test_computer_use_remote_status_prefers_linux_atspi_skill_hint() -> None:
    def handler(payload: dict[str, object]) -> dict[str, object]:
        return {
            "op_id": payload["op_id"],
            "ok": True,
            "result": {
                "status": "active",
                "trust_mode": "allow",
                "backend_id": "wayland",
                "backend_family": "linux",
                "features": [
                    "portal-remote-desktop",
                    "accessibility-tree-snapshot",
                    "atspi-tree-snapshot",
                    "atspi-structural-targeting",
                ],
            },
        }

    shared_ws_manager, ws_runtime_mod, tool_mod = _load_computer_use_remote_tool(
        computer_use_handler=handler
    )
    agent = _FakeRemoteAgent()
    ws_runtime_mod.register_sid("sid-cli")
    ws_runtime_mod.subscribe_sid_to_context("sid-cli", agent.context.id)
    ws_runtime_mod.store_sid_computer_use_metadata(
        "sid-cli",
        {
            "supported": True,
            "enabled": True,
            "trust_mode": "allow",
            "status": "active",
            "last_error": "",
            "restore_token_present": True,
            "artifact_root": "/a0/tmp/_a0_connector/computer_use",
        },
    )

    response = asyncio.run(
        _create_computer_use_remote(tool_mod, agent, action="status").execute()
    )

    assert "Load skill `host-computer-use-linux`" in response.message
    assert "host-computer-use-macos" not in response.message
    assert "backend=wayland/linux" in response.message


def test_computer_use_remote_rearm_metadata_tells_agent_to_stop_without_dispatch() -> None:
    calls: list[dict[str, object]] = []

    def handler(payload: dict[str, object]) -> dict[str, object]:
        calls.append(dict(payload))
        return {
            "op_id": payload["op_id"],
            "ok": False,
            "code": "COMPUTER_USE_REARM_REQUIRED",
            "error": (
                "Silent restore was not available. Run /computer-use on and approve "
                "the platform permission prompt."
            ),
            "result": {
                "status": "rearm required",
                "trust_mode": "allow",
                "last_error": (
                    "Silent restore was not available. Run /computer-use on and approve "
                    "the platform permission prompt."
                ),
            },
        }

    shared_ws_manager, ws_runtime_mod, tool_mod = _load_computer_use_remote_tool(
        computer_use_handler=handler
    )
    agent = _FakeRemoteAgent()
    ws_runtime_mod.register_sid("sid-cli")
    ws_runtime_mod.subscribe_sid_to_context("sid-cli", agent.context.id)
    ws_runtime_mod.store_sid_computer_use_metadata(
        "sid-cli",
        {
            "supported": True,
            "enabled": True,
            "trust_mode": "allow",
            "status": "rearm required",
            "last_error": (
                "Silent restore was not available. Run /computer-use on and approve "
                "the platform permission prompt."
            ),
            "restore_token_present": True,
            "artifact_root": "/a0/tmp/_a0_connector/computer_use",
        },
    )

    response = asyncio.run(
        _create_computer_use_remote(tool_mod, agent, action="start_session").execute()
    )

    assert "COMPUTER_USE_REARM_REQUIRED" in response.message
    assert "Stop using computer_use_remote for now" in response.message
    assert "platform permission prompt" in response.message
    assert "Do not retry or use screenshot fallbacks" in response.message
    assert calls == []


def test_computer_use_remote_runtime_rearm_result_tells_agent_to_stop() -> None:
    calls: list[dict[str, object]] = []

    def handler(payload: dict[str, object]) -> dict[str, object]:
        calls.append(dict(payload))
        return {
            "op_id": payload["op_id"],
            "ok": False,
            "code": "COMPUTER_USE_REARM_REQUIRED",
            "error": (
                "Silent restore was not available. Run /computer-use on and approve "
                "the platform permission prompt."
            ),
            "result": {
                "status": "rearm required",
                "trust_mode": "allow",
                "last_error": (
                    "Silent restore was not available. Run /computer-use on and approve "
                    "the platform permission prompt."
                ),
            },
        }

    shared_ws_manager, ws_runtime_mod, tool_mod = _load_computer_use_remote_tool(
        computer_use_handler=handler
    )
    agent = _FakeRemoteAgent()
    ws_runtime_mod.register_sid("sid-cli")
    ws_runtime_mod.subscribe_sid_to_context("sid-cli", agent.context.id)
    ws_runtime_mod.store_sid_computer_use_metadata(
        "sid-cli",
        {
            "supported": True,
            "enabled": True,
            "trust_mode": "allow",
            "status": "allow",
            "last_error": "",
            "restore_token_present": True,
            "artifact_root": "/a0/tmp/_a0_connector/computer_use",
        },
    )

    response = asyncio.run(
        _create_computer_use_remote(tool_mod, agent, action="start_session").execute()
    )

    assert "COMPUTER_USE_REARM_REQUIRED" in response.message
    assert "Stop using computer_use_remote for now" in response.message
    assert "platform permission prompt" in response.message
    assert "Do not retry or use screenshot fallbacks" in response.message
    assert [call["action"] for call in calls] == ["start_session"]


def test_computer_use_remote_runtime_approval_required_tells_agent_to_stop() -> None:
    calls: list[dict[str, object]] = []

    def handler(payload: dict[str, object]) -> dict[str, object]:
        calls.append(dict(payload))
        return {
            "op_id": payload["op_id"],
            "ok": False,
            "code": "COMPUTER_USE_APPROVAL_REQUIRED",
            "error": "macOS Accessibility permission is required.",
            "result": {
                "status": "rearm required",
                "trust_mode": "allow",
                "last_error": "macOS Accessibility permission is required.",
            },
        }

    shared_ws_manager, ws_runtime_mod, tool_mod = _load_computer_use_remote_tool(
        computer_use_handler=handler
    )
    agent = _FakeRemoteAgent()
    ws_runtime_mod.register_sid("sid-cli")
    ws_runtime_mod.subscribe_sid_to_context("sid-cli", agent.context.id)
    ws_runtime_mod.store_sid_computer_use_metadata(
        "sid-cli",
        {
            "supported": True,
            "enabled": True,
            "trust_mode": "allow",
            "status": "allow",
            "last_error": "",
            "restore_token_present": True,
            "artifact_root": "/a0/tmp/_a0_connector/computer_use",
        },
    )

    response = asyncio.run(
        _create_computer_use_remote(tool_mod, agent, action="start_session").execute()
    )

    assert "COMPUTER_USE_REARM_REQUIRED" in response.message
    assert "macOS Accessibility permission is required" in response.message
    assert "Stop using computer_use_remote for now" in response.message
    assert "Do not retry or use screenshot fallbacks" in response.message
    assert [call["action"] for call in calls] == ["start_session"]


def test_computer_use_remote_capture_records_shared_path_image_message(tmp_path: Path) -> None:
    image_path = _write_png_fixture(tmp_path)

    def handler(payload: dict[str, object]) -> dict[str, object]:
        return {
            "op_id": payload["op_id"],
            "ok": True,
            "result": {
                "status": "active",
                "session_id": "sess-1",
                "host_path": str(image_path),
                "width": 1,
                "height": 1,
            },
        }

    shared_ws_manager, ws_runtime_mod, tool_mod = _load_computer_use_remote_tool(
        computer_use_handler=handler
    )
    agent = _FakeRemoteAgent()
    ws_runtime_mod.register_sid("sid-cli")
    ws_runtime_mod.subscribe_sid_to_context("sid-cli", agent.context.id)
    ws_runtime_mod.store_sid_computer_use_metadata(
        "sid-cli",
        {
            "supported": True,
            "enabled": True,
            "trust_mode": "allow",
            "artifact_root": "/a0/tmp/_a0_connector/computer_use",
        },
    )

    response = asyncio.run(
        _create_computer_use_remote(tool_mod, agent, action="capture", session_id="sess-1").execute()
    )

    assert response.message == (
        f"Current screen attached: {_expected_capture_summary()} {_capture_verification_note(tool_mod)}"
    )
    assert shared_ws_manager.calls[0]["payload"]["action"] == "capture"
    assert agent.history_messages == []
    _assert_capture_response_content(
        response,
        message=response.message,
        preview=_expected_capture_summary(),
        image_url=str(image_path),
    )

    tool = _create_computer_use_remote(tool_mod, agent, action="capture", session_id="sess-1")
    tool.name = "computer_use_remote"
    asyncio.run(tool.after_execution(response))
    _assert_capture_history_message(
        agent,
        message=response.message,
        preview=_expected_capture_summary(),
        image_url=str(image_path),
    )


def test_computer_use_remote_capture_uses_shared_png_path(
    tmp_path: Path,
) -> None:
    image_path = _write_png_fixture(tmp_path)

    def handler(payload: dict[str, object]) -> dict[str, object]:
        return {
            "op_id": payload["op_id"],
            "ok": True,
            "result": {
                "status": "active",
                "session_id": "sess-1",
                "host_path": str(image_path),
                "width": 1,
                "height": 1,
            },
        }

    shared_ws_manager, ws_runtime_mod, tool_mod = _load_computer_use_remote_tool(
        computer_use_handler=handler
    )
    agent = _FakeRemoteAgent()
    ws_runtime_mod.register_sid("sid-cli")
    ws_runtime_mod.subscribe_sid_to_context("sid-cli", agent.context.id)
    ws_runtime_mod.store_sid_computer_use_metadata(
        "sid-cli",
        {
            "supported": True,
            "enabled": True,
            "trust_mode": "allow",
            "artifact_root": "/a0/tmp/_a0_connector/computer_use",
        },
    )

    response = asyncio.run(
        _create_computer_use_remote(tool_mod, agent, action="capture", session_id="sess-1").execute()
    )

    assert response.message == (
        f"Current screen attached: {_expected_capture_summary()} {_capture_verification_note(tool_mod)}"
    )
    assert [call["payload"]["action"] for call in shared_ws_manager.calls] == ["capture"]
    assert agent.history_messages == []
    _assert_capture_response_content(
        response,
        message=response.message,
        preview=_expected_capture_summary(),
        image_url=str(image_path),
    )


def test_computer_use_remote_capture_uses_base64_data_url_without_materializing(
    tmp_path: Path,
) -> None:
    image_path = _write_png_fixture(tmp_path)
    capture_bytes = image_path.read_bytes()

    def handler(payload: dict[str, object]) -> dict[str, object]:
        return {
            "op_id": payload["op_id"],
            "ok": True,
            "result": {
                "status": "active",
                "session_id": "sess-1",
                "host_path": "/tmp/_a0_connector/computer_use/ctx-1/host-only.png",
                "container_path": "/a0/tmp/_a0_connector/computer_use/ctx-1/host-only.png",
                "artifact": {
                    "filename": "host-only.png",
                    "mime": "image/png",
                    "encoding": "base64",
                    "data": base64.b64encode(capture_bytes).decode("ascii"),
                },
                "width": 1,
                "height": 1,
            },
        }

    shared_ws_manager, ws_runtime_mod, tool_mod = _load_computer_use_remote_tool(
        computer_use_handler=handler
    )
    agent = _FakeRemoteAgent()
    ws_runtime_mod.register_sid("sid-cli")
    ws_runtime_mod.subscribe_sid_to_context("sid-cli", agent.context.id)
    ws_runtime_mod.store_sid_computer_use_metadata(
        "sid-cli",
        {
            "supported": True,
            "enabled": True,
            "trust_mode": "allow",
            "artifact_root": "/a0/tmp/_a0_connector/computer_use",
        },
    )

    response = asyncio.run(
        _create_computer_use_remote(tool_mod, agent, action="capture", session_id="sess-1").execute()
    )

    expected_summary = _expected_capture_summary("host-only")
    assert response.message == (
        f"Current screen attached: {expected_summary} {_capture_verification_note(tool_mod)}"
    )
    assert agent.history_messages == []
    additional = response.additional
    assert isinstance(additional, dict)
    raw_content = additional["raw_content"]
    image_url = raw_content[1]["image_url"]["url"]
    assert image_url == f"data:image/png;base64,{base64.b64encode(capture_bytes).decode('ascii')}"
    assert list(tmp_path.rglob("*.png")) == [image_path]


def test_computer_use_remote_capture_prefers_shared_path_over_base64_artifact(
    tmp_path: Path,
) -> None:
    image_path = _write_png_fixture(tmp_path)
    capture_bytes = image_path.read_bytes()

    def handler(payload: dict[str, object]) -> dict[str, object]:
        return {
            "op_id": payload["op_id"],
            "ok": True,
            "result": {
                "status": "active",
                "session_id": "sess-1",
                "host_path": str(image_path),
                "artifact": {
                    "filename": "capture.png",
                    "mime": "image/png",
                    "encoding": "base64",
                    "data": base64.b64encode(capture_bytes).decode("ascii"),
                },
                "width": 1,
                "height": 1,
            },
        }

    shared_ws_manager, ws_runtime_mod, tool_mod = _load_computer_use_remote_tool(
        computer_use_handler=handler
    )
    agent = _FakeRemoteAgent()
    ws_runtime_mod.register_sid("sid-cli")
    ws_runtime_mod.subscribe_sid_to_context("sid-cli", agent.context.id)
    ws_runtime_mod.store_sid_computer_use_metadata(
        "sid-cli",
        {
            "supported": True,
            "enabled": True,
            "trust_mode": "allow",
            "artifact_root": "/a0/tmp/_a0_connector/computer_use",
        },
    )

    response = asyncio.run(
        _create_computer_use_remote(tool_mod, agent, action="capture", session_id="sess-1").execute()
    )

    expected_summary = _expected_capture_summary()
    assert response.message == (
        f"Current screen attached: {expected_summary} {_capture_verification_note(tool_mod)}"
    )
    _assert_capture_response_content(
        response,
        message=response.message,
        preview=expected_summary,
        image_url=str(image_path),
    )


def test_computer_use_remote_capture_missing_path_returns_tool_message() -> None:
    def handler(payload: dict[str, object]) -> dict[str, object]:
        return {
            "op_id": payload["op_id"],
            "ok": True,
            "result": {
                "status": "active",
                "session_id": "sess-1",
                "host_path": "/tmp/_a0_connector/computer_use/ctx-1/missing.png",
                "container_path": "/a0/tmp/_a0_connector/computer_use/ctx-1/missing.png",
                "width": 1,
                "height": 1,
            },
        }

    shared_ws_manager, ws_runtime_mod, tool_mod = _load_computer_use_remote_tool(
        computer_use_handler=handler
    )
    agent = _FakeRemoteAgent()
    ws_runtime_mod.register_sid("sid-cli")
    ws_runtime_mod.subscribe_sid_to_context("sid-cli", agent.context.id)
    ws_runtime_mod.store_sid_computer_use_metadata(
        "sid-cli",
        {
            "supported": True,
            "enabled": True,
            "trust_mode": "allow",
            "artifact_root": "/a0/tmp/_a0_connector/computer_use",
        },
    )

    response = asyncio.run(
        _create_computer_use_remote(tool_mod, agent, action="capture", session_id="sess-1").execute()
    )

    assert response.message.startswith("computer_use_remote: error sending action='capture': ")
    assert "Capture artifact was not found in any advertised path" in response.message
    assert agent.history_messages == []


def test_computer_use_remote_start_session_auto_refreshes_screen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image_path = _write_png_fixture(tmp_path)

    def handler(payload: dict[str, object]) -> dict[str, object]:
        if payload["action"] == "start_session":
            return {
                "op_id": payload["op_id"],
                "ok": True,
                "result": {
                    "status": "active",
                    "session_id": "sess-1",
                    "width": 1,
                    "height": 1,
                },
            }
        return {
            "op_id": payload["op_id"],
            "ok": True,
            "result": {
                "status": "active",
                "session_id": "sess-1",
                "host_path": str(image_path),
                "fresh": bool(payload.get("fresh")),
                "fresh_after_satisfied": True,
                "width": 1,
                "height": 1,
            },
        }

    shared_ws_manager, ws_runtime_mod, tool_mod = _load_computer_use_remote_tool(
        computer_use_handler=handler
    )
    monkeypatch.setattr(tool_mod.asyncio, "sleep", _no_sleep)
    agent = _FakeRemoteAgent()
    ws_runtime_mod.register_sid("sid-cli")
    ws_runtime_mod.subscribe_sid_to_context("sid-cli", agent.context.id)
    ws_runtime_mod.store_sid_computer_use_metadata(
        "sid-cli",
        {
            "supported": True,
            "enabled": True,
            "trust_mode": "allow",
            "artifact_root": "/a0/tmp/_a0_connector/computer_use",
        },
    )

    response = asyncio.run(
        _create_computer_use_remote(tool_mod, agent, action="start_session").execute()
    )

    assert response.message == (
        "Computer-use session started: session_id=sess-1 size=1x1 "
        f"Latest screen attached: {_expected_capture_summary(fresh=True)} "
        f"{_capture_verification_note(tool_mod)}"
    )
    assert [call["payload"]["action"] for call in shared_ws_manager.calls] == ["start_session", "capture"]
    _assert_fresh_auto_capture(tool_mod, shared_ws_manager.calls[1]["payload"])
    assert agent.history_messages == []
    _assert_capture_response_content(
        response,
        message=response.message,
        preview=_expected_capture_summary(fresh=True),
        image_url=str(image_path),
    )


def test_computer_use_remote_start_session_reports_auto_capture_attach_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(payload: dict[str, object]) -> dict[str, object]:
        if payload["action"] == "start_session":
            return {
                "op_id": payload["op_id"],
                "ok": True,
                "result": {
                    "status": "active",
                    "session_id": "sess-1",
                    "width": 1,
                    "height": 1,
                },
            }
        return {
            "op_id": payload["op_id"],
            "ok": True,
            "result": {
                "status": "active",
                "session_id": "sess-1",
                "host_path": "/tmp/_a0_connector/computer_use/ctx-1/missing.png",
                "fresh": bool(payload.get("fresh")),
                "fresh_after_satisfied": True,
                "width": 1,
                "height": 1,
            },
        }

    shared_ws_manager, ws_runtime_mod, tool_mod = _load_computer_use_remote_tool(
        computer_use_handler=handler
    )
    monkeypatch.setattr(tool_mod.asyncio, "sleep", _no_sleep)
    agent = _FakeRemoteAgent()
    ws_runtime_mod.register_sid("sid-cli")
    ws_runtime_mod.subscribe_sid_to_context("sid-cli", agent.context.id)
    ws_runtime_mod.store_sid_computer_use_metadata(
        "sid-cli",
        {
            "supported": True,
            "enabled": True,
            "trust_mode": "allow",
            "artifact_root": "/a0/tmp/_a0_connector/computer_use",
        },
    )

    response = asyncio.run(
        _create_computer_use_remote(tool_mod, agent, action="start_session").execute()
    )

    assert response.message.startswith(
        "Computer-use session started: session_id=sess-1 size=1x1 "
        "Automatic screen refresh failed: Capture artifact was not found in any advertised path"
    )
    assert [call["payload"]["action"] for call in shared_ws_manager.calls] == ["start_session", "capture"]
    assert agent.history_messages == []


def test_computer_use_remote_ax_snapshot_formats_structural_tree() -> None:
    def handler(payload: dict[str, object]) -> dict[str, object]:
        assert payload["action"] == "ax_snapshot"
        assert payload["max_depth"] == 3
        return {
            "op_id": payload["op_id"],
            "ok": True,
            "result": {
                "status": "active",
                "session_id": "sess-1",
                "app": {"name": "Fake App"},
                "node_count": 3,
                "truncated": False,
                "tree": {
                    "path": [],
                    "role": "AXApplication",
                    "title": "Fake App",
                },
            },
        }

    shared_ws_manager, ws_runtime_mod, tool_mod = _load_computer_use_remote_tool(
        computer_use_handler=handler
    )
    agent = _FakeRemoteAgent()
    ws_runtime_mod.register_sid("sid-cli")
    ws_runtime_mod.subscribe_sid_to_context("sid-cli", agent.context.id)
    ws_runtime_mod.store_sid_computer_use_metadata(
        "sid-cli",
        {
            "supported": True,
            "enabled": True,
            "trust_mode": "allow",
            "artifact_root": "/a0/tmp/_a0_connector/computer_use",
        },
    )

    response = asyncio.run(
        _create_computer_use_remote(
            tool_mod,
            agent,
            action="ax_snapshot",
            max_depth=3,
        ).execute()
    )

    assert response.message == (
        "AX snapshot for Fake App: 3 node(s). Root AXApplication 'Fake App'. "
        "Use path or semantic target fields with ax_action.\n\n"
        "Nodes:\n"
        "- path=[] role=AXApplication title='Fake App'"
    )
    assert [call["payload"]["action"] for call in shared_ws_manager.calls] == ["ax_snapshot"]


def test_computer_use_remote_ax_action_auto_refreshes_screen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image_path = _write_png_fixture(tmp_path)

    def handler(payload: dict[str, object]) -> dict[str, object]:
        if payload["action"] == "ax_action":
            return {
                "op_id": payload["op_id"],
                "ok": True,
                "result": {
                    "status": "active",
                    "session_id": "sess-1",
                    "operation": "press",
                    "target": {"path": [0, 1], "role": "AXButton", "title": "Save"},
                },
            }
        return {
            "op_id": payload["op_id"],
            "ok": True,
            "result": {
                "status": "active",
                "session_id": "sess-1",
                "host_path": str(image_path),
                "fresh": bool(payload.get("fresh")),
                "fresh_after_satisfied": True,
                "width": 1,
                "height": 1,
            },
        }

    shared_ws_manager, ws_runtime_mod, tool_mod = _load_computer_use_remote_tool(
        computer_use_handler=handler
    )
    monkeypatch.setattr(tool_mod.asyncio, "sleep", _no_sleep)
    agent = _FakeRemoteAgent()
    ws_runtime_mod.register_sid("sid-cli")
    ws_runtime_mod.subscribe_sid_to_context("sid-cli", agent.context.id)
    ws_runtime_mod.store_sid_computer_use_metadata(
        "sid-cli",
        {
            "supported": True,
            "enabled": True,
            "trust_mode": "allow",
            "artifact_root": "/a0/tmp/_a0_connector/computer_use",
        },
    )

    response = asyncio.run(
        _create_computer_use_remote(
            tool_mod,
            agent,
            action="ax_action",
            target={"role": "AXButton", "title": "Save"},
            operation="press",
        ).execute()
    )

    assert response.message == (
        "Performed AX press on AXButton 'Save' path=[0, 1]. "
        f"Latest screen attached: {_expected_capture_summary(fresh=True)} "
        f"{_capture_verification_note(tool_mod)}"
    )
    assert [call["payload"]["action"] for call in shared_ws_manager.calls] == ["ax_action", "capture"]
    assert shared_ws_manager.calls[0]["payload"]["target"] == {"role": "AXButton", "title": "Save"}
    assert shared_ws_manager.calls[0]["payload"]["operation"] == "press"
    _assert_fresh_auto_capture(tool_mod, shared_ws_manager.calls[1]["payload"])


def test_computer_use_remote_uia_snapshot_formats_structural_tree() -> None:
    def handler(payload: dict[str, object]) -> dict[str, object]:
        assert payload["action"] == "uia_snapshot"
        assert payload["max_depth"] == 3
        return {
            "op_id": payload["op_id"],
            "ok": True,
            "result": {
                "status": "active",
                "session_id": "sess-1",
                "app": {"name": "Windows desktop"},
                "node_count": 3,
                "truncated": False,
                "tree": {
                    "path": [],
                    "role": "Desktop",
                    "title": "Windows desktop",
                },
            },
        }

    shared_ws_manager, ws_runtime_mod, tool_mod = _load_computer_use_remote_tool(
        computer_use_handler=handler
    )
    agent = _FakeRemoteAgent()
    ws_runtime_mod.register_sid("sid-cli")
    ws_runtime_mod.subscribe_sid_to_context("sid-cli", agent.context.id)
    ws_runtime_mod.store_sid_computer_use_metadata(
        "sid-cli",
        {
            "supported": True,
            "enabled": True,
            "trust_mode": "allow",
            "artifact_root": "/a0/tmp/_a0_connector/computer_use",
        },
    )

    response = asyncio.run(
        _create_computer_use_remote(
            tool_mod,
            agent,
            action="uia_snapshot",
            max_depth=3,
        ).execute()
    )

    assert response.message == (
        "Windows UIA snapshot for Windows desktop: 3 node(s). Root Desktop 'Windows desktop'. "
        "Prefer node actions with uia_action; use focus_window/minimize/restore/maximize "
        "for windows, and reserve click for a last resort.\n\n"
        "Nodes:\n"
        "- path=[] role=Desktop title='Windows desktop'"
    )
    assert [call["payload"]["action"] for call in shared_ws_manager.calls] == ["uia_snapshot"]


def test_computer_use_remote_uia_action_auto_refreshes_screen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image_path = _write_png_fixture(tmp_path)

    def handler(payload: dict[str, object]) -> dict[str, object]:
        if payload["action"] == "uia_action":
            return {
                "op_id": payload["op_id"],
                "ok": True,
                "result": {
                    "status": "active",
                    "session_id": "sess-1",
                    "operation": "invoke",
                    "target": {"path": [0, 1], "role": "Button", "title": "Save"},
                },
            }
        return {
            "op_id": payload["op_id"],
            "ok": True,
            "result": {
                "status": "active",
                "session_id": "sess-1",
                "host_path": str(image_path),
                "fresh": bool(payload.get("fresh")),
                "fresh_after_satisfied": True,
                "width": 1,
                "height": 1,
            },
        }

    shared_ws_manager, ws_runtime_mod, tool_mod = _load_computer_use_remote_tool(
        computer_use_handler=handler
    )
    monkeypatch.setattr(tool_mod.asyncio, "sleep", _no_sleep)
    agent = _FakeRemoteAgent()
    ws_runtime_mod.register_sid("sid-cli")
    ws_runtime_mod.subscribe_sid_to_context("sid-cli", agent.context.id)
    ws_runtime_mod.store_sid_computer_use_metadata(
        "sid-cli",
        {
            "supported": True,
            "enabled": True,
            "trust_mode": "allow",
            "artifact_root": "/a0/tmp/_a0_connector/computer_use",
        },
    )

    response = asyncio.run(
        _create_computer_use_remote(
            tool_mod,
            agent,
            action="uia_action",
            target={"role": "Button", "title": "Save"},
            selector="role:Button && name:Save",
            operation="invoke",
        ).execute()
    )

    assert response.message == (
        "Performed Windows UIA invoke on Button 'Save' path=[0, 1]. "
        f"Latest screen attached: {_expected_capture_summary(fresh=True)} "
        f"{_capture_verification_note(tool_mod)}"
    )
    assert [call["payload"]["action"] for call in shared_ws_manager.calls] == ["uia_action", "capture"]
    assert shared_ws_manager.calls[0]["payload"]["target"] == {
        "role": "Button",
        "title": "Save",
        "selector": "role:Button && name:Save",
    }
    assert shared_ws_manager.calls[0]["payload"]["operation"] == "invoke"
    _assert_fresh_auto_capture(tool_mod, shared_ws_manager.calls[1]["payload"])


def test_computer_use_remote_click_auto_refreshes_screen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image_path = _write_png_fixture(tmp_path)

    def handler(payload: dict[str, object]) -> dict[str, object]:
        if payload["action"] == "click":
            return {
                "op_id": payload["op_id"],
                "ok": True,
                "result": {
                    "button": "left",
                    "count": 1,
                    "session_id": "sess-1",
                },
            }
        return {
            "op_id": payload["op_id"],
            "ok": True,
            "result": {
                "status": "active",
                "session_id": "sess-1",
                "host_path": str(image_path),
                "fresh": bool(payload.get("fresh")),
                "fresh_after_satisfied": True,
                "width": 1,
                "height": 1,
            },
        }

    shared_ws_manager, ws_runtime_mod, tool_mod = _load_computer_use_remote_tool(
        computer_use_handler=handler
    )
    monkeypatch.setattr(tool_mod.asyncio, "sleep", _no_sleep)
    agent = _FakeRemoteAgent()
    ws_runtime_mod.register_sid("sid-cli")
    ws_runtime_mod.subscribe_sid_to_context("sid-cli", agent.context.id)
    ws_runtime_mod.store_sid_computer_use_metadata(
        "sid-cli",
        {
            "supported": True,
            "enabled": True,
            "trust_mode": "allow",
            "artifact_root": "/a0/tmp/_a0_connector/computer_use",
        },
    )

    response = asyncio.run(
        _create_computer_use_remote(tool_mod, agent, action="click", session_id="sess-1").execute()
    )

    assert response.message == (
        f"Clicked left button 1 time(s). Latest screen attached: {_expected_capture_summary(fresh=True)} "
        f"{_capture_verification_note(tool_mod)}"
    )
    assert [call["payload"]["action"] for call in shared_ws_manager.calls] == ["click", "capture"]
    _assert_fresh_auto_capture(tool_mod, shared_ws_manager.calls[1]["payload"])
    assert agent.history_messages == []
    _assert_capture_response_content(
        response,
        message=response.message,
        preview=_expected_capture_summary(fresh=True),
        image_url=str(image_path),
    )


def test_computer_use_remote_type_submit_sends_submit_flag_and_auto_refreshes_screen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image_path = _write_png_fixture(tmp_path)

    def handler(payload: dict[str, object]) -> dict[str, object]:
        if payload["action"] == "type":
            return {
                "op_id": payload["op_id"],
                "ok": True,
                "result": {
                    "text": payload["text"],
                    "submitted": bool(payload.get("submit")),
                    "session_id": "sess-1",
                },
            }
        return {
            "op_id": payload["op_id"],
            "ok": True,
            "result": {
                "status": "active",
                "session_id": "sess-1",
                "host_path": str(image_path),
                "fresh": bool(payload.get("fresh")),
                "fresh_after_satisfied": True,
                "width": 1,
                "height": 1,
            },
        }

    shared_ws_manager, ws_runtime_mod, tool_mod = _load_computer_use_remote_tool(
        computer_use_handler=handler
    )
    monkeypatch.setattr(tool_mod.asyncio, "sleep", _no_sleep)
    agent = _FakeRemoteAgent()
    ws_runtime_mod.register_sid("sid-cli")
    ws_runtime_mod.subscribe_sid_to_context("sid-cli", agent.context.id)
    ws_runtime_mod.store_sid_computer_use_metadata(
        "sid-cli",
        {
            "supported": True,
            "enabled": True,
            "trust_mode": "allow",
            "artifact_root": "/a0/tmp/_a0_connector/computer_use",
        },
    )

    response = asyncio.run(
        _create_computer_use_remote(
            tool_mod,
            agent,
            action="type",
            session_id="sess-1",
            text="Hello from Agent Zero",
            submit=True,
        ).execute()
    )

    assert response.message == (
        "Sent 21 global keyboard character(s) and submitted; destination was not verified. "
        "Inspect the attached screen before claiming where the text landed. Latest screen attached: "
        f"{_expected_capture_summary(fresh=True)} {_capture_verification_note(tool_mod)}"
    )
    assert shared_ws_manager.calls[0]["payload"]["submit"] is True
    assert [call["payload"]["action"] for call in shared_ws_manager.calls] == ["type", "capture"]
    _assert_fresh_auto_capture(tool_mod, shared_ws_manager.calls[1]["payload"])
    assert agent.history_messages == []
    _assert_capture_response_content(
        response,
        message=response.message,
        preview=_expected_capture_summary(fresh=True),
        image_url=str(image_path),
    )


def test_computer_use_remote_invalid_numeric_args_return_message() -> None:
    shared_ws_manager, ws_runtime_mod, tool_mod = _load_computer_use_remote_tool(
        computer_use_handler=lambda payload: {"op_id": payload["op_id"], "ok": True, "result": {"status": "active"}}
    )
    agent = _FakeRemoteAgent()
    ws_runtime_mod.register_sid("sid-cli")
    ws_runtime_mod.subscribe_sid_to_context("sid-cli", agent.context.id)
    ws_runtime_mod.store_sid_computer_use_metadata(
        "sid-cli",
        {
            "supported": True,
            "enabled": True,
            "trust_mode": "allow",
            "artifact_root": "/a0/tmp/_a0_connector/computer_use",
        },
    )

    response = asyncio.run(
        _create_computer_use_remote(tool_mod, agent, action="click", count="two").execute()
    )

    assert response.message == "computer_use_remote: count must be an integer"
    assert shared_ws_manager.calls == []


def test_text_editor_remote_patch_requires_prior_read(tmp_path: Path) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("line-1\nline-2\n", encoding="utf-8")
    utility = RemoteFileUtility(scan_root=str(tmp_path))

    shared_ws_manager, ws_runtime_mod, tool_mod = _load_text_editor_remote_tool(
        file_op_handler=utility.handle_file_op
    )
    agent = _FakeRemoteAgent()
    _register_remote_file_cli(ws_runtime_mod, "sid-cli", agent.context.id)

    response = asyncio.run(
        _create_text_editor_remote(
            tool_mod,
            agent,
            action="patch",
            path=str(target),
            edits=[{"from": 2, "to": 2, "content": "line-2-updated\n"}],
        ).execute()
    )

    assert "fw.text_editor.patch_need_read.md" in response.message
    assert shared_ws_manager.ops == ["stat"]
    assert target.read_text(encoding="utf-8") == "line-1\nline-2\n"


def test_text_editor_remote_context_patch_does_not_require_prior_read(tmp_path: Path) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("line-1\nline-2\nline-3\n", encoding="utf-8")
    utility = RemoteFileUtility(scan_root=str(tmp_path))

    shared_ws_manager, ws_runtime_mod, tool_mod = _load_text_editor_remote_tool(
        file_op_handler=utility.handle_file_op
    )
    agent = _FakeRemoteAgent()
    _register_remote_file_cli(ws_runtime_mod, "sid-cli", agent.context.id)

    first_patch = asyncio.run(
        _create_text_editor_remote(
            tool_mod,
            agent,
            action="patch",
            path=str(target),
            patch_text=(
                "*** Begin Patch\n"
                "*** Update File: sample.txt\n"
                "@@ line-1\n"
                "+inserted\n"
                "*** End Patch"
            ),
        ).execute()
    )
    second_patch = asyncio.run(
        _create_text_editor_remote(
            tool_mod,
            agent,
            action="patch",
            path=str(target),
            patch_text=(
                "*** Begin Patch\n"
                "*** Update File: sample.txt\n"
                " line-2\n"
                "-line-3\n"
                "+line-3-updated\n"
                "*** End Patch"
            ),
        ).execute()
    )

    assert first_patch.message == f"{target} patched successfully"
    assert second_patch.message == f"{target} patched successfully"
    assert shared_ws_manager.ops == ["patch", "patch"]
    assert target.read_text(encoding="utf-8") == (
        "line-1\ninserted\nline-2\nline-3-updated\n"
    )


def test_text_editor_remote_patch_detects_stale_remote_reads(tmp_path: Path) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("line-1\nline-2\n", encoding="utf-8")
    utility = RemoteFileUtility(scan_root=str(tmp_path))

    shared_ws_manager, ws_runtime_mod, tool_mod = _load_text_editor_remote_tool(
        file_op_handler=utility.handle_file_op
    )
    agent = _FakeRemoteAgent()
    _register_remote_file_cli(ws_runtime_mod, "sid-cli", agent.context.id)

    asyncio.run(
        _create_text_editor_remote(
            tool_mod,
            agent,
            action="read",
            path=str(target),
            line_from=1,
            line_to=2,
        ).execute()
    )

    target.write_text("line-1\nline-2-external\n", encoding="utf-8")
    bumped_mtime = target.stat().st_mtime + 5
    os.utime(target, (bumped_mtime, bumped_mtime))

    response = asyncio.run(
        _create_text_editor_remote(
            tool_mod,
            agent,
            action="patch",
            path=str(target),
            edits=[{"from": 2, "to": 2, "content": "line-2-patched\n"}],
        ).execute()
    )

    assert "fw.text_editor.patch_stale_read.md" in response.message
    assert shared_ws_manager.ops == ["read", "stat"]
    assert target.read_text(encoding="utf-8") == "line-1\nline-2-external\n"


def test_text_editor_remote_write_then_patch_succeeds_without_reread(tmp_path: Path) -> None:
    target = tmp_path / "sample.txt"
    utility = RemoteFileUtility(scan_root=str(tmp_path))

    shared_ws_manager, ws_runtime_mod, tool_mod = _load_text_editor_remote_tool(
        file_op_handler=utility.handle_file_op
    )
    agent = _FakeRemoteAgent()
    _register_remote_file_cli(ws_runtime_mod, "sid-cli", agent.context.id)

    write_response = asyncio.run(
        _create_text_editor_remote(
            tool_mod,
            agent,
            action="write",
            path=str(target),
            content="line-1\nline-2\n",
        ).execute()
    )
    patch_response = asyncio.run(
        _create_text_editor_remote(
            tool_mod,
            agent,
            action="patch",
            path=str(target),
            edits=[{"from": 2, "to": 2, "content": "line-2-updated\n"}],
        ).execute()
    )

    assert write_response.message == f"{target} written successfully"
    assert patch_response.message == f"{target} patched successfully"
    assert shared_ws_manager.ops == ["write", "stat", "patch"]
    assert target.read_text(encoding="utf-8") == "line-1\nline-2-updated\n"


def test_text_editor_remote_line_preserving_patches_refresh_state(tmp_path: Path) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("line-1\nline-2\nline-3\n", encoding="utf-8")
    utility = RemoteFileUtility(scan_root=str(tmp_path))

    shared_ws_manager, ws_runtime_mod, tool_mod = _load_text_editor_remote_tool(
        file_op_handler=utility.handle_file_op
    )
    agent = _FakeRemoteAgent()
    _register_remote_file_cli(ws_runtime_mod, "sid-cli", agent.context.id)

    asyncio.run(
        _create_text_editor_remote(
            tool_mod,
            agent,
            action="read",
            path=str(target),
            line_from=1,
            line_to=3,
        ).execute()
    )
    first_patch = asyncio.run(
        _create_text_editor_remote(
            tool_mod,
            agent,
            action="patch",
            path=str(target),
            edits=[{"from": 2, "to": 2, "content": "line-2a\n"}],
        ).execute()
    )
    second_patch = asyncio.run(
        _create_text_editor_remote(
            tool_mod,
            agent,
            action="patch",
            path=str(target),
            edits=[{"from": 3, "to": 3, "content": "line-3b\n"}],
        ).execute()
    )
    freshness_mod = _reload("plugins._a0_connector.helpers.text_editor_freshness")

    stored = agent.data[freshness_mod._FRESHNESS_KEY][os.path.realpath(str(target))]

    assert first_patch.message == f"{target} patched successfully"
    assert second_patch.message == f"{target} patched successfully"
    assert shared_ws_manager.ops == ["read", "stat", "patch", "stat", "patch"]
    assert stored["total_lines"] == 3
    assert stored["mtime"] == target.stat().st_mtime
    assert target.read_text(encoding="utf-8") == "line-1\nline-2a\nline-3b\n"


def test_text_editor_remote_line_count_changes_force_reread(tmp_path: Path) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("line-1\nline-2\nline-3\n", encoding="utf-8")
    utility = RemoteFileUtility(scan_root=str(tmp_path))

    shared_ws_manager, ws_runtime_mod, tool_mod = _load_text_editor_remote_tool(
        file_op_handler=utility.handle_file_op
    )
    agent = _FakeRemoteAgent()
    _register_remote_file_cli(ws_runtime_mod, "sid-cli", agent.context.id)

    asyncio.run(
        _create_text_editor_remote(
            tool_mod,
            agent,
            action="read",
            path=str(target),
            line_from=1,
            line_to=3,
        ).execute()
    )
    first_patch = asyncio.run(
        _create_text_editor_remote(
            tool_mod,
            agent,
            action="patch",
            path=str(target),
            edits=[{"from": 2, "content": "inserted\n"}],
        ).execute()
    )
    second_patch = asyncio.run(
        _create_text_editor_remote(
            tool_mod,
            agent,
            action="patch",
            path=str(target),
            edits=[{"from": 3, "to": 3, "content": "line-2b\n"}],
        ).execute()
    )

    assert first_patch.message == f"{target} patched successfully"
    assert "fw.text_editor.patch_stale_read.md" in second_patch.message
    assert shared_ws_manager.ops == ["read", "stat", "patch", "stat"]
    assert target.read_text(encoding="utf-8") == "line-1\ninserted\nline-2\nline-3\n"


def test_text_editor_remote_requires_cli_stat_support_for_fresh_patching(tmp_path: Path) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("line-1\nline-2\n", encoding="utf-8")
    utility = RemoteFileUtility(scan_root=str(tmp_path))

    def legacy_handler(payload: dict[str, object]) -> dict[str, object]:
        if payload.get("op") == "stat":
            return {
                "op_id": payload.get("op_id"),
                "ok": False,
                "error": "Unknown op: stat",
            }
        return utility.handle_file_op(payload)

    shared_ws_manager, ws_runtime_mod, tool_mod = _load_text_editor_remote_tool(
        file_op_handler=legacy_handler
    )
    agent = _FakeRemoteAgent()
    _register_remote_file_cli(ws_runtime_mod, "sid-cli", agent.context.id)

    response = asyncio.run(
        _create_text_editor_remote(
            tool_mod,
            agent,
            action="patch",
            path=str(target),
            edits=[{"from": 2, "to": 2, "content": "line-2-updated\n"}],
        ).execute()
    )

    assert "Upgrade the CLI" in response.message
    assert shared_ws_manager.ops == ["stat"]
    assert target.read_text(encoding="utf-8") == "line-1\nline-2\n"
