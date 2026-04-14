from __future__ import annotations

import asyncio
import importlib
import os
import sys
import types
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = PROJECT_ROOT / "plugin"
if not (PLUGIN_ROOT / "_a0_connector").exists():
    sibling_root = PROJECT_ROOT.parent / "agent-zero" / "plugins"
    if (sibling_root / "_a0_connector").exists():
        PLUGIN_ROOT = sibling_root

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent_zero_cli.remote_files import RemoteFileUtility


def _purge_modules() -> None:
    prefixes = (
        "agent",
        "helpers",
        "plugins",
    )
    for name in list(sys.modules):
        if name.startswith(prefixes):
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

    api_mod.ApiHandler = ApiHandler
    api_mod.Request = Request
    api_mod.Response = Response
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
    sys.modules["helpers.login"] = login_mod
    sys.modules["helpers.plugins"] = plugins_mod
    sys.modules["helpers.print_style"] = print_style_mod
    sys.modules["helpers.tool"] = tool_mod
    sys.modules["helpers.ws"] = ws_mod
    sys.modules["helpers.ws_manager"] = ws_manager_mod

    helpers_pkg.api = api_mod
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
        ws_runtime_mod._remote_tree_snapshots.clear()


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


class _FakeRemoteAgent:
    def __init__(self, *, context_id: str = "ctx-1") -> None:
        self.context = types.SimpleNamespace(id=context_id)
        self.data: dict[str, object] = {}

    def read_prompt(self, file: str, **kwargs) -> str:
        path = kwargs.get("path", "")
        return f"{file}::{path}"


def _load_text_editor_remote_tool(*, file_op_handler):
    shared_ws_manager = _FakeCliWsManager(file_op_handler=file_op_handler)
    _install_fake_helpers(shared_ws_manager=shared_ws_manager)
    ws_runtime_mod = _reload("plugins._a0_connector.helpers.ws_runtime")
    _reset_ws_runtime_state(ws_runtime_mod)
    shared_ws_manager.ws_runtime_mod = ws_runtime_mod
    tool_mod = _reload("plugins._a0_connector.tools.text_editor_remote")
    return shared_ws_manager, ws_runtime_mod, tool_mod


def _create_text_editor_remote(
    tool_mod,
    agent: _FakeRemoteAgent,
    **args,
):
    return tool_mod.TextEditorRemote(agent=agent, args=args)


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
    assert {"pause", "nudge", "remote_file_tree", "code_execution_remote"} <= set(payload["features"])
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
    assert payload["exec_config"]["version"] == 1
    assert payload["exec_config"]["code_exec_timeouts"]["first_output_timeout"] == 12
    assert payload["exec_config"]["output_timeouts"]["max_exec_timeout"] == 120
    assert payload["exec_config"]["prompt_patterns"] == ["PS .+> ?$"]
    assert payload["exec_config"]["dialog_patterns"] == ["yes/no"]


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
