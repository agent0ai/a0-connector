from __future__ import annotations

import asyncio
import base64
import importlib
import os
import sys
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
        if name == "agent" or name.startswith(("agent.", "helpers", "plugins")):
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
    _make_package("plugins._model_config")
    _make_package("plugins._model_config.helpers")
    _make_package("plugins._chat_compaction")
    _make_package("plugins._chat_compaction.helpers")

    helpers_pkg = _make_package("helpers")
    api_mod = types.ModuleType("helpers.api")
    login_mod = types.ModuleType("helpers.login")
    plugins_mod = types.ModuleType("helpers.plugins")
    print_style_mod = types.ModuleType("helpers.print_style")
    history_mod = types.ModuleType("helpers.history")
    tool_mod = types.ModuleType("helpers.tool")
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
        def __init__(self, message: str = "", break_loop: bool = False) -> None:
            self.message = message
            self.break_loop = break_loop

    class PrintStyle:
        @staticmethod
        def error(*args, **kwargs) -> None:
            return None

        @staticmethod
        def debug(*args, **kwargs) -> None:
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

    api_mod.ApiHandler = ApiHandler
    api_mod.Request = Request
    api_mod.Response = Response
    history_mod.RawMessage = raw_message
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
    sys.modules["helpers.history"] = history_mod
    sys.modules["helpers.login"] = login_mod
    sys.modules["helpers.plugins"] = plugins_mod
    sys.modules["helpers.print_style"] = print_style_mod
    sys.modules["helpers.tool"] = tool_mod
    sys.modules["helpers.ws"] = ws_mod
    sys.modules["helpers.ws_manager"] = ws_manager_mod

    helpers_pkg.api = api_mod
    helpers_pkg.history = history_mod
    helpers_pkg.login = login_mod
    helpers_pkg.plugins = plugins_mod
    helpers_pkg.print_style = print_style_mod
    helpers_pkg.tool = tool_mod
    helpers_pkg.ws = ws_mod
    helpers_pkg.ws_manager = ws_manager_mod

    for module_name in (
        "helpers.settings",
        "helpers.subagents",
        "helpers.skills",
        "helpers.files",
        "helpers.projects",
        "helpers.runtime",
        "plugins._model_config.helpers.model_config",
        "plugins._chat_compaction.helpers.compactor",
    ):
        sys.modules[module_name] = types.ModuleType(module_name)

    plugins_pkg._model_config = sys.modules["plugins._model_config"]
    plugins_pkg._chat_compaction = sys.modules["plugins._chat_compaction"]


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


def _load_code_execution_remote_tool(*, exec_handler):
    shared_ws_manager = _FakeExecWsManager(exec_handler=exec_handler)
    _install_fake_helpers(shared_ws_manager=shared_ws_manager)
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


def test_capabilities_advertise_current_ws_contract() -> None:
    _install_fake_helpers()
    _reload("plugins._a0_connector.api.v1.base")
    capabilities_mod = _reload("plugins._a0_connector.api.v1.capabilities")

    payload = asyncio.run(capabilities_mod.Capabilities(None, None).process({}, object()))

    assert payload["protocol"] == "a0-connector.v1"
    assert payload["auth"] == ["session"]
    assert payload["auth_required"] is False
    assert payload["websocket_namespace"] == "/ws"
    assert payload["websocket_handlers"] == ["plugins/_a0_connector/ws_connector"]
    assert {"pause", "nudge", "remote_file_tree", "code_execution_remote", "computer_use_remote"} <= set(payload["features"])
    assert {"settings_get", "settings_set", "agents_list", "model_switcher", "compact_chat"} <= set(payload["features"])


def test_capabilities_reflect_core_login_requirement() -> None:
    _install_fake_helpers(auth_required=True)
    _reload("plugins._a0_connector.api.v1.base")
    capabilities_mod = _reload("plugins._a0_connector.api.v1.capabilities")

    payload = asyncio.run(capabilities_mod.Capabilities(None, None).process({}, object()))

    assert payload["auth_required"] is True


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
    assert "remote_file_tree" in payload["features"]
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
                    "trust_mode": "persistent",
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
        "trust_mode": "persistent",
        "artifact_root": "/a0/tmp/_a0_connector/computer_use",
        "backend_id": "wayland",
        "backend_family": "linux",
        "features": ["inline-png-capture", "pointer-injection"],
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


