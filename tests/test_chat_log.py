from __future__ import annotations

from types import SimpleNamespace

from rich.padding import Padding

from agent_zero_cli.widgets.chat_log import ChatLog, StatusEntry, sanitize_status_meta


def test_append_or_update_new_entry_auto_scrolls_by_default(
    monkeypatch,
) -> None:
    log = ChatLog(id="chat-log")
    mount_calls: list[object] = []
    scroll_end_calls: list[bool] = []

    monkeypatch.setattr(log, "mount", lambda widget, before=None, after=None: mount_calls.append(widget))
    monkeypatch.setattr(log, "call_after_refresh", lambda callback: callback())
    monkeypatch.setattr(log, "scroll_end", lambda animate=False: scroll_end_calls.append(animate))
    monkeypatch.setattr(log, "is_at_bottom", lambda: False)

    log.append_or_update(1, "hello", scroll=True)

    assert len(mount_calls) == 1
    assert scroll_end_calls == [False]


def test_append_or_update_new_entry_does_not_force_scroll_when_auto_follow_paused(
    monkeypatch,
) -> None:
    log = ChatLog(id="chat-log")
    mount_calls: list[object] = []
    scroll_end_calls: list[bool] = []

    monkeypatch.setattr(log, "mount", lambda widget, before=None, after=None: mount_calls.append(widget))
    monkeypatch.setattr(log, "call_after_refresh", lambda callback: callback())
    monkeypatch.setattr(log, "scroll_end", lambda animate=False: scroll_end_calls.append(animate))
    log._auto_follow = False
    monkeypatch.setattr(log, "is_at_bottom", lambda: False)

    log.append_or_update(1, "hello", scroll=True)

    assert len(mount_calls) == 1
    assert scroll_end_calls == []


def test_append_or_update_existing_entry_resumes_auto_follow_when_back_at_bottom(
    monkeypatch,
) -> None:
    log = ChatLog(id="chat-log")
    existing = SimpleNamespace(
        update=lambda renderable: None,
    )
    log._seq_to_widget[7] = existing
    log._auto_follow = False
    scroll_end_calls: list[bool] = []

    monkeypatch.setattr(log, "call_after_refresh", lambda callback: callback())
    monkeypatch.setattr(log, "scroll_end", lambda animate=False: scroll_end_calls.append(animate))
    monkeypatch.setattr(log, "is_at_bottom", lambda: False)

    log.append_or_update(7, "updated", scroll=True)
    assert scroll_end_calls == []

    monkeypatch.setattr(log, "is_at_bottom", lambda: True)
    log.append_or_update(7, "updated again", scroll=True)
    assert scroll_end_calls == [False]
    assert log._auto_follow is True


def test_status_entry_uses_left_padding() -> None:
    entry = StatusEntry()

    entry.set_status("Thinking", "step", {"thoughts": ["First thought"]}, active=True)

    assert isinstance(entry.content, Padding)
    assert entry.content.left == 2


def test_sanitize_status_meta_extracts_kvps_and_truncated_thoughts() -> None:
    rows, thoughts, hidden_thoughts = sanitize_status_meta(
        {
            "headline": "Test confirmed with full context - all systems operational",
            "step": "Using response...",
            "tool_name": "response",
            "tool_args": {
                "text": "x" * 220,
                "temperature": 0.2,
                "options": {"verbose": True},
            },
            "thoughts": [
                "First thought about the task at hand.",
                "Second thought with\nextra whitespace.",
            ]
            + [f"Overflow thought {index}" for index in range(10)],
        }
    )

    assert ("headline", "Test confirmed with full context - all systems operational") in rows
    assert ("tool", "response") in rows
    assert ("arg.temperature", "0.2") in rows
    assert ("arg.options", "1 key") in rows
    assert thoughts[0] == "First thought about the task at hand."
    assert thoughts[1] == "Second thought with extra whitespace."
    assert len(thoughts) == 6
    assert hidden_thoughts == 6
