import importlib
import sys
import types
import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import AsyncMock

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DOCKERVOLUME_ROOT = PROJECT_ROOT.parent / "dockervolume"
PLUGIN_ROOT = PROJECT_ROOT / "plugin"
if not (PLUGIN_ROOT / "a0_connector").exists():
    PLUGIN_ROOT = DOCKERVOLUME_ROOT / "usr" / "plugins"

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if DOCKERVOLUME_ROOT.exists() and str(DOCKERVOLUME_ROOT) not in sys.path:
    sys.path.insert(0, str(DOCKERVOLUME_ROOT))


def _install_test_plugin_namespace() -> None:
    usr_pkg = types.ModuleType("usr")
    usr_pkg.__path__ = [str(DOCKERVOLUME_ROOT)]
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
    extension_mod = types.ModuleType("helpers.extension")
    ws_mod = types.ModuleType("helpers.ws")
    ws_manager_mod = types.ModuleType("helpers.ws_manager")
    settings_mod = types.ModuleType("helpers.settings")
    runtime_mod = types.ModuleType("helpers.runtime")
    projects_mod = types.ModuleType("helpers.projects")
    files_mod = types.ModuleType("helpers.files")
    persist_chat_mod = types.ModuleType("helpers.persist_chat")
    subagents_mod = types.ModuleType("helpers.subagents")
    skills_mod = types.ModuleType("helpers.skills")
    state_monitor_mod = types.ModuleType("helpers.state_monitor_integration")
    model_config_mod = types.ModuleType("plugins._model_config.helpers.model_config")
    compactor_mod = types.ModuleType("plugins._chat_compaction.helpers.compactor")

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

        @staticmethod
        def debug(*args, **kwargs) -> None:
            return None

    class Extension:
        def __init__(self, *args, **kwargs) -> None:
            self.agent = None

    class WsResult(dict):
        @classmethod
        def error(
            cls,
            *,
            code: str,
            message: str,
            correlation_id: str | None = None,
        ):
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

    class WsHandler:
        def __init__(self, app=None, thread_lock=None) -> None:
            self.app = app
            self.thread_lock = thread_lock

        async def emit_to(
            self,
            sid: str,
            event: str,
            payload: dict,
            correlation_id: str | None = None,
        ) -> None:
            return None

    class ConnectionNotFoundError(RuntimeError):
        pass

    class _FakeSharedWsManager:
        async def emit_to(self, *args, **kwargs) -> None:
            return None

    api_mod.ApiHandler = ApiHandler
    api_mod.Request = Request
    api_mod.Response = Response
    print_style_mod.PrintStyle = PrintStyle
    extension_mod.Extension = Extension
    ws_mod.WsHandler = WsHandler
    ws_mod.NAMESPACE = "/ws"
    ws_manager_mod.WsResult = WsResult
    ws_manager_mod.ConnectionNotFoundError = ConnectionNotFoundError
    ws_manager_mod.get_shared_ws_manager = lambda: _FakeSharedWsManager()
    security_mod.safe_filename = lambda value: value
    runtime_mod.is_development = lambda: True
    runtime_mod.is_dockerized = lambda: False
    projects_mod.get_project_folder = lambda project_name: f"/projects/{project_name}"
    projects_mod.get_project_meta = (
        lambda project_name, *parts: "/projects/" + project_name + "/" + "/".join(parts)
    )
    files_mod.normalize_a0_path = lambda value: value
    files_mod.is_in_dir = lambda path, root: str(path).startswith(str(root))
    files_mod.get_abs_path = lambda *parts: "/".join(str(part).strip("/") for part in parts if str(part))
    persist_chat_mod.save_tmp_chat = lambda context: None
    settings_state = {
        "agent_profile": "default",
        "agent_knowledge_subdir": "knowledge",
        "chat_inherit_project": True,
        "auth_login": "admin",
        "auth_password": "secret",
        "workdir_path": "/workdir",
        "workdir_show": True,
        "workdir_max_depth": 3,
        "workdir_max_files": 4,
        "workdir_max_folders": 5,
        "workdir_max_lines": 6,
        "update_check_enabled": True,
        "websocket_server_restart_enabled": True,
        "uvicorn_access_logs_enabled": False,
        "mcp_server_enabled": True,
        "mcp_client_init_timeout": 10,
        "mcp_client_tool_timeout": 20,
        "a2a_server_enabled": False,
    }
    settings_mod.Settings = dict
    settings_mod.get_settings = lambda: {"mcp_server_token": mcp_server_token, **dict(settings_state)}
    settings_mod.convert_out = lambda backend: {
        "settings": dict(backend),
        "additional": {"runtime_settings": {}},
    }
    settings_mod.convert_in = lambda frontend: dict(frontend)
    settings_mod.set_settings = lambda backend: settings_state.update(backend) or dict(settings_state)
    subagents_mod.get_all_agents_list = lambda: [
        {"key": "default", "label": "Default"},
        {"key": "hacker", "label": "Hacker"},
    ]

    class _FakeSkill:
        def __init__(self, name: str, description: str, path: str) -> None:
            self.name = name
            self.description = description
            self.path = Path(path)

    skills_mod.list_skills = lambda: [
        _FakeSkill("alpha", "Alpha skill", "/skills/global/alpha"),
        _FakeSkill("beta", "Beta skill", "/skills/global/beta"),
    ]
    deleted_skills: list[str] = []
    skills_mod.delete_skill = lambda skill_path: deleted_skills.append(skill_path)
    state_monitor_mod.mark_dirty_all = lambda reason=None: None

    model_config_state = {
        "allowed": True,
        "chat_model": {"provider": "anthropic", "name": "chat-model"},
        "utility_model": {"provider": "anthropic", "name": "utility-model"},
        "presets": [{"name": "fast", "chat": {"provider": "x", "name": "y"}}],
    }
    model_config_mod.get_presets = lambda: list(model_config_state["presets"])
    model_config_mod.save_presets = lambda presets: model_config_state.__setitem__("presets", list(presets))
    model_config_mod.reset_presets = lambda: model_config_state["presets"]
    model_config_mod.get_preset_by_name = lambda name: next(
        (preset for preset in model_config_state["presets"] if preset.get("name") == name),
        None,
    )
    model_config_mod.is_chat_override_allowed = lambda agent=None: bool(model_config_state["allowed"])
    model_config_mod.get_chat_providers = lambda: [
        {"value": "anthropic", "label": "Anthropic"},
        {"value": "openai", "label": "OpenAI"},
    ]
    model_config_mod.has_provider_api_key = (
        lambda provider, configured_api_key="": provider == "anthropic" or bool(str(configured_api_key or "").strip())
    )

    def _override(agent):
        context = getattr(agent, "_context", None) if agent is not None else None
        if context is None:
            return None
        return context.get_data("chat_model_override")

    def _chat_model(agent=None):
        override = _override(agent)
        if isinstance(override, dict):
            preset_name = override.get("preset_name")
            if preset_name:
                preset = model_config_mod.get_preset_by_name(preset_name) or {}
                chat = preset.get("chat", {})
                if chat.get("provider") or chat.get("name"):
                    return dict(chat)
            chat = override.get("chat", override)
            if isinstance(chat, dict) and (chat.get("provider") or chat.get("name")):
                return dict(chat)
        return dict(model_config_state["chat_model"])

    def _utility_model(agent=None):
        override = _override(agent)
        if isinstance(override, dict):
            preset_name = override.get("preset_name")
            if preset_name:
                preset = model_config_mod.get_preset_by_name(preset_name) or {}
                utility = preset.get("utility", {})
                if utility.get("provider") or utility.get("name"):
                    return dict(utility)
            utility = override.get("utility", {})
            if isinstance(utility, dict) and (utility.get("provider") or utility.get("name")):
                return dict(utility)
        return dict(model_config_state["utility_model"])

    model_config_mod.get_chat_model_config = _chat_model
    model_config_mod.get_utility_model_config = _utility_model

    compaction_state = {
        "stats": {
            "message_count": 3,
            "token_count": 1200,
            "model_name": "chat-model",
            "chat_model_name": "chat-model",
            "utility_model_name": "utility-model",
        }
    }
    compactor_mod.MIN_COMPACTION_TOKENS = 1000
    compactor_mod.get_compaction_stats = AsyncMock(return_value=compaction_state["stats"])
    compactor_mod.run_compaction = AsyncMock(return_value=None)

    _dotenv_store = {
        "AUTH_LOGIN": auth_login,
        "AUTH_PASSWORD": auth_password,
    }
    dotenv_mod.KEY_AUTH_LOGIN = "AUTH_LOGIN"
    dotenv_mod.KEY_AUTH_PASSWORD = "AUTH_PASSWORD"
    dotenv_mod.get_dotenv_value = lambda key, default=None: _dotenv_store.get(key) or default

    sys.modules["helpers"] = helpers_pkg
    sys.modules["helpers.api"] = api_mod
    sys.modules["helpers.print_style"] = print_style_mod
    sys.modules["helpers.extension"] = extension_mod
    sys.modules["helpers.ws"] = ws_mod
    sys.modules["helpers.ws_manager"] = ws_manager_mod
    sys.modules["helpers.security"] = security_mod
    sys.modules["helpers.dotenv"] = dotenv_mod
    sys.modules["helpers.settings"] = settings_mod
    sys.modules["helpers.runtime"] = runtime_mod
    sys.modules["helpers.projects"] = projects_mod
    sys.modules["helpers.files"] = files_mod
    sys.modules["helpers.persist_chat"] = persist_chat_mod
    sys.modules["helpers.subagents"] = subagents_mod
    sys.modules["helpers.skills"] = skills_mod
    sys.modules["helpers.state_monitor_integration"] = state_monitor_mod

    plugins_pkg = types.ModuleType("plugins")
    plugins_pkg.__path__ = [str(DOCKERVOLUME_ROOT / "plugins")]
    model_config_pkg = types.ModuleType("plugins._model_config")
    model_config_pkg.__path__ = [str(DOCKERVOLUME_ROOT / "plugins" / "_model_config")]
    model_config_helpers_pkg = types.ModuleType("plugins._model_config.helpers")
    model_config_helpers_pkg.__path__ = [str(DOCKERVOLUME_ROOT / "plugins" / "_model_config" / "helpers")]
    chat_compaction_pkg = types.ModuleType("plugins._chat_compaction")
    chat_compaction_pkg.__path__ = [str(DOCKERVOLUME_ROOT / "plugins" / "_chat_compaction")]
    chat_compaction_helpers_pkg = types.ModuleType("plugins._chat_compaction.helpers")
    chat_compaction_helpers_pkg.__path__ = [str(DOCKERVOLUME_ROOT / "plugins" / "_chat_compaction" / "helpers")]

    sys.modules["plugins"] = plugins_pkg
    sys.modules["plugins._model_config"] = model_config_pkg
    sys.modules["plugins._model_config.helpers"] = model_config_helpers_pkg
    sys.modules["plugins._model_config.helpers.model_config"] = model_config_mod
    sys.modules["plugins._chat_compaction"] = chat_compaction_pkg
    sys.modules["plugins._chat_compaction.helpers"] = chat_compaction_helpers_pkg
    sys.modules["plugins._chat_compaction.helpers.compactor"] = compactor_mod


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

    _reload("usr.plugins.a0_connector.api.v1.base")
    capabilities_mod = _reload("usr.plugins.a0_connector.api.v1.capabilities")
    handler = capabilities_mod.Capabilities(None, None)

    payload = asyncio.run(handler.process({}, object()))

    assert payload["protocol"] == "a0-connector.v1"
    assert payload["auth"] == ["api_key", "login"]
    assert payload["websocket_namespace"] == "/ws"
    assert payload["websocket_handlers"] == ["plugins/a0_connector/ws_connector"]
    assert "connector_login" in payload["features"]
    assert "token_status" in payload["features"]
    assert "remote_file_tree" in payload["features"]
    assert "code_execution_remote" in payload["features"]
    assert "pause" in payload["features"]
    assert "nudge" in payload["features"]
    assert "settings_get" in payload["features"]
    assert "settings_set" in payload["features"]
    assert "agents_list" in payload["features"]
    assert "skills_list" in payload["features"]
    assert "skills_delete" in payload["features"]
    assert "model_presets" in payload["features"]
    assert "model_switcher" in payload["features"]
    assert "compact_chat" in payload["features"]
    assert capabilities_mod.Capabilities.requires_api_key() is False