def test_computer_use_remote_guidance_is_injected_as_extras_when_enabled_cli_is_available() -> None:
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
        "_77_include_computer_use_remote"
    )

    sid = "sid-computer-use"
    context_id = "ctx-computer-use"
    ws_runtime_mod.register_sid(sid)
    ws_runtime_mod.subscribe_sid_to_context(sid, context_id)
    ws_runtime_mod.store_sid_computer_use_metadata(
        sid,
        {
            "supported": True,
            "enabled": True,
            "trust_mode": "persistent",
            "artifact_root": "/a0/tmp/_a0_connector/computer_use",
            "backend_id": "wayland",
            "backend_family": "linux",
            "features": ["inline-png-capture", "pointer-injection"],
            "support_reason": "Wayland portal backend is available.",
        },
    )

    class FakeContext:
        id = context_id

    class FakeAgent:
        context = FakeContext()

        def read_prompt(self, file: str, **kwargs) -> str:
            assert file == "agent.extras.computer_use_remote.md"
            return (
                "COMPUTER_USE_EXTRAS\n"
                f"{kwargs['backend']}\n"
                f"{kwargs['trust_mode']}\n"
                f"{kwargs['features']}\n"
                f"{kwargs['support_reason']}"
            )

    loop_data = FakeLoopData()
    loop_data.system.append("static system prompt")

    asyncio.run(
        include_mod.IncludeComputerUseRemote(agent=FakeAgent()).execute(loop_data=loop_data)
    )

    assert loop_data.system == ["static system prompt"]
    assert set(loop_data.extras_temporary) == {"computer_use_remote"}
    prompt = loop_data.extras_temporary["computer_use_remote"]
    assert "COMPUTER_USE_EXTRAS" in prompt
    assert "wayland/linux" in prompt
    assert "persistent" in prompt
    assert "inline-png-capture, pointer-injection" in prompt


def test_computer_use_remote_guidance_is_not_injected_without_enabled_cli() -> None:
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
        "_77_include_computer_use_remote"
    )

    sid = "sid-disabled"
    context_id = "ctx-computer-use"
    ws_runtime_mod.register_sid(sid)
    ws_runtime_mod.subscribe_sid_to_context(sid, context_id)
    ws_runtime_mod.store_sid_computer_use_metadata(
        sid,
        {
            "supported": True,
            "enabled": False,
            "trust_mode": "persistent",
            "artifact_root": "/a0/tmp/_a0_connector/computer_use",
        },
    )

    class FakeContext:
        id = context_id

    class FakeAgent:
        context = FakeContext()

        def read_prompt(self, file: str, **kwargs) -> str:
            raise AssertionError(f"read_prompt should not be called, got {file!r}")

    loop_data = FakeLoopData()
    loop_data.system.append("static system prompt")

    asyncio.run(
        include_mod.IncludeComputerUseRemote(agent=FakeAgent()).execute(loop_data=loop_data)
    )

    assert loop_data.system == ["static system prompt"]
    assert loop_data.extras_temporary == {}


