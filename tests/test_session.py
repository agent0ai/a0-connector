from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agent_zero_cli.config import CLIConfig
from agent_zero_cli.session import ConnectorSession, SessionError


pytestmark = pytest.mark.anyio


def _capabilities(**overrides: Any) -> dict[str, Any]:
    payload = {
        "protocol": "a0-connector.v1",
        "websocket_namespace": "/ws",
        "websocket_handlers": ["plugins/_a0_connector/ws_connector"],
        "auth": ["session"],
        "auth_required": False,
        "features": ["chat_create", "chat_get", "message_queue"],
    }
    payload.update(overrides)
    return payload


class Observer:
    def __init__(self) -> None:
        self.stages: list[tuple[str, str, str]] = []
        self.events: list[dict[str, Any]] = []
        self.snapshots: list[tuple[list[dict[str, Any]], list[dict[str, Any]]]] = []
        self.completed: list[str] = []
        self.errors: list[tuple[str, str]] = []
        self.disconnected = 0

    def on_stage(self, stage: str, message: str, detail: str = "") -> None:
        self.stages.append((stage, message, detail))

    def on_event(self, event: dict[str, Any]) -> None:
        self.events.append(event)

    def on_snapshot(self, events: list[dict[str, Any]], queue: list[dict[str, Any]]) -> None:
        self.snapshots.append((events, queue))

    def on_complete(self, context_id: str) -> None:
        self.completed.append(context_id)

    def on_error(self, code: str, message: str) -> None:
        self.errors.append((code, message))

    def on_disconnect(self) -> None:
        self.disconnected += 1


