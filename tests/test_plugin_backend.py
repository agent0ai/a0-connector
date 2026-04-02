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


def _install_fake_helpers(
    *,
    auth_login: str = "",
    auth_password: str = "",
    mcp_server_token: str = "test-token-abc",
) -> None:
    _install_test_plugin_namespace()

    helpers_pkg = types.ModuleType("helpers")
    api_mod = types.ModuleType("helpers.api")
    print_style_mod = types.ModuleType("helpers.print_style")
    security_mod = types.ModuleType("helpers.security")
    dotenv_mod = types.ModuleType("helpers.dotenv")
    settings_mod = types.ModuleType("helpers.settings")

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

    _dotenv_store = {
        "AUTH_LOGIN": auth_login,
        "AUTH_PASSWORD": auth_password,
    }
    dotenv_mod.KEY_AUTH_LOGIN = "AUTH_LOGIN"
    dotenv_mod.KEY_AUTH_PASSWORD = "AUTH_PASSWORD"
    dotenv_mod.get_dotenv_value = lambda key, default=None: _dotenv_store.get(key) or default

    settings_mod.get_settings = lambda: {"mcp_server_token": mcp_server_token}

    sys.modules["helpers"] = helpers_pkg
    sys.modules["helpers.api"] = api_mod
    sys.modules["helpers.print_style"] = print_style_mod
    sys.modules["helpers.security"] = security_mod
    sys.modules["helpers.dotenv"] = dotenv_mod
    sys.modules["helpers.settings"] = settings_mod


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
    assert payload["auth"] == ["api_key", "login"]
    assert payload["websocket_namespace"] == "/ws"
    assert payload["websocket_handlers"] == ["plugins/a0_connector/ws_connector"]
    assert "connector_login" in payload["features"]
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


def test_connector_login_returns_token_when_no_auth_configured() -> None:
    _install_fake_helpers(auth_login="", mcp_server_token="open-token")

    _reload("usr.plugins.a0_connector.api.v1.base")
    login_mod = _reload("usr.plugins.a0_connector.api.v1.connector_login")
    handler = login_mod.ConnectorLogin(None, None)

    result = asyncio.run(handler.process({}, object()))

    assert result == {"api_key": "open-token"}
    assert login_mod.ConnectorLogin.requires_api_key() is False
    assert login_mod.ConnectorLogin.requires_auth() is False


def test_connector_login_returns_token_on_valid_credentials() -> None:
    _install_fake_helpers(
        auth_login="admin",
        auth_password="secret",
        mcp_server_token="protected-token",
    )

    _reload("usr.plugins.a0_connector.api.v1.base")
    login_mod = _reload("usr.plugins.a0_connector.api.v1.connector_login")
    handler = login_mod.ConnectorLogin(None, None)

    result = asyncio.run(
        handler.process({"username": "admin", "password": "secret"}, object())
    )

    assert result == {"api_key": "protected-token"}


def test_connector_login_rejects_invalid_credentials() -> None:
    _install_fake_helpers(
        auth_login="admin",
        auth_password="secret",
        mcp_server_token="protected-token",
    )

    _reload("usr.plugins.a0_connector.api.v1.base")
    login_mod = _reload("usr.plugins.a0_connector.api.v1.connector_login")
    handler = login_mod.ConnectorLogin(None, None)

    result = asyncio.run(
        handler.process({"username": "admin", "password": "wrong"}, object())
    )

    assert hasattr(result, "status")
    assert result.status == 401


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