def test_code_execution_remote_guidance_is_injected_as_extras_when_cli_is_available() -> None:
    _install_fake_helpers(
        code_execution_config={
            "code_exec_first_output_timeout": 12,
            "code_exec_between_output_timeout": 7,
            "code_exec_max_exec_timeout": 99,
            "code_exec_dialog_timeout": 3,
            "output_first_output_timeout": 33,
            "output_between_output_timeout": 21,
            "output_max_exec_timeout": 120,
            "output_dialog_timeout": 4,
            "prompt_patterns": ["PS .+> ?$"],
            "dialog_patterns": ["yes/no"],
        }
    )
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
        "_78_include_code_execution_remote"
    )

    sid = "sid-exec"
    context_id = "ctx-exec"
    ws_runtime_mod.register_sid(sid)
    ws_runtime_mod.subscribe_sid_to_context(sid, context_id)
    ws_runtime_mod.store_sid_remote_file_metadata(
        sid,
        {
            "enabled": True,
            "write_enabled": True,
            "mode": "read_write",
        },
    )
    ws_runtime_mod.store_sid_remote_exec_metadata(sid, {"enabled": True})

    class FakeContext:
        id = context_id

    class FakeAgent:
        context = FakeContext()

        def read_prompt(self, file: str, **kwargs) -> str:
            assert file == "agent.extras.code_execution_remote.md"
            return (
                "CODE_EXEC_REMOTE_EXTRAS\n"
                f"{kwargs['access_mode']}\n"
                f"{kwargs['write_runtime_guidance']}\n"
                f"{kwargs['write_runtime_examples']}\n"
                f"{kwargs['code_exec_timeouts']}\n"
                f"{kwargs['output_timeouts']}\n"
                f"{kwargs['prompt_patterns']}\n"
                f"{kwargs['dialog_patterns']}"
            )

    loop_data = FakeLoopData()
    loop_data.system.append("static system prompt")

    asyncio.run(
        include_mod.IncludeCodeExecutionRemote(agent=FakeAgent()).execute(loop_data=loop_data)
    )

    assert loop_data.system == ["static system prompt"]
    assert set(loop_data.extras_temporary) == {"code_execution_remote"}
    prompt = loop_data.extras_temporary["code_execution_remote"]
    assert "CODE_EXEC_REMOTE_EXTRAS" in prompt
    assert "Read&Write" in prompt
    assert '"runtime": "terminal"' in prompt
    assert '"code": "pwd"' in prompt
    assert "first_output_timeout=12" in prompt
    assert "max_exec_timeout=120" in prompt
    assert "PS .+> ?$" in prompt
    assert "yes/no" in prompt


def test_code_execution_remote_guidance_reflects_read_only_mode() -> None:
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
        "_78_include_code_execution_remote"
    )

    sid = "sid-exec"
    context_id = "ctx-exec"
    ws_runtime_mod.register_sid(sid)
    ws_runtime_mod.subscribe_sid_to_context(sid, context_id)
    ws_runtime_mod.store_sid_remote_file_metadata(
        sid,
        {
            "enabled": True,
            "write_enabled": False,
            "mode": "read_only",
        },
    )
    ws_runtime_mod.store_sid_remote_exec_metadata(sid, {"enabled": True})

    class FakeContext:
        id = context_id

    class FakeAgent:
        context = FakeContext()

        def read_prompt(self, file: str, **kwargs) -> str:
            assert file == "agent.extras.code_execution_remote.md"
            return (
                "CODE_EXEC_REMOTE_EXTRAS\n"
                f"{kwargs['access_mode']}\n"
                f"{kwargs['write_runtime_guidance']}\n"
                f"{kwargs['write_runtime_examples']}"
            )

    loop_data = FakeLoopData()
    loop_data.system.append("static system prompt")

    asyncio.run(
        include_mod.IncludeCodeExecutionRemote(agent=FakeAgent()).execute(loop_data=loop_data)
    )

    prompt = loop_data.extras_temporary["code_execution_remote"]
    assert "Read only" in prompt
    assert "Press F3" in prompt
    assert '"runtime": "terminal"' not in prompt


def test_code_execution_remote_guidance_is_not_injected_without_cli() -> None:
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
        "_78_include_code_execution_remote"
    )

    class FakeContext:
        id = "ctx-exec"

    class FakeAgent:
        context = FakeContext()

        def read_prompt(self, file: str, **kwargs) -> str:
            raise AssertionError(f"read_prompt should not be called, got {file!r}")

    loop_data = FakeLoopData()
    loop_data.system.append("static system prompt")

    asyncio.run(
        include_mod.IncludeCodeExecutionRemote(agent=FakeAgent()).execute(loop_data=loop_data)
    )

    assert loop_data.system == ["static system prompt"]
    assert loop_data.extras_temporary == {}


