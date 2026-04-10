from __future__ import annotations

import asyncio
import importlib
import sys
import types
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = PROJECT_ROOT / "plugin"

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


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


def _install_fake_helpers(*, auth_required: bool = False) -> None:
    plugins_pkg = _make_package("plugins", path=PLUGIN_ROOT)
    _make_package("plugins._model_config")
    _make_package("plugins._model_config.helpers")
    _make_package("plugins._chat_compaction")
    _make_package("plugins._chat_compaction.helpers")

    helpers_pkg = _make_package("helpers")
    api_mod = types.ModuleType("helpers.api")
    login_mod = types.ModuleType("helpers.login")
    print_style_mod = types.ModuleType("helpers.print_style")
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

    class PrintStyle:
        @staticmethod
        def error(*args, **kwargs) -> None:
            return None

        @staticmethod
        def debug(*args, **kwargs) -> None:
            return None

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

    api_mod.ApiHandler = ApiHandler
    api_mod.Request = Request
    api_mod.Response = Response
    login_mod.is_login_required = lambda: auth_required
    print_style_mod.PrintStyle = PrintStyle
    ws_mod.WsHandler = WsHandler
    ws_manager_mod.WsResult = WsResult

    sys.modules["helpers.api"] = api_mod
    sys.modules["helpers.login"] = login_mod
    sys.modules["helpers.print_style"] = print_style_mod
    sys.modules["helpers.ws"] = ws_mod
    sys.modules["helpers.ws_manager"] = ws_manager_mod

    helpers_pkg.api = api_mod
    helpers_pkg.login = login_mod
    helpers_pkg.print_style = print_style_mod
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
    assert {"settings_get", "model_switcher", "compact_chat"} <= set(payload["features"])


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
    _install_fake_helpers()
    ws_connector_mod = _reload("plugins._a0_connector.api.ws_connector")

    payload = asyncio.run(ws_connector_mod.WsConnector(None, None).process("connector_hello", {}, "sid-1"))

    assert payload["protocol"] == "a0-connector.v1"
    assert "remote_file_tree" in payload["features"]
    assert "code_execution_remote" in payload["features"]


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
