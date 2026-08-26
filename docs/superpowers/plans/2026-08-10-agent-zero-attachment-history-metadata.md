# Agent Zero Attachment History Metadata Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make user image attachments sent through `_a0_connector`'s WebSocket path replay with sanitized attachment filenames, matching the metadata available from the HTTP message path.

**Architecture:** Add one pure filename-to-log-metadata helper beside `WsConnector`, use it only when logging an accepted WebSocket user message, and prove the resulting log entry passes through the existing event bridge unchanged. This is an Agent Zero Core change; no image bytes, upload behavior, event type, or connector protocol field changes.

**Tech Stack:** Agent Zero Core Python 3.12+, builtin `plugins/_a0_connector`, Socket.IO WebSocket handler, pytest.

## Global Constraints

- Implement in the tracked Agent Zero Core repository, not in `a0-connector` and not only in an ignored live `usr/` copy.
- Read the Core root, `plugins/`, `plugins/_a0_connector/`, and `tests/` AGENTS files again in the execution worktree before editing.
- Preserve authentication, attachment validation, and existing `connector_send_message` result behavior.
- Store only sanitized attachment basenames in user log metadata.
- Strip directory components, URL query strings, and fragments so credentials or local paths cannot enter replay metadata.
- Do not send image bytes through Socket.IO or add a connector event type.
- Do not modify browser screenshot capture, materialization, consent, or privacy behavior.
- A message without attachments must retain `kvps={}`.
- Run focused Core tests before any runtime synchronization.
- Ask before committing, pushing, or changing/restarting a live Agent Zero runtime.
- Report tracked implementation, live-runtime synchronization, and cross-client replay as separate evidence surfaces.

---

## File Structure

- Modify: `plugins/_a0_connector/api/ws_connector.py` — sanitize accepted attachment refs and place basenames in the user log `kvps` only when non-empty.
- Create: `tests/test_a0_connector_attachment_metadata.py` — pure sanitization, handler logging, event bridge replay, no-attachment, and invalid-attachment regressions.
- Modify: `plugins/_a0_connector/AGENTS.md` — state that accepted WebSocket user attachments enter replay metadata as sanitized basenames only.

## Interface

Add this module-level helper beside `WsConnector`:

```python
def _attachment_log_metadata(attachments: list[str]) -> dict[str, list[str]]:
    """Return replay-safe attachment basenames for a user log entry."""
```

It is consumed by `WsConnector._handle_send_message()` and produces either:

```python
{"attachments": ["scan.png", "result.jpg"]}
```

or, when no safe names remain:

```python
{}
```

---

### Task 1: Preserve Sanitized WebSocket Attachment Names in Replay Metadata

**Files:**
- Modify: `plugins/_a0_connector/api/ws_connector.py`
- Create: `tests/test_a0_connector_attachment_metadata.py`
- Modify: `plugins/_a0_connector/AGENTS.md`

**Interfaces:**
- Consumes: the already normalized `attachments: list[str]` returned by `WsConnector._normalize_attachment_refs()`.
- Produces: `_attachment_log_metadata(attachments) -> dict[str, list[str]]`; the accepted user log call's `kvps` argument includes safe filenames.

- [ ] **Step 1: Write failing pure sanitization tests**

Create the test file with these imports and cases:

```python
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from helpers.ws_manager import WsResult
from plugins._a0_connector.api import ws_connector as ws_module
from plugins._a0_connector.api.ws_connector import (
    WsConnector,
    _attachment_log_metadata,
)
from plugins._a0_connector.helpers.event_bridge import log_entry_to_connector_event


def test_attachment_log_metadata_keeps_only_safe_basenames() -> None:
    assert _attachment_log_metadata(
        [
            "/a0/usr/uploads/scan.png",
            r"C:\\Users\\person\\result.jpg",
            "https://agent.test/api/image_get?path=/a0/usr/uploads/chart.webp&token=secret#view",
            "/a0/usr/uploads/",
            "",
        ]
    ) == {
        "attachments": ["scan.png", "result.jpg", "image_get"]
    }


def test_attachment_log_metadata_omits_empty_metadata() -> None:
    assert _attachment_log_metadata([]) == {}
    assert _attachment_log_metadata(["", "/"]) == {}
```

The URL test intentionally records `image_get`, not its `path` query value: query strings are removed wholesale so secret-bearing parameters never become log metadata. Normal A0 CLI uploads arrive as `/a0/usr/uploads/<filename>` and retain the uploaded filename.

- [ ] **Step 2: Write the failing handler and replay test**

Add a fake log/context and close the scheduled message coroutine so the test never starts an agent:

```python
class RecordingLog:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def log(self, **kwargs: object) -> None:
        self.calls.append(dict(kwargs))


@pytest.mark.asyncio
async def test_websocket_attachment_names_reach_replayed_user_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log = RecordingLog()
    context = SimpleNamespace(log=log)
    handler = WsConnector(None, None)
    monkeypatch.setattr(
        handler,
        "_resolve_context",
        AsyncMock(return_value=(context, "ctx-1")),
    )
    monkeypatch.setattr(
        ws_module,
        "subscribed_contexts_for_sid",
        lambda sid: {"ctx-1"} if sid == "sid-cli" else set(),
    )

    scheduled: list[bool] = []

    def close_scheduled(coroutine: object) -> SimpleNamespace:
        close = getattr(coroutine, "close")
        close()
        scheduled.append(True)
        return SimpleNamespace()

    monkeypatch.setattr(asyncio, "create_task", close_scheduled)

    result = await handler._handle_send_message(
        {
            "context_id": "ctx-1",
            "message": "Review these",
            "attachments": [
                "/a0/usr/uploads/scan.png",
                "/a0/usr/uploads/result.jpg",
            ],
            "client_message_id": "client-1",
        },
        "sid-cli",
    )

    assert result == {
        "context_id": "ctx-1",
        "status": "accepted",
        "client_message_id": "client-1",
    }
    assert scheduled == [True]
    assert log.calls == [
        {
            "type": "user",
            "heading": "",
            "content": "Review these",
            "kvps": {"attachments": ["scan.png", "result.jpg"]},
            "id": "client-1",
        }
    ]

    replayed = log_entry_to_connector_event(
        {"no": 0, **log.calls[0]},
        "ctx-1",
    )
    assert replayed["event"] == "user_message"
    assert replayed["data"]["meta"] == {
        "attachments": ["scan.png", "result.jpg"]
    }
```

- [ ] **Step 3: Write no-attachment and validation regressions**

Add a second handler test that sends only text and asserts the recorded `kvps` is `{}`. Use the same fake context and scheduled-coroutine closer from Step 2:

```python
@pytest.mark.asyncio
async def test_websocket_text_only_message_keeps_empty_kvps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log = RecordingLog()
    context = SimpleNamespace(log=log)
    handler = WsConnector(None, None)
    monkeypatch.setattr(
        handler,
        "_resolve_context",
        AsyncMock(return_value=(context, "ctx-1")),
    )
    monkeypatch.setattr(
        ws_module,
        "subscribed_contexts_for_sid",
        lambda sid: {"ctx-1"} if sid == "sid-cli" else set(),
    )

    def close_scheduled(coroutine: object) -> SimpleNamespace:
        getattr(coroutine, "close")()
        return SimpleNamespace()

    monkeypatch.setattr(asyncio, "create_task", close_scheduled)
    await handler._handle_send_message(
        {"context_id": "ctx-1", "message": "Text only"},
        "sid-cli",
    )
    assert log.calls[0]["kvps"] == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "code"),
    [
        (
            {
                "message": "image",
                "attachments": [{"path": "data:image/png;base64,AAAA"}],
            },
            "INVALID_ATTACHMENTS",
        ),
        ({"message": "", "attachments": []}, "MISSING_MESSAGE"),
    ],
)
async def test_websocket_rejected_attachments_do_not_reach_context(
    payload: dict[str, object],
    code: str,
) -> None:
    handler = WsConnector(None, None)
    result = await handler.process("connector_send_message", payload, "sid-cli")
    assert isinstance(result, WsResult)
    rendered = result.as_result(handler_id="test", fallback_correlation_id=None)
    assert rendered["ok"] is False
    assert rendered["error"]["code"] == code
```

For rejected cases no context patch is required because validation returns before `_resolve_context()`.

- [ ] **Step 4: Run the focused test and verify the helper is missing**

Run from the Agent Zero Core worktree:

```bash
pytest tests/test_a0_connector_attachment_metadata.py -v
```

Expected: collection fails because `_attachment_log_metadata` is not defined.

- [ ] **Step 5: Implement the pure helper and use it in the log call**

Add `urlsplit` to imports and implement:

```python
from urllib.parse import urlsplit


def _attachment_log_metadata(attachments: list[str]) -> dict[str, list[str]]:
    names: list[str] = []
    for attachment in attachments:
        normalized = str(attachment or "").strip().replace("\\", "/")
        if not normalized:
            continue
        parsed = urlsplit(normalized)
        path = parsed.path if parsed.scheme else normalized.split("?", 1)[0].split("#", 1)[0]
        name = path.rstrip("/").rsplit("/", 1)[-1]
        if name and name not in {".", ".."}:
            names.append(name)
    return {"attachments": names} if names else {}
```

Change only the existing accepted-message log call:

```python
context.log.log(
    type="user",
    heading="",
    content=message,
    kvps=_attachment_log_metadata(attachments),
    id=message_id,
)
```