def test_text_editor_remote_guidance_is_injected_as_extras_when_cli_is_available() -> None:
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
        "_79_include_text_editor_remote"
    )

    sid = "sid-editor"
    context_id = "ctx-editor"
    ws_runtime_mod.register_sid(sid)
    ws_runtime_mod.subscribe_sid_to_context(sid, context_id)
    ws_runtime_mod.store_sid_remote_file_metadata(
        sid,
        {
            "enabled": True,
            "write_enabled": False,
            "mode": "read_only",
        },
    )

    class FakeContext:
        id = context_id

    class FakeAgent:
        context = FakeContext()

        def read_prompt(self, file: str, **kwargs) -> str:
            assert file == "agent.extras.text_editor_remote.md"
            return (
                "TEXT_EDITOR_REMOTE_EXTRAS\n"
                f"{kwargs['access_mode']}\n"
                f"{kwargs['write_guidance']}\n"
                f"{kwargs['write_examples']}"
            )

    loop_data = FakeLoopData()
    loop_data.system.append("static system prompt")

    asyncio.run(
        include_mod.IncludeTextEditorRemote(agent=FakeAgent()).execute(loop_data=loop_data)
    )

    assert loop_data.system == ["static system prompt"]
    prompt = loop_data.extras_temporary["text_editor_remote"]
    assert "TEXT_EDITOR_REMOTE_EXTRAS" in prompt
    assert "Read only" in prompt
    assert "Press F3" in prompt


def test_text_editor_remote_guidance_is_not_injected_without_cli() -> None:
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
        "_79_include_text_editor_remote"
    )

    class FakeContext:
        id = "ctx-editor"

    class FakeAgent:
        context = FakeContext()

        def read_prompt(self, file: str, **kwargs) -> str:
            raise AssertionError(f"read_prompt should not be called, got {file!r}")

    loop_data = FakeLoopData()
    loop_data.system.append("static system prompt")

    asyncio.run(
        include_mod.IncludeTextEditorRemote(agent=FakeAgent()).execute(loop_data=loop_data)
    )

    assert loop_data.system == ["static system prompt"]
    assert loop_data.extras_temporary == {}


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
        {"supported": True, "enabled": False, "trust_mode": "persistent", "artifact_root": "/a0/tmp"},
    )
    ws_runtime_mod.store_sid_computer_use_metadata(
        "sid-unsupported",
        {"supported": False, "enabled": True, "trust_mode": "persistent", "artifact_root": "/a0/tmp"},
    )
    ws_runtime_mod.store_sid_computer_use_metadata(
        "sid-enabled",
        {"supported": True, "enabled": True, "trust_mode": "persistent", "artifact_root": "/a0/tmp"},
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
            "trust_mode": "persistent",
            "artifact_root": "/a0/tmp/_a0_connector/computer_use",
        },
    )

    response = asyncio.run(
        _create_computer_use_remote(tool_mod, agent, action="status").execute()
    )

    assert "no subscribed CLI" in response.message


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
            "trust_mode": "persistent",
            "artifact_root": "/a0/tmp/_a0_connector/computer_use",
        },
    )

    response = asyncio.run(
        _create_computer_use_remote(tool_mod, agent, action="capture", session_id="sess-1").execute()
    )

    assert response.message == "Current screen attached."
    assert shared_ws_manager.calls[0]["payload"]["action"] == "capture"
    assert len(agent.history_messages) == 1
    raw_message = agent.history_messages[0]["content"]
    assert raw_message["preview"] == "Computer-use capture 1x1."
    assert raw_message["raw_content"][1]["type"] == "image_url"
    assert raw_message["raw_content"][1]["image_url"]["url"] == str(image_path)


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
            "trust_mode": "persistent",
            "artifact_root": "/a0/tmp/_a0_connector/computer_use",
        },
    )

    response = asyncio.run(
        _create_computer_use_remote(tool_mod, agent, action="capture", session_id="sess-1").execute()
    )

    assert response.message == "Current screen attached."
    assert [call["payload"]["action"] for call in shared_ws_manager.calls] == ["capture"]
    assert len(agent.history_messages) == 1
    raw_message = agent.history_messages[0]["content"]
    assert raw_message["raw_content"][1]["image_url"]["url"] == str(image_path)


def test_computer_use_remote_start_session_auto_refreshes_screen(tmp_path: Path) -> None:
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
            "trust_mode": "persistent",
            "artifact_root": "/a0/tmp/_a0_connector/computer_use",
        },
    )

    response = asyncio.run(
        _create_computer_use_remote(tool_mod, agent, action="start_session").execute()
    )

    assert response.message == "Computer-use session started: session_id=sess-1 size=1x1 Latest screen attached."
    assert [call["payload"]["action"] for call in shared_ws_manager.calls] == ["start_session", "capture"]
    assert len(agent.history_messages) == 1