class FakeClient:
    instances: list["FakeClient"] = []
    capabilities = _capabilities()
    verify_session_result = True
    login_result = True
    chats: list[dict[str, Any]] = []
    chat_metadata: dict[str, dict[str, Any]] = {}
    create_chat_id = "ctx-created"

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self.connected = False
        self.hello_calls: list[dict[str, Any]] = []
        self.subscribe_calls: list[tuple[str, int]] = []
        self.unsubscribe_calls: list[str] = []
        self.remote_tree_updates: list[dict[str, Any]] = []
        self.create_calls = 0
        self.sent_messages: list[tuple[str, str, list[str] | None]] = []
        self.queued_messages: list[tuple[str, str, list[str] | None]] = []
        self.queue_send_calls: list[tuple[str, str | None, bool]] = []
        self.queue_remove_calls: list[tuple[str, str | None]] = []
        self.login_calls: list[tuple[str, str]] = []
        self.restore_calls: list[str] = []
        self.clear_session_calls = 0
        self.disconnect_calls: list[tuple[bool, bool]] = []
        self.on_connect = None
        self.on_disconnect = None
        self.on_context_snapshot = None
        self.on_context_event = None
        self.on_context_complete = None
        self.on_message_queue_updated = None
        self.on_settings_updated = None
        self.on_error = None
        self.on_file_op = None
        self.on_exec_op = None
        self.on_computer_use_op = None
        self.on_browser_op = None
        FakeClient.instances.append(self)

    async def fetch_capabilities(self) -> dict[str, Any]:
        return dict(self.capabilities)

    def restore_session(self, host: str) -> bool:
        self.restore_calls.append(host)
        return True

    async def verify_session(self) -> bool:
        return bool(self.verify_session_result)

    def clear_session(self) -> None:
        self.clear_session_calls += 1

    async def login(self, username: str, password: str) -> bool:
        self.login_calls.append((username, password))
        return bool(self.login_result)

    async def connect_websocket(self) -> None:
        self.connected = True
        if self.on_connect is not None:
            self.on_connect()

    async def send_hello(self, **payload: Any) -> dict[str, Any]:
        self.hello_calls.append(payload)
        return {"exec_config": {"version": 1}}

    async def create_chat(self, *, current_context_id: str | None = None) -> str:
        del current_context_id
        self.create_calls += 1
        return self.create_chat_id

    async def list_chats(self) -> list[dict[str, Any]]:
        return list(self.chats)

    async def get_chat(self, context_id: str) -> dict[str, Any]:
        return dict(self.chat_metadata.get(context_id, {}))

    async def subscribe_context(self, context_id: str, from_seq: int = 0) -> dict[str, Any]:
        self.subscribe_calls.append((context_id, from_seq))
        if self.on_context_snapshot is not None:
            self.on_context_snapshot(
                {
                    "context_id": context_id,
                    "events": [{"event": "info", "sequence": 1, "data": {"text": "loaded"}}],
                    "message_queue": [],
                }
            )
        return {}

    async def unsubscribe_context(self, context_id: str) -> dict[str, Any]:
        self.unsubscribe_calls.append(context_id)
        return {}

    async def send_remote_tree_update(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.remote_tree_updates.append(payload)
        return {}

    async def send_message(
        self,
        text: str,
        context_id: str,
        attachments: list[str] | None = None,
    ) -> dict[str, Any]:
        self.sent_messages.append((text, context_id, attachments))
        return {}

    async def add_message_to_queue(
        self,
        text: str,
        context_id: str,
        attachments: list[str] | None = None,
    ) -> dict[str, Any]:
        self.queued_messages.append((text, context_id, attachments))
        return {"message_queue": [{"id": "queued-1", "text": text}]}

    async def send_message_queue(
        self,
        context_id: str,
        *,
        item_id: str | None = None,
        send_all: bool = True,
    ) -> dict[str, Any]:
        self.queue_send_calls.append((context_id, item_id, send_all))
        return {"sent_count": 1, "message_queue": []}

    async def remove_message_from_queue(
        self,
        context_id: str,
        *,
        item_id: str | None = None,
    ) -> dict[str, Any]:
        self.queue_remove_calls.append((context_id, item_id))
        return {"message_queue": []}

    async def pause_agent(self, context_id: str | None, *, paused: bool = True) -> dict[str, Any]:
        return {"ok": True, "paused": paused, "context_id": context_id}

    async def reset_chat(self, context_id: str) -> dict[str, Any]:
        return {"ok": True, "context_id": context_id}

    async def disconnect(self, *, close_http: bool = True, notify: bool = True) -> None:
        self.disconnect_calls.append((close_http, notify))
        self.connected = False


@pytest.fixture(autouse=True)
def reset_fake_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    FakeClient.instances = []
    FakeClient.capabilities = _capabilities()
    FakeClient.verify_session_result = True
    FakeClient.login_result = True
    FakeClient.chats = []
    FakeClient.chat_metadata = {}
    FakeClient.create_chat_id = "ctx-created"

    import agent_zero_cli.config as config_mod

    env_dir = tmp_path / ".agent-zero"
    env_file = env_dir / ".env"
    monkeypatch.setattr(config_mod, "_ENV_DIR", env_dir)
    monkeypatch.setattr(config_mod, "_ENV_FILE", env_file)


async def test_session_connects_and_advertises_headless_metadata(tmp_path: Path) -> None:
    FakeClient.chat_metadata = {"ctx-default": {"last_message": "hello"}}
    observer = Observer()
    session = ConnectorSession(
        CLIConfig(default_context_id="ctx-default"),
        observer,
        workspace=tmp_path,
        client_factory=FakeClient,
    )

    context_id = await session.connect("http://agent.test")

    client = FakeClient.instances[-1]
    assert context_id == "ctx-default"
    assert client.subscribe_calls == [("ctx-default", 0)]
    assert observer.snapshots
    assert client.remote_tree_updates
    assert client.hello_calls[-1]["computer_use"]["enabled"] is False
    assert client.hello_calls[-1]["host_browser"]["supported"] is False
    assert client.hello_calls[-1]["remote_files"] == {
        "enabled": True,
        "write_enabled": True,
        "mode": "read_write",
    }
    assert client.hello_calls[-1]["remote_exec"] == {"enabled": True}

    await session.close()


async def test_deferred_context_keeps_host_tools_without_selecting_a_chat(tmp_path: Path) -> None:
    session = ConnectorSession(
        CLIConfig(),
        Observer(),
        workspace=tmp_path,
        client_factory=FakeClient,
        defer_context=True,
        remember_context=False,
    )

    assert await session.connect("http://agent.test") == ""
    client = FakeClient.instances[-1]
    assert client.create_calls == 0
    assert client.subscribe_calls == []
    assert client.remote_tree_updates

    await session.switch_context("ctx-acp")
    assert client.subscribe_calls == [("ctx-acp", 0)]
    await session.close()


class GatewayFakeClient(FakeClient):
    capabilities = _capabilities(
        features=[
            "launcher_gateway",
            "launcher_gateway_file_write",
            "code_execution_remote",
            "browser_host_remote",
        ]
    )

    async def send_hello(self, **payload: Any) -> dict[str, Any]:
        self.hello_calls.append(payload)
        return {
            "features": ["launcher_gateway_control"],
            "exec_config": {"version": 1},
        }


class FakeHostBrowser:
    def __init__(self) -> None:
        self.enabled = False
        self.closed = 0
        self.requests: list[dict[str, Any]] = []

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled

    def hello_metadata(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "supported": True,
            "enabled": self.enabled,
            "status": "ready",
            "browser_id": kwargs.get("browser_selection", ""),
        }

    async def handle_op(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(payload)
        return {"op_id": payload.get("op_id"), "ok": True, "result": {"browser": True}}

    async def close(self) -> None:
        self.closed += 1


class FakeComputerUse:
    def __init__(self) -> None:
        self.enabled = False
        self.closed = 0
        self.requests: list[dict[str, Any]] = []

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled

    def hello_metadata(self) -> dict[str, Any]:
        return {"supported": True, "enabled": self.enabled, "status": "allow"}

    async def handle_op(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(payload)
        return {"op_id": payload.get("op_id"), "ok": True, "result": {"computer": True}}

    async def close(self) -> None:
        self.closed += 1


async def test_tools_only_gateway_connects_without_creating_or_subscribing_to_chat(
    tmp_path: Path,
) -> None:
    browser = FakeHostBrowser()
    computer = FakeComputerUse()
    scopes = {
        "files": True,
        "file_write": True,
        "code_execution": True,
        "browser": True,
        "computer_use": True,
    }
    session = ConnectorSession(
        CLIConfig(),
        Observer(),
        workspace=tmp_path,
        client_factory=GatewayFakeClient,
        tools_only=True,
        gateway={
            "id": "launcher-test",
            "host_label": "Test host",
            "master_enabled": True,
            "scopes": scopes,
        },
        host_browser_manager=browser,
        computer_use_manager=computer,
        browser_selection="chromium:default",
    )

    context_id = await session.connect("http://agent.test")
    client = FakeClient.instances[-1]

    assert context_id == ""
    assert session.context_id == ""
    assert client.create_calls == 0
    assert client.subscribe_calls == []
    assert client.hello_calls[-1]["gateway"]["kind"] == "launcher"
    assert client.hello_calls[-1]["gateway"]["state"] == "connected"
    assert client.hello_calls[-1]["gateway"]["status"]["browser"]["browser_id"] == "chromium:default"
    assert client.hello_calls[-1]["gateway"]["status"]["computer_use"]["status"] == "allow"
    assert client.hello_calls[-1]["host_browser"]["browser_id"] == "chromium:default"
    assert client.remote_tree_updates

    browser_result = await client.on_browser_op(
        {
            "op_id": "browser-1",
            "action": "status",
            "profile_mode": "agent",
            "browser_selection": "stale:profile",
        }
    )
    computer_result = await client.on_computer_use_op({"op_id": "computer-1", "action": "status"})
    assert browser_result["ok"] is True
    assert browser.requests[-1]["profile_mode"] == "existing"
    assert browser.requests[-1]["browser_selection"] == "chromium:default"
    assert computer_result["ok"] is True

    await session.close()
    assert browser.closed >= 1
    assert computer.closed >= 1


async def test_tools_only_gateway_reconnects_without_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("agent_zero_cli.session._RECOVERY_DELAYS_SECONDS", (0.0,))
    session = ConnectorSession(
        CLIConfig(),
        Observer(),
        workspace=tmp_path,
        client_factory=GatewayFakeClient,
        tools_only=True,
        gateway={"id": "launcher-test", "scopes": {"files": True}},
    )
    await session.connect("http://agent.test")
    client = FakeClient.instances[-1]
    client.connected = False

    session._handle_disconnect()
    assert session._recovery_task is not None
    await session._recovery_task

    assert session.connected is True
    assert session.context_id == ""
    assert client.subscribe_calls == []
    assert len(client.hello_calls) >= 3
    await session.close()


async def test_tools_only_gateway_requires_both_core_features(tmp_path: Path) -> None:
    class MissingFeatureClient(GatewayFakeClient):
        capabilities = _capabilities(features=[])

    session = ConnectorSession(
        CLIConfig(),
        Observer(),
        workspace=tmp_path,
        client_factory=MissingFeatureClient,
        tools_only=True,
        gateway={"id": "launcher-test", "scopes": {"files": True}},
    )
    with pytest.raises(SessionError, match="Launcher gateway") as exc_info:
        await session.connect("http://agent.test")
    assert exc_info.value.code == "CONTRACT_MISMATCH"


async def test_gateway_scope_dependency_and_emergency_disconnect_are_immediate(
    tmp_path: Path,
) -> None:
    disconnected: list[bool] = []
    browser = FakeHostBrowser()
    computer = FakeComputerUse()
    session = ConnectorSession(
        CLIConfig(),
        Observer(),
        workspace=tmp_path,
        client_factory=GatewayFakeClient,
        tools_only=True,
        gateway={
            "id": "launcher-test",
            "scopes": {
                "files": True,
                "file_write": True,
                "code_execution": True,
                "browser": True,
                "computer_use": True,
            },
        },
        host_browser_manager=browser,
        computer_use_manager=computer,
        on_gateway_disconnect=lambda: disconnected.append(True),
    )
    await session.connect("http://agent.test")
    browser_closes = browser.closed
    computer_closes = computer.closed
    exec_closes: list[bool] = []

    async def close_exec() -> None:
        exec_closes.append(True)

    session.remote_exec.close = close_exec

    response = await session._handle_gateway_control(
        {
            "request_id": "scope-1",
            "action": "replace_scopes",
            "scopes": {
                "files": False,
                "file_write": True,
                "code_execution": True,
                "browser": True,
                "computer_use": True,
            },
        }
    )
    assert response["gateway"]["scopes"]["code_execution"] is False
    assert (await session._handle_file_op({"op_id": "file-1", "op": "read"}))["ok"] is False
    assert exec_closes == [True]
    assert browser.closed == browser_closes
    assert computer.closed == computer_closes

    paused = await session._handle_gateway_control(
        {"request_id": "pause-1", "action": "set_master", "enabled": False}
    )
    assert paused["gateway"]["state"] == "paused"
    assert len(exec_closes) == 2
    assert browser.closed == browser_closes + 1
    assert computer.closed == computer_closes + 1

    emergency = {"request_id": "stop-1", "action": "emergency_disconnect"}
    result = await session._handle_gateway_control(emergency)
    assert result["gateway"]["state"] == "disconnected"
    await session._handle_gateway_control_result_sent(emergency, result)
    assert disconnected == [True]
    await session.close()


async def test_gateway_file_write_scope_controls_writes_without_hiding_reads(
    tmp_path: Path,
) -> None:
    target = tmp_path / "note.txt"
    target.write_text("hello", encoding="utf-8")
    session = ConnectorSession(
        CLIConfig(),
        Observer(),
        workspace=tmp_path,
        client_factory=GatewayFakeClient,
        tools_only=True,
        gateway={
            "id": "launcher-test",
            "scopes": {
                "files": True,
                "file_write": False,
                "code_execution": True,
                "browser": False,
                "computer_use": False,
            },
        },
    )
    await session.connect("http://agent.test")

    assert session._gateway_scopes()["code_execution"] is False
    assert (await session._handle_file_op({"op_id": "read", "op": "read", "path": "note.txt"}))["ok"] is True
    assert (await session._handle_file_op({"op_id": "write", "op": "write", "path": "note.txt", "content": "changed"}))["ok"] is False
    assert session._remote_file_metadata()["mode"] == "read_only"
    await session.close()


async def test_session_recovers_websocket_after_transport_drop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("agent_zero_cli.session._RECOVERY_DELAYS_SECONDS", (0.0,))
    observer = Observer()
    session = ConnectorSession(
        CLIConfig(default_context_id="ctx-default"),
        observer,
        workspace=tmp_path,
        client_factory=FakeClient,
    )
    await session.connect("http://agent.test")

    client = FakeClient.instances[-1]
    client.connected = False
    session._handle_disconnect()
    assert session.connected is False

    assert session._recovery_task is not None
    await session._recovery_task

    assert observer.disconnected == 0
    assert session.connected is True
    assert session.context_id == "ctx-default"
    assert client.subscribe_calls == [("ctx-default", 0), ("ctx-default", 0)]
    assert client.hello_calls[-1]["context_id"] == "ctx-default"
    assert observer.stages[-1] == ("ready", "Reconnected.", "http://agent.test")

    await session.close()


async def test_session_raises_auth_required_without_credentials(tmp_path: Path) -> None:
    FakeClient.capabilities = _capabilities(auth_required=True)
    FakeClient.verify_session_result = False
    session = ConnectorSession(
        CLIConfig(),
        Observer(),
        workspace=tmp_path,
        client_factory=FakeClient,
    )

    with pytest.raises(SessionError) as exc_info:
        await session.connect("http://agent.test")

    assert exc_info.value.code == "AUTH_REQUIRED"
    assert exc_info.value.exit_code == 2
    assert FakeClient.instances[-1].login_calls == []


async def test_session_logs_in_with_credentials(tmp_path: Path) -> None:
    FakeClient.capabilities = _capabilities(auth_required=True)
    FakeClient.verify_session_result = False
    FakeClient.login_result = True
    session = ConnectorSession(
        CLIConfig(),
        Observer(),
        workspace=tmp_path,
        client_factory=FakeClient,
    )

    await session.connect("http://agent.test", username="neo", password="trinity")

    client = FakeClient.instances[-1]
    assert client.restore_calls == ["http://agent.test"]
    assert client.login_calls == [("neo", "trinity")]

    await session.close()


async def test_session_rejects_capability_mismatch(tmp_path: Path) -> None:
    FakeClient.capabilities = _capabilities(protocol="a0-connector.v0")
    session = ConnectorSession(
        CLIConfig(),
        Observer(),
        workspace=tmp_path,
        client_factory=FakeClient,
    )

    with pytest.raises(SessionError) as exc_info:
        await session.connect("http://agent.test")

    assert exc_info.value.code == "CONTRACT_MISMATCH"
    assert exc_info.value.exit_code == 2


async def test_session_restores_saved_context_when_no_default(tmp_path: Path) -> None:
    FakeClient.chats = [{"id": "ctx-saved", "last_message": "saved"}]
    config = CLIConfig(
        last_context_id="ctx-saved",
        last_context_host="http://agent.test",
    )
    session = ConnectorSession(
        config,
        Observer(),
        workspace=tmp_path,
        client_factory=FakeClient,
    )

    await session.connect("http://agent.test")

    assert session.context_id == "ctx-saved"
    assert FakeClient.instances[-1].subscribe_calls == [("ctx-saved", 0)]

    await session.close()


async def test_session_queues_messages_while_agent_active(tmp_path: Path) -> None:
    session = ConnectorSession(
        CLIConfig(default_context_id="ctx-default"),
        Observer(),
        workspace=tmp_path,
        client_factory=FakeClient,
    )
    await session.connect("http://agent.test")
    session.agent_active = True

    await session.send_message("next")

    client = FakeClient.instances[-1]
    assert client.sent_messages == []
    assert client.queued_messages == [("next", "ctx-default", [])]
    assert session.message_queue == [{"id": "queued-1", "text": "next"}]

    await session.close()


async def test_session_does_not_queue_after_post_complete_status_event(tmp_path: Path) -> None:
    session = ConnectorSession(
        CLIConfig(default_context_id="ctx-default"),
        Observer(),
        workspace=tmp_path,
        client_factory=FakeClient,
    )
    await session.connect("http://agent.test")
    session.agent_active = True
    session._context_run_complete = False

    session._handle_context_complete({"context_id": "ctx-default"})
    session._handle_context_event(
        {
            "context_id": "ctx-default",
            "event": "status",
            "sequence": 2,
            "data": {"meta": {"step": "Memorizing results"}},
        }
    )
    await session.send_message("new task")

    client = FakeClient.instances[-1]
    assert session.agent_active is True
    assert client.sent_messages == [("new task", "ctx-default", [])]
    assert client.queued_messages == []

    await session.close()


async def test_session_can_send_and_manage_message_queue(tmp_path: Path) -> None:
    session = ConnectorSession(
        CLIConfig(default_context_id="ctx-default"),
        Observer(),
        workspace=tmp_path,
        client_factory=FakeClient,
    )
    await session.connect("http://agent.test")
    session.message_queue = [{"id": "queued-1", "text": "next"}]

    send_response = await session.send_message_queue(send_all=True)
    remove_response = await session.remove_message_from_queue("queued-1")
    clear_response = await session.clear_message_queue()

    client = FakeClient.instances[-1]
    assert send_response["sent_count"] == 1
    assert client.queue_send_calls == [("ctx-default", None, True)]
    assert client.queue_remove_calls == [("ctx-default", "queued-1"), ("ctx-default", None)]
    assert remove_response["message_queue"] == []
    assert clear_response["message_queue"] == []
    assert session.message_queue == []

    await session.close()


async def test_session_refresh_context_snapshot_replays_current_context(tmp_path: Path) -> None:
    session = ConnectorSession(
        CLIConfig(default_context_id="ctx-default"),
        Observer(),
        workspace=tmp_path,
        client_factory=FakeClient,
    )
    await session.connect("http://agent.test")

    client = FakeClient.instances[-1]
    client.subscribe_calls.clear()
    await session.refresh_context_snapshot()

    assert client.subscribe_calls == [("ctx-default", 0)]

    await session.close()
