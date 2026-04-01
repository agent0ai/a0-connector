import importlib
import sys
import types
import asyncio
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = PROJECT_ROOT / "plugin"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _install_test_plugin_namespace() -> None:
    usr_pkg = types.ModuleType("usr")
    usr_pkg.__path__ = [str(PROJECT_ROOT)]
    usr_plugins_pkg = types.ModuleType("usr.plugins")
    usr_plugins_pkg.__path__ = [str(PLUGIN_ROOT)]

    sys.modules["usr"] = usr_pkg
    sys.modules["usr.plugins"] = usr_plugins_pkg


def _install_fake_helpers() -> None:
    _install_test_plugin_namespace()

    helpers_pkg = types.ModuleType("helpers")
    api_mod = types.ModuleType("helpers.api")
    print_style_mod = types.ModuleType("helpers.print_style")
    security_mod = types.ModuleType("helpers.security")

    class ApiHandler:
        def __init__(self, app=None, thread_lock=None) -> None:
            self.app = app
            self.thread_lock = thread_lock

        @classmethod
        def requires_auth(cls) -> bool:
            return True

        @classmethod
        def requires_csrf(cls) -> bool:
            return True

        @classmethod
        def requires_api_key(cls) -> bool:
            return False

    class Request:
        pass

    class Response:
        def __init__(
            self,
            response: str = "",
            status: int = 200,
            mimetype: str = "application/json",
        ) -> None:
            self.response = response
            self.status = status
            self.mimetype = mimetype

    class PrintStyle:
        @staticmethod
        def error(*args, **kwargs) -> None:
            return None

    api_mod.ApiHandler = ApiHandler
    api_mod.Request = Request
    api_mod.Response = Response
    print_style_mod.PrintStyle = PrintStyle
    security_mod.safe_filename = lambda value: value

    sys.modules["helpers"] = helpers_pkg
    sys.modules["helpers.api"] = api_mod
    sys.modules["helpers.print_style"] = print_style_mod
    sys.modules["helpers.security"] = security_mod


def _reload(module_name: str):
    sys.modules.pop(module_name, None)
    return importlib.import_module(module_name)


def test_capabilities_advertise_current_ws_contract() -> None:
    _install_fake_helpers()

    _reload("usr.plugins.a0_connector.api.v1.base")
    capabilities_mod = _reload("usr.plugins.a0_connector.api.v1.capabilities")
    handler = capabilities_mod.Capabilities(None, None)

    payload = asyncio.run(handler.process({}, object()))

    assert payload["protocol"] == "a0-connector.v1"
    assert payload["auth"] == ["api_key"]
    assert payload["websocket_namespace"] == "/ws"
    assert payload["websocket_handlers"] == ["plugins/a0_connector/ws_connector"]
    assert capabilities_mod.Capabilities.requires_api_key() is False


def test_protected_handlers_require_api_key_only() -> None:
    _install_fake_helpers()

    _reload("usr.plugins.a0_connector.api.v1.base")
    modules = [
        "usr.plugins.a0_connector.api.v1.chat_create",
        "usr.plugins.a0_connector.api.v1.chat_delete",
        "usr.plugins.a0_connector.api.v1.chat_get",
        "usr.plugins.a0_connector.api.v1.chat_reset",
        "usr.plugins.a0_connector.api.v1.chats_list",
        "usr.plugins.a0_connector.api.v1.log_tail",
        "usr.plugins.a0_connector.api.v1.message_send",
        "usr.plugins.a0_connector.api.v1.projects_list",
    ]
    class_names = [
        "ChatCreate",
        "ChatDelete",
        "ChatGet",
        "ChatReset",
        "ChatsList",
        "LogTail",
        "MessageSend",
        "ProjectsList",
    ]

    for module_name, class_name in zip(modules, class_names, strict=True):
        module = _reload(module_name)
        handler_cls = getattr(module, class_name)
        assert handler_cls.requires_auth() is False
        assert handler_cls.requires_csrf() is False
        assert handler_cls.requires_api_key() is True


@dataclass(frozen=True)
class _FakeLogOutput:
    items: list[dict]
    start: int
    end: int


def test_event_bridge_uses_log_output_cursor() -> None:
    _install_fake_helpers()

    class _FakeLog:
        def output(self, start=None, end=None):
            assert start == 5
            return _FakeLogOutput(
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
                start=5,
                end=7,
            )

    class _FakeContext:
        log = _FakeLog()

    agent_mod = types.ModuleType("agent")
    agent_mod.AgentContext = types.SimpleNamespace(get=lambda context_id: _FakeContext())
    sys.modules["agent"] = agent_mod

    bridge_mod = _reload("usr.plugins.a0_connector.helpers.event_bridge")

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