def test_computer_use_remote_click_auto_refreshes_screen(tmp_path: Path) -> None:
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
            "trust_mode": "persistent",
            "artifact_root": "/a0/tmp/_a0_connector/computer_use",
        },
    )

    response = asyncio.run(
        _create_computer_use_remote(tool_mod, agent, action="click", session_id="sess-1").execute()
    )

    assert response.message == "Clicked left button 1 time(s). Latest screen attached."
    assert [call["payload"]["action"] for call in shared_ws_manager.calls] == ["click", "capture"]
    assert len(agent.history_messages) == 1


def test_computer_use_remote_type_submit_sends_submit_flag_and_auto_refreshes_screen(tmp_path: Path) -> None:
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
            "trust_mode": "persistent",
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

    assert response.message == "Typed 21 character(s) and submitted. Latest screen attached."
    assert shared_ws_manager.calls[0]["payload"]["submit"] is True
    assert [call["payload"]["action"] for call in shared_ws_manager.calls] == ["type", "capture"]
    assert len(agent.history_messages) == 1


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
            "trust_mode": "persistent",
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
    ws_runtime_mod.register_sid("sid-cli")
    ws_runtime_mod.subscribe_sid_to_context("sid-cli", agent.context.id)

    response = asyncio.run(
        _create_text_editor_remote(
            tool_mod,
            agent,
            op="patch",
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
    ws_runtime_mod.register_sid("sid-cli")
    ws_runtime_mod.subscribe_sid_to_context("sid-cli", agent.context.id)

    first_patch = asyncio.run(
        _create_text_editor_remote(
            tool_mod,
            agent,
            op="patch",
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
            op="patch",
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
    ws_runtime_mod.register_sid("sid-cli")
    ws_runtime_mod.subscribe_sid_to_context("sid-cli", agent.context.id)

    asyncio.run(
        _create_text_editor_remote(
            tool_mod,
            agent,
            op="read",
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
            op="patch",
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
    ws_runtime_mod.register_sid("sid-cli")
    ws_runtime_mod.subscribe_sid_to_context("sid-cli", agent.context.id)

    write_response = asyncio.run(
        _create_text_editor_remote(
            tool_mod,
            agent,
            op="write",
            path=str(target),
            content="line-1\nline-2\n",
        ).execute()
    )
    patch_response = asyncio.run(
        _create_text_editor_remote(
            tool_mod,
            agent,
            op="patch",
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
    ws_runtime_mod.register_sid("sid-cli")
    ws_runtime_mod.subscribe_sid_to_context("sid-cli", agent.context.id)

    asyncio.run(
        _create_text_editor_remote(
            tool_mod,
            agent,
            op="read",
            path=str(target),
            line_from=1,
            line_to=3,
        ).execute()
    )
    first_patch = asyncio.run(
        _create_text_editor_remote(
            tool_mod,
            agent,
            op="patch",
            path=str(target),
            edits=[{"from": 2, "to": 2, "content": "line-2a\n"}],
        ).execute()
    )
    second_patch = asyncio.run(
        _create_text_editor_remote(
            tool_mod,
            agent,
            op="patch",
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
    ws_runtime_mod.register_sid("sid-cli")
    ws_runtime_mod.subscribe_sid_to_context("sid-cli", agent.context.id)

    asyncio.run(
        _create_text_editor_remote(
            tool_mod,
            agent,
            op="read",
            path=str(target),
            line_from=1,
            line_to=3,
        ).execute()
    )
    first_patch = asyncio.run(
        _create_text_editor_remote(
            tool_mod,
            agent,
            op="patch",
            path=str(target),
            edits=[{"from": 2, "content": "inserted\n"}],
        ).execute()
    )
    second_patch = asyncio.run(
        _create_text_editor_remote(
            tool_mod,
            agent,
            op="patch",
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
    ws_runtime_mod.register_sid("sid-cli")
    ws_runtime_mod.subscribe_sid_to_context("sid-cli", agent.context.id)

    response = asyncio.run(
        _create_text_editor_remote(
            tool_mod,
            agent,
            op="patch",
            path=str(target),
            edits=[{"from": 2, "to": 2, "content": "line-2-updated\n"}],
        ).execute()
    )

    assert "Upgrade the CLI" in response.message
    assert shared_ws_manager.ops == ["stat"]
    assert target.read_text(encoding="utf-8") == "line-1\nline-2\n"
