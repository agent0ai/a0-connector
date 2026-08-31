from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_zero_cli.config import CLIConfig
from agent_zero_cli.headless.commands import command_may_start_agent, dispatch_headless_command
from agent_zero_cli.headless.renderer import JsonlRenderer, TextRenderer
from agent_zero_cli.headless.runner import (
    HeadlessOptions,
    HeadlessRunner,
    normalize_launcher_tag_attachment_ref,
    parse_launcher_tag_result,
)
from agent_zero_cli.session import SessionError


pytestmark = pytest.mark.anyio


class RecordingStream(io.StringIO):
    def __init__(
        self,
        writes: list[tuple[str, str]],
        label: str,
        *,
        is_tty: bool = False,
    ) -> None:
        super().__init__()
        self._writes = writes
        self._label = label
        self._is_tty = is_tty

    def write(self, value: str) -> int:
        self._writes.append((self._label, value))
        return super().write(value)

    def isatty(self) -> bool:
        return self._is_tty


class FakeSession:
    instances: list["FakeSession"] = []
    connect_error: SessionError | None = None
    stream_text = "4"
    final_snapshot_text = "4"
    initial_queue: list[dict[str, Any]] = []

    def __init__(
        self,
        config: CLIConfig,
        observer: Any,
        *,
        workspace: Path,
        remote_file_write_enabled: bool,
        remote_exec_enabled: bool,
        remote_files_enabled: bool = True,
        remember_context: bool = True,
    ) -> None:
        self.config = config
        self.observer = observer
        self.workspace = workspace
        self.remote_file_write_enabled = remote_file_write_enabled
        self.remote_exec_enabled = remote_exec_enabled
        self.remote_files_enabled = remote_files_enabled
        self.remember_context = remember_context
        self.remote_files = SimpleNamespace(scan_root=str(workspace))
        self.connector_features = {"message_queue", "chat_create"}
        self.host = ""
        self.context_id = "ctx-1"
        self.agent_active = False
        self.message_queue = [dict(item) for item in FakeSession.initial_queue]
        self.goal: dict[str, Any] | None = None
        self.sent: list[str] = []
        self.sent_attachments: list[list[str] | None] = []
        self.new_chat_agent_profile = ""
        self.queue_send_calls: list[tuple[str | None, bool]] = []
        self.queue_remove_calls: list[str | None] = []
        self.goal_calls: list[tuple[str, dict[str, Any]]] = []
        self.closed = False
        FakeSession.instances.append(self)

    async def connect(
        self,
        host: str,
        *,
        username: str = "",
        password: str = "",
        context_id: str = "",
        chat_last: bool = False,
        new_chat: bool = False,
        new_chat_agent_profile: str = "",
        restore_session: bool = True,
    ) -> str:
        del username, password, chat_last, restore_session
        if FakeSession.connect_error is not None:
            raise FakeSession.connect_error
        self.host = host
        self.new_chat_agent_profile = new_chat_agent_profile
        self.context_id = context_id or ("ctx-new" if new_chat else "ctx-1")
        self.observer.on_stage("ready", "Ready when you are.", host)
        return self.context_id

    async def send_message(self, text: str, attachments: list[str] | None = None) -> dict[str, Any]:
        self.sent.append(text)
        self.sent_attachments.append(list(attachments) if attachments else None)
        self.agent_active = True
        self.observer.on_event(
            {
                "context_id": self.context_id,
                "event": "assistant_message",
                "sequence": 2,
                "data": {"text": self.stream_text},
            }
        )
        self.agent_active = False
        self.observer.on_complete(self.context_id)
        return {}

    async def send_message_queue(
        self,
        *,
        item_id: str | None = None,
        send_all: bool = True,
    ) -> dict[str, Any]:
        self.queue_send_calls.append((item_id, send_all))
        self.message_queue = []
        self.agent_active = True
        self.observer.on_event(
            {
                "context_id": self.context_id,
                "event": "assistant_message",
                "sequence": 2,
                "data": {"text": self.stream_text},
            }
        )
        self.agent_active = False
        self.observer.on_complete(self.context_id)
        return {"sent_count": 1, "message_queue": []}

    async def clear_message_queue(self) -> dict[str, Any]:
        self.queue_remove_calls.append(None)
        self.message_queue = []
        return {"message_queue": []}

    async def remove_message_from_queue(self, item_id: str) -> dict[str, Any]:
        self.queue_remove_calls.append(item_id)
        self.message_queue = [item for item in self.message_queue if item.get("id") != item_id]
        return {"message_queue": self.message_queue}

    async def goal_action(self, action: str, **payload: Any) -> dict[str, Any]:
        self.goal_calls.append((action, dict(payload)))
        previous_status = str((self.goal or {}).get("status") or "")
        if action in {"create", "update"}:
            objective = str(payload.get("objective") or (self.goal or {}).get("objective") or "")
            status = str(payload.get("status") or (self.goal or {}).get("status") or "active")
            self.goal = {"objective": objective, "status": status}
        elif action == "pause":
            self.goal = {**(self.goal or {"objective": "current"}), "status": "paused"}
        elif action == "resume":
            self.goal = {**(self.goal or {"objective": "current"}), "status": "active"}
        elif action == "delete":
            self.goal = None
        return {
            "ok": True,
            "goal": self.goal,
            "reactivated": (
                action == "update"
                and previous_status in {"blocked", "complete"}
                and (self.goal or {}).get("status") == "active"
            ),
        }

    async def refresh_goal(self) -> dict[str, Any]:
        self.goal_calls.append(("get", {}))
        return {"ok": True, "goal": self.goal}

    async def refresh_context_snapshot(self) -> None:
        if self.final_snapshot_text is None:
            return
        self.observer.on_snapshot(
            [
                {
                    "context_id": self.context_id,
                    "event": "assistant_message",
                    "sequence": 2,
                    "data": {"text": self.final_snapshot_text, "meta": {"finished": True}},
                }
            ],
            [],
        )

    async def pause(self) -> dict[str, Any]:
        return {"ok": True}

    async def resume(self) -> dict[str, Any]:
        return {"ok": True}

    async def nudge(self) -> dict[str, Any]:
        return {"ok": True}

    async def reset(self) -> dict[str, Any]:
        return {"ok": True}

    async def list_chats(self) -> list[dict[str, Any]]:
        return [
            {"id": self.context_id, "name": "Active", "updated_at": "2026-06-11T08:00:00+00:00"},
            {"id": "ctx-2", "name": "Archive", "updated_at": "2026-06-10T08:00:00+00:00"},
        ]

    async def switch_context(self, context_id: str, *, has_messages_hint: bool | None = None) -> None:
        del has_messages_hint
        self.context_id = context_id

    async def new_context(self) -> str:
        self.context_id = "ctx-new"
        return self.context_id

    async def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def reset_fake_session(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeSession.instances = []
    FakeSession.connect_error = None
    FakeSession.stream_text = "4"
    FakeSession.final_snapshot_text = "4"
    FakeSession.initial_queue = []

    import agent_zero_cli.headless.runner as runner_mod

    monkeypatch.setattr(runner_mod, "ConnectorSession", FakeSession)
    monkeypatch.setattr(runner_mod, "_COMPLETION_SETTLE_SECONDS", 0.0)


def test_text_renderer_deduplicates_status_lines() -> None:
    renderer = TextRenderer(color=False)
    event = {
        "event": "tool_start",
        "sequence": 1,
        "data": {"heading": "web_search", "text": ""},
    }

    assert renderer.render_event(event) == ["- Using tool [web_search]"]
    assert renderer.render_event(event) == []
    assert renderer.render_event(
        {
            "event": "assistant_message",
            "sequence": 2,
            "data": {"text": "Done."},
        }
    ) == ["Done."]


def test_jsonl_renderer_emits_valid_records() -> None:
    renderer = JsonlRenderer()
    lines = renderer.render_event(
        {
            "context_id": "ctx-1",
            "event": "assistant_message",
            "sequence": 2,
            "data": {"text": "Done."},
        }
    )

    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["type"] == "event"
    assert payload["event"] == "assistant_message"


def test_launcher_tag_result_parser_accepts_exact_modes_and_fails_closed() -> None:
    assert parse_launcher_tag_result(
        "<!--a0-tag:v1;mode=replace-->\nField-ready text"
    ) == ("replace", "Field-ready text", True, "")
    assert parse_launcher_tag_result(
        "<!--a0-tag:v1;mode=action-->\nCompleted the workflow."
    ) == ("action", "Completed the workflow.", True, "")
    assert parse_launcher_tag_result(
        "<!--a0-tag:v1;mode=replace-->\n\tIndented reply\n\n"
    ) == ("replace", "\tIndented reply\n\n", True, "")

    assert parse_launcher_tag_result("Field-ready text") == (
        "overlay",
        "Field-ready text",
        False,
        "INVALID_TAG_RESULT",
    )
    assert parse_launcher_tag_result(
        "<!--a0-tag:v1;mode=replace-->\n<!--a0-tag:v1;mode=action-->\ntext"
    )[2:] == (False, "INVALID_TAG_RESULT")


def test_launcher_tag_attachment_ref_accepts_only_upload_basename() -> None:
    assert normalize_launcher_tag_attachment_ref(
        "/a0/usr/uploads/a0-tag-123.png"
    ) == "/a0/usr/uploads/a0-tag-123.png"

    with pytest.raises(SessionError, match="one Agent Zero upload reference"):
        normalize_launcher_tag_attachment_ref("/a0/usr/uploads/nested/image.png")


async def test_headless_commands_status_and_tui_only(tmp_path: Path) -> None:
    session = FakeSession(
        CLIConfig(),
        SimpleNamespace(on_event=lambda event: None, on_complete=lambda context_id: None),
        workspace=tmp_path,
        remote_file_write_enabled=True,
        remote_exec_enabled=True,
    )
    session.host = "http://agent.test"

    status = await dispatch_headless_command(session, "/status")
    unavailable = await dispatch_headless_command(session, "/browser host on")

    assert any(line == "host: http://agent.test" for line in status.lines)
    assert any(line == "message queue: empty" for line in status.lines)
    assert unavailable.error is True
    assert unavailable.lines == ["/browser is not available in headless mode."]


async def test_headless_queue_commands_send_clear_and_remove(tmp_path: Path) -> None:
    session = FakeSession(
        CLIConfig(),
        SimpleNamespace(on_event=lambda event: None, on_complete=lambda context_id: None),
        workspace=tmp_path,
        remote_file_write_enabled=True,
        remote_exec_enabled=True,
    )
    session.message_queue = [
        {"id": "item-1", "text": "first queued prompt", "attachment_count": 0},
        {"id": "item-2", "text": "second queued prompt", "attachment_count": 1},
    ]

    summary = await dispatch_headless_command(session, "/queue")
    remove = await dispatch_headless_command(session, "/queue remove 2")
    clear = await dispatch_headless_command(session, "/queue clear")
    session.message_queue = [{"id": "item-1", "text": "first queued prompt"}]
    send = await dispatch_headless_command(session, "/send")

    assert summary.lines == [
        "Queued messages (2):",
        "1. first queued prompt",
        "2. second queued prompt [1 files]",
    ]
    assert remove.lines == ["Queued message removed."]
    assert clear.lines == ["Queue cleared."]
    assert send.lines == ["sent 1 queued message"]
    assert send.await_completion is True
    assert session.queue_remove_calls == ["item-2", None]
    assert session.queue_send_calls == [(None, True)]


async def test_headless_goal_commands_set_update_and_delete(tmp_path: Path) -> None:
    session = FakeSession(
        CLIConfig(),
        SimpleNamespace(on_event=lambda event: None, on_complete=lambda context_id: None),
        workspace=tmp_path,
        remote_file_write_enabled=True,
        remote_exec_enabled=True,
    )

    created = await dispatch_headless_command(session, "/goal Find weak spots")
    updated = await dispatch_headless_command(session, "/goal update Ship CLI row")
    status = await dispatch_headless_command(session, "/goal")
    deleted = await dispatch_headless_command(session, "/goal delete")

    assert created.lines == ["Goal set."]
    assert created.await_completion is True
    assert updated.lines == ["Goal updated."]
    assert status.lines == ["goal: active - Ship CLI row"]
    assert deleted.lines == ["Goal deleted."]
    assert session.sent == ["Find weak spots"]
    assert session.goal_calls == [
        ("create", {"objective": "Find weak spots", "created_by": "user"}),
        ("update", {"objective": "Ship CLI row", "status": "active"}),
        ("get", {}),
        ("delete", {}),
    ]


async def test_headless_terminal_goal_update_restarts_agent(tmp_path: Path) -> None:
    session = FakeSession(
        CLIConfig(),
        SimpleNamespace(on_event=lambda event: None, on_complete=lambda context_id: None),
        workspace=tmp_path,
        remote_file_write_enabled=True,
        remote_exec_enabled=True,
    )
    session.goal = {"objective": "Old goal", "status": "blocked"}

    updated = await dispatch_headless_command(session, "/goal update Continue the goal")

    assert updated.lines == ["Goal updated."]
    assert updated.await_completion is True
    assert session.sent == ["Continue the goal"]


def test_headless_queue_send_command_is_agent_starting() -> None:
    assert command_may_start_agent("/send") is True
    assert command_may_start_agent("/queue send") is True
    assert command_may_start_agent("/goal Find weak spots") is True
    assert command_may_start_agent("/goal update Ship CLI row") is True
    assert command_may_start_agent("/goal delete") is False
    assert command_may_start_agent("/queue") is False
    assert command_may_start_agent("/status") is False


def test_headless_completion_notifies_once_on_tty_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key in ("A0_TERMINAL_NOTIFY", "KITTY_WINDOW_ID", "TERM_PROGRAM", "TMUX"):
        monkeypatch.delenv(key, raising=False)
    writes: list[tuple[str, str]] = []
    stdout = RecordingStream(writes, "stdout")
    stderr = RecordingStream(writes, "stderr", is_tty=True)
    runner = HeadlessRunner(
        HeadlessOptions(
            output="jsonl",
            stdout=stdout,
            stderr=stderr,
        )
    )

    runner.on_complete("ctx-1")
    runner.on_complete("ctx-1")

    records = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert records == [{"type": "complete", "context_id": "ctx-1"}]
    assert stderr.getvalue() == "\x1b]777;notify;Agent Zero;Ready for input\x07"


async def test_print_mode_jsonl_stdout_is_valid(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    del monkeypatch
    stdout = io.StringIO()
    stderr = io.StringIO()
    options = HeadlessOptions(
        host="http://agent.test",
        output="jsonl",
        print_prompt="",
        workspace=tmp_path,
        config=CLIConfig(),
        stdin=io.StringIO("what is 2+2\n"),
        stdout=stdout,
        stderr=stderr,
    )

    exit_code = await HeadlessRunner(options).run()

    assert exit_code == 0
    assert stderr.getvalue() == ""
    records = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert [record["type"] for record in records] == ["ready", "event", "complete"]
    assert records[1]["data"]["text"] == "4"
    assert FakeSession.instances[-1].sent == ["what is 2+2"]
    assert FakeSession.instances[-1].closed is True


async def test_launcher_tag_mode_is_capability_silent_and_emits_normalized_result(
    tmp_path: Path,
) -> None:
    FakeSession.stream_text = "<!--a0-tag:v1;mode=replace-->\nDraft answer"
    FakeSession.final_snapshot_text = "<!--a0-tag:v1;mode=replace-->\nFinal answer"
    stdout = io.StringIO()
    stderr = io.StringIO()
    options = HeadlessOptions(
        host="http://agent.test",
        new_chat=True,
        output="jsonl",
        print_prompt="",
        workspace=tmp_path,
        discover_instances=False,
        launcher_tag=True,
        agent_profile="developer",
        attachment_refs=[
            "/a0/usr/uploads/a0-tag-window.png",
            "/a0/usr/uploads/brief.pdf",
        ],
        config=CLIConfig(),
        stdin=io.StringIO("tagged prompt"),
        stdout=stdout,
        stderr=stderr,
    )

    exit_code = await HeadlessRunner(options).run()

    assert exit_code == 0
    records = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert [record["type"] for record in records] == ["ready", "tag_result", "complete"]
    assert records[1] == {
        "type": "tag_result",
        "context_id": "ctx-new",
        "mode": "replace",
        "text": "Final answer",
        "valid": True,
    }
    session = FakeSession.instances[-1]
    assert session.remote_files_enabled is False
    assert session.remote_file_write_enabled is False
    assert session.remote_exec_enabled is False
    assert session.remember_context is False
    assert session.new_chat_agent_profile == "developer"
    assert session.sent == ["tagged prompt"]
    assert session.sent_attachments == [[
        "/a0/usr/uploads/a0-tag-window.png",
        "/a0/usr/uploads/brief.pdf",
    ]]
    assert stderr.getvalue() == ""


async def test_launcher_tag_mode_requires_safe_one_shot_options(tmp_path: Path) -> None:
    stdout = io.StringIO()
    options = HeadlessOptions(
        host="http://agent.test",
        output="jsonl",
        launcher_tag=True,
        agent_profile="developer",
        workspace=tmp_path,
        config=CLIConfig(),
        stdout=stdout,
        stderr=io.StringIO(),
    )

    assert await HeadlessRunner(options).run() == 1
    assert json.loads(stdout.getvalue())["code"] == "INVALID_TAG_MODE"


async def test_print_mode_renders_final_snapshot_before_complete_and_notification(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    for key in ("A0_TERMINAL_NOTIFY", "KITTY_WINDOW_ID", "TERM_PROGRAM", "TMUX"):
        monkeypatch.delenv(key, raising=False)
    FakeSession.stream_text = "HEADLESS_REMOTE_EXEC_SHORT"
    FakeSession.final_snapshot_text = "HEADLESS_REMOTE_EXEC_SHORT_OK"
    writes: list[tuple[str, str]] = []
    stdout = RecordingStream(writes, "stdout")
    stderr = RecordingStream(writes, "stderr", is_tty=True)
    options = HeadlessOptions(
        host="http://agent.test",
        output="jsonl",
        print_prompt="remote exec check",
        workspace=tmp_path,
        config=CLIConfig(),
        stdout=stdout,
        stderr=stderr,
    )

    exit_code = await HeadlessRunner(options).run()

    assert exit_code == 0
    records = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert [record["type"] for record in records] == ["ready", "event", "event", "complete"]
    assert records[1]["data"]["text"] == "HEADLESS_REMOTE_EXEC_SHORT"
    assert records[2]["data"]["text"] == "HEADLESS_REMOTE_EXEC_SHORT_OK"
    assert records[2]["data"]["meta"]["finished"] is True
    notification = "\x1b]777;notify;Agent Zero;Ready for input\x07"
    assert stderr.getvalue() == notification
    final_snapshot_write = next(
        index for index, write in enumerate(writes) if "HEADLESS_REMOTE_EXEC_SHORT_OK" in write[1]
    )
    complete_write = next(
        index for index, write in enumerate(writes) if '"type":"complete"' in write[1]
    )
    notification_write = writes.index(("stderr", notification))
    assert final_snapshot_write < complete_write < notification_write


async def test_print_mode_send_command_flushes_queue_and_waits(tmp_path: Path) -> None:
    FakeSession.initial_queue = [{"id": "queued-1", "text": "queued prompt"}]
    stdout = io.StringIO()
    stderr = io.StringIO()
    options = HeadlessOptions(
        host="http://agent.test",
        output="jsonl",
        print_prompt="/send",
        workspace=tmp_path,
        config=CLIConfig(),
        stdout=stdout,
        stderr=stderr,
    )

    exit_code = await HeadlessRunner(options).run()

    assert exit_code == 0
    records = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert [record["type"] for record in records] == ["ready", "event", "notice", "complete"]
    assert records[1]["data"]["text"] == "4"
    assert records[2]["message"] == "sent 1 queued message"
    assert FakeSession.instances[-1].queue_send_calls == [(None, True)]


async def test_completion_wait_stops_on_disconnect_without_timeout(tmp_path: Path) -> None:
    stdout = io.StringIO()
    stderr = io.StringIO()
    runner = HeadlessRunner(
        HeadlessOptions(
            host="http://agent.test",
            workspace=tmp_path,
            config=CLIConfig(),
            stdout=stdout,
            stderr=stderr,
        )
    )
    runner.session = FakeSession(
        CLIConfig(),
        runner,
        workspace=tmp_path,
        remote_file_write_enabled=True,
        remote_exec_enabled=True,
    )
    runner.session.agent_active = True

    wait_task = asyncio.create_task(runner._wait_for_completion())
    await asyncio.sleep(0)
    runner.on_disconnect()

    exit_code = await asyncio.wait_for(wait_task, timeout=1.0)

    assert exit_code == 1
    assert "DISCONNECTED" in stderr.getvalue()


async def test_non_tty_auth_failure_exits_two(tmp_path: Path) -> None:
    FakeSession.connect_error = SessionError(
        "AUTH_REQUIRED",
        "auth required: set A0_USERNAME/A0_PASSWORD or run the TUI once with remember host.",
        exit_code=2,
    )
    stdout = io.StringIO()
    stderr = io.StringIO()
    options = HeadlessOptions(
        host="http://agent.test",
        output="text",
        print_prompt="hello",
        workspace=tmp_path,
        config=CLIConfig(),
        stdin=io.StringIO(""),
        stdout=stdout,
        stderr=stderr,
    )

    exit_code = await HeadlessRunner(options).run()

    assert exit_code == 2
    assert "AUTH_REQUIRED" in stderr.getvalue()
    assert "A0_USERNAME/A0_PASSWORD" in stderr.getvalue()