Do not change `_normalize_attachment_refs()`, `_run_message()`, `UserMessage.attachments`, WebSocket results, or event bridge mapping.

- [ ] **Step 6: Run focused connector regressions**

```bash
pytest tests/test_a0_connector_attachment_metadata.py -v
pytest tests/test_a0_connector_launcher_gateway.py tests/test_a0_connector_computer_use_metadata.py tests/test_a0_connector_prompt_gating.py -v
```

Expected: all tests pass; the new test shows replay metadata contains basenames and rejected attachments remain rejected.

- [ ] **Step 7: Run the DOX pass and broader test suite**

Re-read the applicable AGENTS chain. Add the accepted-WebSocket-attachment replay rule to `plugins/_a0_connector/AGENTS.md`; the existing tests DOX already owns all focused connector tests and needs no change. Then run:

```bash
pytest
git diff --check
git status --short
```

Expected: full Core tests pass, diff check is clean, and only the handler, focused test, and any justified DOX lines are modified.

- [ ] **Step 8: Commit the tracked Core correction after approval**

```bash
git add plugins/_a0_connector/api/ws_connector.py plugins/_a0_connector/AGENTS.md tests/test_a0_connector_attachment_metadata.py
git commit -m "fix: preserve connector attachment history metadata"
```

---

### Task 2: Validate the Intended Live Runtime and Cross-Client Replay

**Files:**
- No tracked source changes.
- Runtime target: the exact Agent Zero Core installation named and approved by the user at execution time.

**Interfaces:**
- Consumes: the tested Core commit from Task 1 and the intended runtime's existing plugin/restart workflow.
- Produces: separate evidence that the runtime loaded the correction and a WebSocket-sent attachment survives reconnect/history replay.

- [ ] **Step 1: Obtain the exact runtime target and restart authority**

Before copying or restarting anything, record the user-approved runtime identity, tracked Core checkout path, live `plugins/_a0_connector/api/ws_connector.py` path, and runtime-specific restart command. If any of these four values is unknown or multiple candidates exist, stop this task and ask the user; do not select a container or installation by port guessing.

- [ ] **Step 2: Compare tracked and live source before mutation**

Run read-only checks using the exact paths established in Step 1:

```bash
git rev-parse --show-toplevel
git rev-parse HEAD
shasum -a 256 plugins/_a0_connector/api/ws_connector.py
read -r -p "Approved live ws_connector.py path: " a0_live_connector_file
test "${a0_live_connector_file#/}" != "$a0_live_connector_file"
test -f "$a0_live_connector_file"
shasum -a 256 "$a0_live_connector_file"
```

Record whether the live file is already identical and preserve unrelated runtime edits. Keep `a0_live_connector_file` scoped to this shell task; do not reuse a common system variable.

- [ ] **Step 3: Synchronize only the tested handler when needed**

If the files differ solely because the live copy lacks the tested commit, copy the tracked handler through the runtime's normal plugin synchronization mechanism. Re-run both hashes and require equality. If the live file has unrelated edits, stop and reconcile those edits in the tracked Core worktree before synchronization; do not overwrite them.

- [ ] **Step 4: Restart or reload the exact runtime and verify plugin health**

Run the restart command approved in Step 1. Discover the actual WebUI/connector URL from that runtime's output or mapping. Verify the connector capabilities endpoint and `/ws` handler load without assuming `localhost:50001`.

- [ ] **Step 5: Perform the replay acceptance path**

With the updated runtime:

1. Upload an image through the A0 CLI and send it in a user message.
2. Confirm the live user event contains `data.meta.attachments == [<sanitized filename>]` and contains no bytes, base64, local directory, query, cookie, or token.
3. Disconnect and reconnect the CLI to the same chat.
4. Confirm the replayed user message contains the same attachment filename.
5. Open the same chat in the WebUI and confirm its existing attachment behavior remains intact.
6. Send a text-only CLI message and confirm its event shape remains unchanged.

- [ ] **Step 6: Report evidence without conflating surfaces**

Report:

- tracked Core commit and focused/full test results;
- live handler hash and restart/reload result;
- CLI reconnect/history replay result; and
- WebUI regression result.

Do not report the A0 CLI native TGP/Sixel/half-cell rendering as accepted from this Core-only task.

---

## Final Verification Checklist

- [ ] WebSocket attachment validation is unchanged.
- [ ] Accepted attachment messages log only sanitized basenames.
- [ ] Text-only messages retain `kvps={}`.
- [ ] Event bridge replay exposes `data.meta.attachments` without a schema change.
- [ ] No image bytes, local directory, URL query, fragment, cookie, or token enters the event.
- [ ] Focused connector regressions and the full Core suite pass.
- [ ] Tracked implementation and live runtime are synchronized only after explicit approval.
- [ ] CLI reconnect and WebUI regression evidence are recorded separately.