def test_capabilities_hide_unsupported_optional_features(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_helpers()

    _reload("usr.plugins.a0_connector.api.v1.base")
    capabilities_mod = _reload("usr.plugins.a0_connector.api.v1.capabilities")

    def fake_module_available(module_name: str) -> bool:
        return module_name not in {
            "helpers.skills",
            "plugins._chat_compaction.helpers.compactor",
        }

    monkeypatch.setattr(capabilities_mod, "_module_available", fake_module_available)

    payload = asyncio.run(capabilities_mod.Capabilities(None, None).process({}, object()))

    assert "skills_list" not in payload["features"]
    assert "skills_delete" not in payload["features"]
    assert "compact_chat" not in payload["features"]
    assert "settings_get" in payload["features"]


def test_protected_handlers_require_api_key_only() -> None:
    _install_fake_helpers()

    _reload("usr.plugins.a0_connector.api.v1.base")
    modules = [
        "usr.plugins.a0_connector.api.v1.chat_create",
        "usr.plugins.a0_connector.api.v1.chat_delete",
        "usr.plugins.a0_connector.api.v1.chat_get",
        "usr.plugins.a0_connector.api.v1.chat_reset",
        "usr.plugins.a0_connector.api.v1.chats_list",
        "usr.plugins.a0_connector.api.v1.pause",
        "usr.plugins.a0_connector.api.v1.nudge",
        "usr.plugins.a0_connector.api.v1.settings_get",
        "usr.plugins.a0_connector.api.v1.settings_set",
        "usr.plugins.a0_connector.api.v1.agents_list",
        "usr.plugins.a0_connector.api.v1.skills_list",
        "usr.plugins.a0_connector.api.v1.skills_delete",
        "usr.plugins.a0_connector.api.v1.model_presets",
        "usr.plugins.a0_connector.api.v1.model_switcher",
        "usr.plugins.a0_connector.api.v1.compact_chat",
        "usr.plugins.a0_connector.api.v1.log_tail",
        "usr.plugins.a0_connector.api.v1.message_send",
        "usr.plugins.a0_connector.api.v1.projects_list",
        "usr.plugins.a0_connector.api.v1.token_status",
    ]
    class_names = [
        "ChatCreate",
        "ChatDelete",
        "ChatGet",
        "ChatReset",
        "ChatsList",
        "Pause",
        "Nudge",
        "SettingsGet",
        "SettingsSet",
        "AgentsList",
        "SkillsList",
        "SkillsDelete",
        "ModelPresets",
        "ModelSwitcher",
        "CompactChat",
        "LogTail",
        "MessageSend",
        "ProjectsList",
        "TokenStatus",
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


def test_settings_round_trip_uses_connector_helpers() -> None:
    _install_fake_helpers()

    settings_get_mod = _reload("usr.plugins.a0_connector.api.v1.settings_get")
    settings_set_mod = _reload("usr.plugins.a0_connector.api.v1.settings_set")

    get_handler = settings_get_mod.SettingsGet(None, None)
    set_handler = settings_set_mod.SettingsSet(None, None)

    got = asyncio.run(get_handler.process({}, object()))
    assert got["settings"]["agent_profile"] == "default"

    updated = asyncio.run(
        set_handler.process({"settings": {"agent_profile": "researcher"}}, object())
    )
    assert updated["settings"]["agent_profile"] == "researcher"


def test_token_status_returns_ctx_window_and_context_limit() -> None:
    _install_fake_helpers()

    token_status_mod = _reload("usr.plugins.a0_connector.api.v1.token_status")
    model_config_mod = sys.modules["plugins._model_config.helpers.model_config"]
    model_config_mod.get_chat_model_config = lambda agent=None: {
        "provider": "anthropic",
        "name": "chat-model",
        "ctx_length": 128000,
    }

    class _FakeAgent:
        DATA_NAME_CTX_WINDOW = "ctx_window"

        def __init__(self) -> None:
            self.data = {"ctx_window": {"text": "prompt", "tokens": 12400}}

        def get_data(self, key: str):
            return self.data.get(key)

    class _FakeContext:
        def __init__(self) -> None:
            self.streaming_agent = None
            self.agent0 = _FakeAgent()

    fake_context = _FakeContext()
    agent_mod = types.ModuleType("agent")
    agent_mod.Agent = _FakeAgent
    agent_mod.AgentContext = types.SimpleNamespace(get=lambda context_id: fake_context if context_id == "ctx-1" else None)
    sys.modules["agent"] = agent_mod

    result = asyncio.run(
        token_status_mod.TokenStatus(None, None).process({"context_id": "ctx-1"}, object())
    )

    assert result == {
        "ok": True,
        "context_id": "ctx-1",
        "token_count": 12400,
        "context_window": 128000,
    }


def test_pause_handler_marks_running_context_paused() -> None:
    _install_fake_helpers()

    class _FakeContext:
        def __init__(self) -> None:
            self.paused = False

        def is_running(self) -> bool:
            return True

    fake_context = _FakeContext()
    agent_mod = types.ModuleType("agent")
    agent_mod.AgentContext = types.SimpleNamespace(get=lambda context_id: fake_context)
    sys.modules["agent"] = agent_mod

    pause_mod = _reload("usr.plugins.a0_connector.api.v1.pause")
    result = asyncio.run(
        pause_mod.Pause(None, None).process({"context_id": "ctx-1", "paused": True}, object())
    )

    assert result == {
        "ok": True,
        "context_id": "ctx-1",
        "paused": True,
        "status": "paused",
        "message": "Agent paused.",
    }
    assert fake_context.paused is True


def test_nudge_handler_starts_nudged_context() -> None:
    _install_fake_helpers()

    class _FakeContext:
        def __init__(self) -> None:
            self.nudged = False
            self.log_entries: list[tuple[str, str]] = []
            self.log = types.SimpleNamespace(log=self._log)

        def is_running(self) -> bool:
            return False

        def nudge(self) -> None:
            self.nudged = True

        def _log(self, *, type: str, content: str) -> None:
            self.log_entries.append((type, content))

    fake_context = _FakeContext()
    agent_mod = types.ModuleType("agent")
    agent_mod.AgentContext = types.SimpleNamespace(get=lambda context_id: fake_context)
    sys.modules["agent"] = agent_mod

    nudge_mod = _reload("usr.plugins.a0_connector.api.v1.nudge")
    result = asyncio.run(
        nudge_mod.Nudge(None, None).process({"context_id": "ctx-1"}, object())
    )

    assert result == {
        "ok": True,
        "context_id": "ctx-1",
        "status": "nudged",
        "message": "Process reset, agent nudged.",
    }
    assert fake_context.nudged is True
    assert fake_context.log_entries == [("info", "Process reset, agent nudged.")]


def test_nudge_handler_allows_running_context() -> None:
    _install_fake_helpers()

    class _FakeContext:
        def __init__(self) -> None:
            self.nudged = False
            self.log = types.SimpleNamespace(log=lambda **kwargs: None)

        def is_running(self) -> bool:
            return True

        def nudge(self) -> None:
            self.nudged = True

    fake_context = _FakeContext()
    agent_mod = types.ModuleType("agent")
    agent_mod.AgentContext = types.SimpleNamespace(get=lambda context_id: fake_context)
    sys.modules["agent"] = agent_mod

    nudge_mod = _reload("usr.plugins.a0_connector.api.v1.nudge")
    result = asyncio.run(
        nudge_mod.Nudge(None, None).process({"context_id": "ctx-1"}, object())
    )

    assert result["ok"] is True
    assert fake_context.nudged is True


def test_agents_skills_and_model_preset_proxy_payloads() -> None:
    _install_fake_helpers()

    agents_mod = _reload("usr.plugins.a0_connector.api.v1.agents_list")
    skills_list_mod = _reload("usr.plugins.a0_connector.api.v1.skills_list")
    skills_delete_mod = _reload("usr.plugins.a0_connector.api.v1.skills_delete")
    presets_mod = _reload("usr.plugins.a0_connector.api.v1.model_presets")

    agents = asyncio.run(agents_mod.AgentsList(None, None).process({}, object()))
    assert agents["data"][0] == {"key": "default", "label": "Default"}

    skills = asyncio.run(skills_list_mod.SkillsList(None, None).process({}, object()))
    assert [item["name"] for item in skills["data"]] == ["alpha", "beta"]

    deleted = asyncio.run(
        skills_delete_mod.SkillsDelete(None, None).process(
            {"skill_path": "/skills/global/alpha"}, object()
        )
    )
    assert deleted["data"]["skill_path"] == "/skills/global/alpha"

    presets = asyncio.run(presets_mod.ModelPresets(None, None).process({}, object()))
    assert presets["presets"][0]["name"] == "fast"


def test_model_switcher_returns_effective_models_and_updates_override() -> None:
    _install_fake_helpers()

    class _FakeContext:
        def __init__(self) -> None:
            self.data = {"chat_model_override": None}
            self.agent0 = types.SimpleNamespace(_context=self)

        def get_data(self, key: str):
            return self.data.get(key)

        def set_data(self, key: str, value):
            self.data[key] = value

    fake_context = _FakeContext()
    agent_mod = types.ModuleType("agent")
    agent_mod.AgentContext = types.SimpleNamespace(get=lambda context_id: fake_context)
    sys.modules["agent"] = agent_mod

    switcher_mod = _reload("usr.plugins.a0_connector.api.v1.model_switcher")
    handler = switcher_mod.ModelSwitcher(None, None)

    initial = asyncio.run(handler.process({"action": "get", "context_id": "ctx-1"}, object()))
    assert initial["allowed"] is True
    assert initial["chat_providers"] == [
        {"value": "anthropic", "label": "Anthropic", "has_api_key": True},
        {"value": "openai", "label": "OpenAI", "has_api_key": False},
    ]
    assert initial["main_model"]["label"] == "anthropic/chat-model"
    assert initial["main_model"]["has_api_key"] is True
    assert initial["utility_model"]["label"] == "anthropic/utility-model"
    assert initial["utility_model"]["has_api_key"] is True
    assert initial["override"] is None

    updated = asyncio.run(
        handler.process({"action": "set_preset", "context_id": "ctx-1", "preset_name": "fast"}, object())
    )
    assert updated["override"] == {"preset_name": "fast"}
    assert updated["main_model"]["label"] == "x/y"
    assert updated["utility_model"]["label"] == "anthropic/utility-model"

    custom = asyncio.run(
        handler.process(
            {
                "action": "set_override",
                "context_id": "ctx-1",
                "main_model": {
                    "provider": "openai",
                    "name": "gpt-4o",
                    "api_base": "https://api.example.main",
                },
                "utility_model": {
                    "provider": "openai",
                    "name": "gpt-4o-mini",
                },
            },
            object(),
        )
    )
    assert custom["override"] == {
        "chat": {
            "provider": "openai",
            "name": "gpt-4o",
            "api_base": "https://api.example.main",
        },
        "utility": {
            "provider": "openai",
            "name": "gpt-4o-mini",
        },
    }
    assert custom["main_model"]["label"] == "openai/gpt-4o"
    assert custom["main_model"]["has_api_key"] is False
    assert custom["utility_model"]["label"] == "openai/gpt-4o-mini"
    assert custom["utility_model"]["has_api_key"] is False

    cleared = asyncio.run(handler.process({"action": "clear", "context_id": "ctx-1"}, object()))
    assert cleared["override"] is None
    assert fake_context.get_data("chat_model_override") is None


def test_compact_chat_returns_stats_and_schedules_compaction() -> None:
    _install_fake_helpers()

    compact_mod = _reload("usr.plugins.a0_connector.api.v1.compact_chat")

    class _FakeLog:
        def __init__(self) -> None:
            self.entries: list[tuple] = []

        def log(self, *args, **kwargs):
            self.entries.append((args, kwargs))

    class _FakeContext:
        def __init__(self) -> None:
            self.log = _FakeLog()
            self.running = False
            self.tasks: list[tuple] = []

        def is_running(self) -> bool:
            return self.running

        def run_task(self, func, *args):
            self.tasks.append((func, args))

    fake_context = _FakeContext()
    agent_mod = types.ModuleType("agent")
    agent_mod.AgentContext = types.SimpleNamespace(get=lambda context_id: fake_context)
    sys.modules["agent"] = agent_mod

    handler = compact_mod.CompactChat(None, None)

    stats = asyncio.run(handler.process({"action": "stats", "context": "ctx-1"}, object()))
    assert stats["stats"]["token_count"] == 1200

    started = asyncio.run(
        handler.process(
            {"action": "compact", "context": "ctx-1", "use_chat_model": "true"},
            object(),
        )
    )
    assert started["message"] == "Compaction started"
    assert fake_context.tasks


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


def test_event_bridge_maps_info_logs_to_standalone_info_events() -> None:
    _install_fake_helpers()

    class _FakeLog:
        def output(self, start=None, end=None):
            return _FakeLogOutput(
                items=[
                    {
                        "no": 8,
                        "type": "info",
                        "content": "Process reset, agent nudged.",
                        "timestamp": "2026-04-01T00:00:00Z",
                    }
                ],
                start=0,
                end=9,
            )

    class _FakeContext:
        log = _FakeLog()

    agent_mod = types.ModuleType("agent")
    agent_mod.AgentContext = types.SimpleNamespace(get=lambda context_id: _FakeContext())
    sys.modules["agent"] = agent_mod

    bridge_mod = _reload("usr.plugins.a0_connector.helpers.event_bridge")

    events, cursor = bridge_mod.get_context_log_entries("ctx-1", after=0)

    assert cursor == 9
    assert events == [
        {
            "context_id": "ctx-1",
            "sequence": 9,
            "event": "info",
            "timestamp": "2026-04-01T00:00:00Z",
            "data": {"text": "Process reset, agent nudged."},
        }
    ]


def test_ws_connector_hello_advertises_remote_exec_and_tree_features() -> None:
    _install_fake_helpers()

    ws_connector_mod = _reload("usr.plugins.a0_connector.api.ws_connector")
    handler = ws_connector_mod.WsConnector(None, None)

    payload = asyncio.run(handler.process("connector_hello", {}, "sid-1"))

    assert payload["protocol"] == "a0-connector.v1"
    assert "remote_file_tree" in payload["features"]
    assert "code_execution_remote" in payload["features"]


def test_ws_connector_remote_tree_update_stores_latest_snapshot() -> None:
    _install_fake_helpers()

    ws_runtime_mod = _reload("usr.plugins.a0_connector.helpers.ws_runtime")
    _reset_ws_runtime_state(ws_runtime_mod)
    ws_connector_mod = _reload("usr.plugins.a0_connector.api.ws_connector")
    handler = ws_connector_mod.WsConnector(None, None)

    sid = "sid-tree"
    context_id = "ctx-tree"
    ws_runtime_mod.register_sid(sid)
    ws_runtime_mod.subscribe_sid_to_context(sid, context_id)

    result = handler._handle_remote_tree_update(
        {
            "root_path": "/tmp/workspace",
            "tree": "/tmp/workspace/\n└── app.py",
            "tree_hash": "tree-hash-1",
            "generated_at": "2026-04-08T00:00:00Z",
        },
        sid,
    )

    assert result["accepted"] is True
    latest = ws_runtime_mod.latest_remote_tree_for_context(context_id, max_age_seconds=90.0)
    assert latest is not None
    assert latest["tree_hash"] == "tree-hash-1"
    assert latest["root_path"] == "/tmp/workspace"

    _reset_ws_runtime_state(ws_runtime_mod)


def test_ws_connector_exec_op_result_resolves_pending_future() -> None:
    _install_fake_helpers()

    ws_runtime_mod = _reload("usr.plugins.a0_connector.helpers.ws_runtime")
    _reset_ws_runtime_state(ws_runtime_mod)
    ws_connector_mod = _reload("usr.plugins.a0_connector.api.ws_connector")
    handler = ws_connector_mod.WsConnector(None, None)

    async def _scenario() -> None:
        sid = "sid-exec"
        ws_runtime_mod.register_sid(sid)
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        ws_runtime_mod.store_pending_exec_op(
            "exec-1",
            sid=sid,
            future=future,
            loop=loop,
            context_id="ctx-exec",
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
    _reset_ws_runtime_state(ws_runtime_mod)


def _install_fake_agent_loopdata_module() -> None:
    agent_mod = types.ModuleType("agent")

    @dataclass
    class LoopData:
        extras_temporary: dict[str, str] = field(default_factory=dict)

    agent_mod.LoopData = LoopData
    sys.modules["agent"] = agent_mod


def test_remote_tree_prompt_extension_injects_when_snapshot_is_fresh() -> None:
    _install_fake_helpers()
    _install_fake_agent_loopdata_module()

    ws_runtime_mod = _reload("usr.plugins.a0_connector.helpers.ws_runtime")
    _reset_ws_runtime_state(ws_runtime_mod)
    extension_mod = _reload(
        "usr.plugins.a0_connector.extensions.python.message_loop_prompts_after._76_include_remote_file_structure"
    )

    sid = "sid-ext-fresh"
    context_id = "ctx-ext-fresh"
    ws_runtime_mod.register_sid(sid)
    ws_runtime_mod.subscribe_sid_to_context(sid, context_id)
    ws_runtime_mod.store_remote_tree_snapshot(
        sid,
        {
            "root_path": "/workspace",
            "tree": "/workspace/\n└── README.md",
            "tree_hash": "fresh-tree",
            "generated_at": "2026-04-08T00:00:00Z",
        },
    )

    extension = extension_mod.IncludeRemoteFileStructure()
    extension.agent = types.SimpleNamespace(
        context=types.SimpleNamespace(id=context_id),
        read_prompt=lambda _name, **kwargs: (
            f"{kwargs['folder']}|{kwargs['generated_at']}|{kwargs['age_seconds']}|{kwargs['file_structure']}"
        ),
    )

    loop_data = sys.modules["agent"].LoopData()
    asyncio.run(extension.execute(loop_data))

    assert "remote_file_structure" in loop_data.extras_temporary
    injected = loop_data.extras_temporary["remote_file_structure"]
    assert "/workspace" in injected
    assert "README.md" in injected

    _reset_ws_runtime_state(ws_runtime_mod)


def test_remote_tree_prompt_extension_skips_stale_or_missing_snapshots() -> None:
    _install_fake_helpers()
    _install_fake_agent_loopdata_module()

    ws_runtime_mod = _reload("usr.plugins.a0_connector.helpers.ws_runtime")
    _reset_ws_runtime_state(ws_runtime_mod)
    extension_mod = _reload(
        "usr.plugins.a0_connector.extensions.python.message_loop_prompts_after._76_include_remote_file_structure"
    )

    sid = "sid-ext-stale"
    context_id = "ctx-ext-stale"
    ws_runtime_mod.register_sid(sid)
    ws_runtime_mod.subscribe_sid_to_context(sid, context_id)
    ws_runtime_mod.store_remote_tree_snapshot(
        sid,
        {
            "root_path": "/workspace",
            "tree": "/workspace/\n└── stale.txt",
            "tree_hash": "stale-tree",
            "generated_at": "2026-04-08T00:00:00Z",
        },
    )
    with ws_runtime_mod._state_lock:
        snapshot = ws_runtime_mod._remote_tree_snapshots[sid]
        ws_runtime_mod._remote_tree_snapshots[sid] = ws_runtime_mod.RemoteTreeSnapshot(
            sid=snapshot.sid,
            payload=dict(snapshot.payload),
            updated_at=time.time() - 200.0,
        )

    extension = extension_mod.IncludeRemoteFileStructure()
    extension.agent = types.SimpleNamespace(
        context=types.SimpleNamespace(id=context_id),
        read_prompt=lambda _name, **kwargs: "should-not-be-used",
    )

    stale_loop_data = sys.modules["agent"].LoopData()
    asyncio.run(extension.execute(stale_loop_data))
    assert "remote_file_structure" not in stale_loop_data.extras_temporary

    ws_runtime_mod.unregister_sid(sid)
    missing_loop_data = sys.modules["agent"].LoopData()
    asyncio.run(extension.execute(missing_loop_data))
    assert "remote_file_structure" not in missing_loop_data.extras_temporary

    _reset_ws_runtime_state(ws_runtime_mod)
