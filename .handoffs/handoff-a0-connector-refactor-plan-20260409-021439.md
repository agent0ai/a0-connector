# app.py Refactoring Plan

**Goal**: Break the 1585-line monolith `src/agent_zero_cli/app.py` into focused modules.
**Constraint**: The `AgentZeroCLI(App)` class stays in `app.py` as the thin orchestrator. Methods move to free functions in new modules that receive `app: AgentZeroCLI` as their first argument (same pattern already used by `model_commands.py` and `token_usage.py`).

---

## Context: What Already Exists

These modules already follow the target pattern and should NOT be touched:

| Module | Responsibility |
|--------|---------------|
| `client.py` | All HTTP + WebSocket transport (A0Client) |
| `model_commands.py` | Model preset/runtime screen flows |
| `model_config.py` | Model switcher state parsing, provider options |
| `token_usage.py` | Token refresh loop, compaction stats polling |
| `rendering.py` | Event rendering, category maps, status labels |
| `commands.py` | CommandSpec + CommandAvailability dataclasses |
| `remote_exec.py` | PythonTTYManager for remote code execution |
| `remote_files.py` | RemoteFileUtility, tree snapshots, file ops |

---

## Agent Zero Duplication Analysis

The connector plugin (`plugin/a0_connector/`) is NOT duplicating Agent Zero code. It is a necessary adapter layer:

- `plugin/pause.py` wraps `agentdocker/api/pause.py` adding API-key auth + structured error responses
- `plugin/nudge.py` wraps `agentdocker/api/nudge.py` adding validation + connector response format
- `plugin/settings_get.py` / `settings_set.py` wrap native settings with API-key protection
- `plugin/ws_connector.py` provides the entire streaming protocol (subscribe, stream events, file ops, exec ops) which has no equivalent in Agent Zero's native WebUI websocket

The CLI's `client.py` calls these plugin endpoints, not the native Agent Zero APIs directly. This three-layer architecture (CLI -> connector plugin -> Agent Zero core) is correct and intentional.

**No code should be removed from the plugin as "duplication".**

---

## Extraction Plan (6 new modules)

All new files go in `src/agent_zero_cli/`. All extracted functions are free functions taking `app: AgentZeroCLI` as the first parameter. The `app.py` class retains thin one-liner delegations for Textual event handlers and action methods.

---

### 1. `connection.py` — Connection Lifecycle

**Extract from app.py** (lines 662-931, ~270 lines):

```
async def startup(app) -> None
async def begin_connection(app, host, *, username, password, save_credentials_flag) -> None
async def fetch_capabilities(app) -> tuple[dict | None, bool, str]
def validate_capabilities(capabilities, protocol_version, ws_namespace, ws_handler) -> None
def set_connected(app, value: bool) -> None
async def disconnect_and_exit(app) -> None
```

**What stays in app.py**: 
- `_startup` becomes `async def _startup(self): await connection.startup(self)`
- `_set_connected` becomes `def _set_connected(self, v): connection.set_connected(self, v)`
- Same for `_begin_connection`, `_disconnect_and_exit`

**Constants that move**: `_DEFAULT_HOST`, `_PROTOCOL_VERSION`, `_WS_NAMESPACE`, `_WS_HANDLER` (already defined in `client.py` too — consolidate into `client.py` and import from there, removing the duplicates from `app.py`).

**Implementation notes**:
- The `begin_connection` function is the largest single method (~165 lines). It handles: capability probe, login flow, API key verification, WebSocket connect, initial chat creation, subscription. It reads/writes `app.config`, `app.client`, `app.capabilities`, `app.connector_features`, `app.current_context`, `app.connected`. All of these are public attributes, so a free function can access them.
- The callback wiring (`app.client.on_connect = ...`) stays in `begin_connection` since it references app methods.

---

### 2. `event_handlers.py` — WebSocket Event Processing

**Extract from app.py** (lines 932-1079, ~150 lines):

```
def handle_context_snapshot(app, data: dict) -> None
def handle_context_event(app, data: dict) -> None
def handle_context_complete(app, data: dict) -> None
def handle_connector_error(app, data: dict) -> None
def handle_file_op(app, data: dict) -> dict
async def handle_exec_op(app, data: dict) -> dict
def start_remote_tree_publisher(app) -> None
def stop_remote_tree_publisher(app) -> None
async def remote_tree_publish_loop(app) -> None
async def publish_remote_tree_snapshot(app) -> None
```

**What stays in app.py**:
- `_handle_context_snapshot` -> one-liner delegation
- `_handle_context_event` -> one-liner delegation
- etc.

**Implementation notes**:
- `handle_context_event` is the most complex handler. It reads `app._compaction_refresh_context`, `app._context_run_complete`, `app._pause_latched`, writes `app.agent_active`. All public/pseudo-public attrs.
- The remote tree publishing loop is thematically related (it's an async background task driven by WS connection state) and fits here better than in `remote_files.py` which is about local FS operations.

---

### 3. `chat_commands.py` — Chat-Related Command Implementations

**Extract from app.py** (lines 1231-1384, 1485-1557, ~220 lines):

```
async def cmd_help(app) -> None
async def cmd_keys(app) -> None
async def cmd_quit(app) -> None
async def cmd_clear(app) -> None
async def cmd_chats(app) -> None
async def cmd_new(app) -> None
async def cmd_settings(app) -> None
async def cmd_pause(app) -> None
async def cmd_resume(app) -> None
async def cmd_nudge(app) -> None
async def switch_context(app, context_id, *, has_messages_hint) -> None
```

**What stays in app.py**:
- `_cmd_help(self)` -> `await chat_commands.cmd_help(self)`
- Same pattern for all others
- The Textual action methods (`action_clear_chat`, `action_list_chats`, etc.) stay as thin wrappers

**Implementation notes**:
- `cmd_chats` uses `push_screen_wait(ChatListScreen(...))` which is a Textual method on `app`. Free function calls `await app.push_screen_wait(...)` — this works fine.
- `cmd_compact` is complex enough to warrant its own module (see below).

---

### 4. `compaction.py` — Compaction Flow

**Extract from app.py** (lines 1275-1341, 1418-1483, ~130 lines):

```
def cancel_compaction_refresh(app) -> None
def finalize_compaction_refresh(app, context_id: str) -> None
def begin_compaction_refresh(app, context_id: str) -> None
async def wait_for_compaction_and_reload(app, context_id: str) -> None
async def cmd_compact(app) -> None
```

**Constants that move**: `_COMPACTION_POLL_INTERVAL_SECONDS`, `_COMPACTION_POLL_TIMEOUT_SECONDS`

**What stays in app.py**:
- `_cmd_compact(self)` -> `await compaction.cmd_compact(self)`
- `_begin_compaction_refresh(self, cid)` -> `compaction.begin_compaction_refresh(self, cid)`
- `_cancel_compaction_refresh(self)` -> `compaction.cancel_compaction_refresh(self)`

---

### 5. `availability.py` — Command Availability Predicates

**Extract from app.py** (lines 564-648, ~85 lines):

```
def require_connection(app) -> CommandAvailability
def require_context(app) -> CommandAvailability
def require_features(app, *features) -> CommandAvailability
def compact_availability(app) -> CommandAvailability
def pause_availability(app) -> CommandAvailability
def resume_availability(app) -> CommandAvailability
def pause_toggle_availability(app) -> CommandAvailability
def nudge_availability(app) -> CommandAvailability
def model_presets_availability(app) -> CommandAvailability
def model_runtime_availability(app) -> CommandAvailability
```

**What stays in app.py**:
- `_require_connection(self)` -> `availability.require_connection(self)`
- The command registry lambdas change from `lambda app: app._require_features(...)` to `lambda app: availability.require_features(app, ...)`

**Implementation notes**:
- These are pure predicates that read `app.connected`, `app.connector_features`, `app.current_context`, `app.agent_active`, `app._model_switch_allowed`, `app._pause_latched`. All accessible from free functions.

---

### 6. `splash_helpers.py` — Splash State Management

**Extract from app.py** (lines 306-432, ~130 lines):

```
def splash_host(app) -> str
def normalize_host(host: str) -> str
def set_splash_state(app, **changes) -> None
def sync_workspace_widgets(app) -> None
def set_workspace_context(app, *, local_workspace, remote_workspace) -> None
def set_splash_stage(app, stage, *, message, detail, host, ...) -> None
def sync_ready_actions(app) -> None
def sync_body_mode(app) -> None
def sync_composer_visibility(app) -> None
def focus_splash_primary(app) -> None
def focus_message_input(app) -> None
def show_notice(app, message, *, error) -> None
def welcome_actions(app) -> tuple[SplashAction, ...]
def surface_help(app) -> None
def available_help_lines(app) -> tuple[list[str], list[str]]
async def refresh_workspace_from_settings(app) -> None
```

**What stays in app.py**:
- All these become thin one-liner delegations

---

## Execution Order

Execute in this order to minimize merge conflicts and allow incremental testing:

### Phase 1: Extract pure logic (no UI coupling)
1. **`availability.py`** — Pure predicates, zero side effects, easiest to test in isolation
2. **`compaction.py`** — Self-contained async flow with clear boundaries

### Phase 2: Extract event processing
3. **`event_handlers.py`** — WebSocket event handlers + remote tree loop

### Phase 3: Extract command implementations
4. **`chat_commands.py`** — All slash-command implementations

### Phase 4: Extract connection + splash
5. **`connection.py`** — Connection lifecycle (depends on splash helpers)
6. **`splash_helpers.py`** — UI state management (depends on availability)

### Phase 5: Cleanup
7. **Consolidate constants** — Remove `_DEFAULT_HOST`, `_PROTOCOL_VERSION`, `_WS_NAMESPACE`, `_WS_HANDLER` from `app.py`; import from `client.py`
8. **Update `_build_command_registry`** — Point lambdas at new module functions
9. **Run tests** — `pytest tests/ -v`

---

## Constant Consolidation

These constants are duplicated between `app.py` and `client.py`:

| Constant | `app.py` | `client.py` |
|----------|----------|-------------|
| `_PROTOCOL_VERSION` | `"a0-connector.v1"` | `"a0-connector.v1"` |
| `_WS_NAMESPACE` | `"/ws"` | `"/ws"` |
| `_WS_HANDLER` | `"plugins/a0_connector/ws_connector"` | `"plugins/a0_connector/ws_connector"` |

**Action**: Keep them in `client.py` (single source of truth for protocol constants), export as public names, import in `connection.py` where needed. Remove from `app.py`.

---

## What app.py Looks Like After

~300-350 lines containing:
- Class definition, `__init__`, `compose()`, reactive declarations, bindings
- Thin one-liner delegations for every extracted method
- Textual event listener methods (`on_*`) that delegate to the appropriate module
- Textual action methods (`action_*`) that delegate
- `_build_command_registry` pointing at the new modules
- Widget query helpers (`_set_activity`, `_set_idle`, `_set_token_usage`, `_clear_token_usage`) — these are so thin (1-2 lines) they stay

---

## Test Impact

- `tests/test_app.py` uses `dummy_app` with `FakeInput`/`FakeRichLog` stubs. The delegation pattern means tests still call `app._cmd_pause()` etc. and the delegation forwards to the free function. **No test changes needed for the extraction itself.**
- New modules can get their own test files later but are not required for this refactor.
- **Verification**: Run `pytest tests/ -v` after each phase.

---

## File Sizes After (estimated)

| File | Lines | 
|------|-------|
| `app.py` | ~300-350 |
| `connection.py` | ~270 |
| `event_handlers.py` | ~150 |
| `chat_commands.py` | ~220 |
| `compaction.py` | ~130 |
| `availability.py` | ~85 |
| `splash_helpers.py` | ~130 |

---

## Feature Improvement A: Eliminate Compaction Polling

### Problem

The current CLI compaction flow uses a polling loop (`_wait_for_compaction_and_reload`) that hits `GET compact_chat?action=stats` every 0.75s for up to 180s to detect when compaction finishes. This is wasteful.

### How Agent Zero Core Does It

Agent Zero's native `compact_chat` API handler (in `plugins/_chat_compaction/api/compact_chat.py`) calls `context.run_task(_run_compaction_task, ...)` which is a `DeferredTask`. The compaction function `run_compaction()` in `compactor.py`:

1. Runs the LLM call(s) to summarize
2. Replaces history
3. Resets the context log via `context.log.reset()`
4. Logs a `type="response"` entry with the compacted content
5. Calls `mark_dirty_all()` which triggers state pushes

The connector's `ws_connector.py` already streams ALL context log events via `_stream_events()` (polling `get_context_log_entries()` every 0.5s). So the compaction progress and completion are already observable through the normal event stream.

### What the Connector Already Streams During Compaction

When `run_compaction()` runs, these log entries appear in the event stream:
1. `type="info"` heading="Compacting chat history..." — **streamed as `EVENT_INFO`**
2. Streaming content updates via `log_item.stream(content=chunk)` — **streamed as `EVENT_INFO` updates**
3. `context.log.reset()` — **clears the log, next poll returns empty**
4. `type="response"` heading="Context compacted" — **streamed as `EVENT_ASSISTANT_MESSAGE`**

### The Fix (Server-Side + Client-Side)

**Server-side (`plugin/a0_connector/api/v1/compact_chat.py`)**: After calling `context.run_task(...)`, the task ID or a "compaction_started" flag is already implicit. No change needed here — the event stream already carries everything.

**Client-side (`compaction.py` in the new module)**: Replace the polling loop with event-driven detection:

```
async def begin_compaction_refresh(app, context_id):
    # Instead of polling compact_chat?action=stats repeatedly:
    # 1. Set app state to "compacting" (disable input, show activity)
    # 2. The existing _handle_context_event handler will receive:
    #    - info events (progress updates) -> shown in chat
    #    - The log.reset() causes the stream to return empty -> next snapshot is fresh
    #    - response event ("Context compacted") -> triggers reload
    # 3. On the next "connector_context_complete" or when we see the
    #    "response" event with heading containing "compacted", reload the context
    
    app._compaction_refresh_context = context_id
    app._set_pause_latched(False)
    app.agent_active = True
    app._sync_ready_actions()
    input_widget = app.query_one("#message-input", ChatInput)
    input_widget.disabled = True
    app._set_activity("Compacting chat history", "Updating context")
    
    # No polling task needed — event_handlers.py will detect compaction 
    # completion via the "response" event with "Context compacted" heading
    # or "connector_context_complete" event, then call finalize_compaction_refresh()
```

**In `event_handlers.py`**: Add detection logic in `handle_context_event`:

```python
# Inside handle_context_event, after processing the event:
if app._compaction_refresh_context == context_id:
    # Compaction streams info/response events directly through the normal flow.
    # When we get a "response" event, compaction is done.
    if event_type == "assistant_message":
        # Compaction completed — reload the context to get clean state
        asyncio.create_task(_compaction_context_reload(app, context_id))
```

Where `_compaction_context_reload` does what `_wait_for_compaction_and_reload` currently does after detecting completion: call `switch_context(app, context_id, has_messages_hint=True)` and `finalize_compaction_refresh`.

**What gets deleted**:
- `_COMPACTION_POLL_INTERVAL_SECONDS` and `_COMPACTION_POLL_TIMEOUT_SECONDS`
- `_wait_for_compaction_and_reload()` (the entire polling loop)
- `_compaction_refresh_task` attribute (no background polling task needed)

**What changes in `_handle_context_event`**:
- Currently: `if self._compaction_refresh_context == context_id and event_type != "error": return` (suppresses all events during compaction)
- After: Let events through normally during compaction (show progress), but detect the response event as the completion signal

### Risk Mitigation

The current code already suppresses events during compaction (`event_type != "error": return`). The compaction emits events via `context.log.log()` which ARE picked up by the stream loop in `ws_connector.py`. The key insight is that `run_compaction()` calls `context.log.reset()` followed by a `type="response"` log — this means the stream will naturally deliver a response event when done. The only edge case is if compaction errors out, in which case a `type="error"` event is logged — which the current code already lets through.

---

## Feature Improvement B: Token Counting from Agent Zero Core

### Problem

The current `token_usage.py` polls `compact_chat?action=stats` every 0.5s to get `token_count`. This:
1. Requires the `_chat_compaction` plugin to be present (gated behind `"compact_chat" in connector_features`)
2. Is expensive — it calls `tokens.approximate_tokens(output_text(agent.history.output()))` server-side every 0.5s
3. Does NOT show the model's context window limit (no `ctx_length` anywhere in the connector)
4. Duplicates what Agent Zero core already computes and stores

### How Agent Zero Core Already Tracks This

In `agent.py` lines 582-589, every message loop iteration stores:

```python
self.set_data(
    Agent.DATA_NAME_CTX_WINDOW,  # = "ctx_window"
    {
        "text": full_text,     # full prompt text sent to LLM
        "tokens": tokens.approximate_tokens(full_text),
    },
)
```

This is the ACTUAL token count of the context window as sent to the LLM (system prompt + history + extras). The existing `api/ctx_window_get.py` endpoint exposes it:

```python
agent = context.streaming_agent or context.agent0
window = agent.get_data(agent.DATA_NAME_CTX_WINDOW)
return {"content": text, "tokens": tokens}
```

The model's `ctx_length` (context window size) is available in the model config:
- `plugins/_model_config/helpers/model_config.py`: `get_chat_model_config(agent)` returns a dict with `ctx_length`
- `models.py`: `ModelConfig.ctx_length: int = 0`

### The Fix

#### Server-Side: New connector endpoint or extend model_switcher

**Option A (preferred)**: Add a lightweight `token_status` endpoint to the connector plugin.

Create `plugin/a0_connector/api/v1/token_status.py`:

```python
"""POST /api/plugins/a0_connector/v1/token_status."""
from __future__ import annotations
from helpers.api import Request, Response
import usr.plugins.a0_connector.api.v1.base as connector_base

class TokenStatus(connector_base.ProtectedConnectorApiHandler):
    async def process(self, input: dict, request: Request) -> dict | Response:
        from agent import AgentContext, Agent

        context_id = str(input.get("context_id", "")).strip()
        if not context_id:
            return Response(response='{"error":"context_id required"}', status=400, mimetype="application/json")

        context = AgentContext.get(context_id)
        if context is None:
            return Response(response='{"error":"Context not found"}', status=404, mimetype="application/json")

        agent = context.streaming_agent or context.agent0

        # Token count from core's ctx_window data (updated every LLM call)
        window = agent.get_data(Agent.DATA_NAME_CTX_WINDOW)
        token_count = int(window["tokens"]) if isinstance(window, dict) and "tokens" in window else None

        # Context window limit from model config
        ctx_length = None
        try:
            from plugins._model_config.helpers.model_config import get_chat_model_config
            chat_cfg = get_chat_model_config(agent)
            raw = int(chat_cfg.get("ctx_length", 0))
            if raw > 0:
                ctx_length = raw
        except Exception:
            pass

        return {
            "ok": True,
            "context_id": context_id,
            "token_count": token_count,
            "context_window": ctx_length,
        }
```

Also register `"token_status"` in the capabilities features list (`plugin/a0_connector/api/v1/capabilities.py`).

#### Client-Side: Update `token_usage.py`

Update `refresh_token_usage()` to call the new `token_status` endpoint instead of `compact_chat?action=stats`:

```python
async def refresh_token_usage(app, *, context_id=None, silent=True):
    if "token_status" not in app.connector_features:
        # Fallback to compaction stats for older connector builds
        # (keep existing logic as fallback)
        ...
        return
    
    target_context = context_id or app.current_context
    if not target_context:
        app._clear_token_usage()
        return

    try:
        payload = await app.client.get_token_status(target_context)
    except Exception:
        if not silent:
            app._show_notice("Failed to refresh token usage.", error=True)
        return

    token_count = payload.get("token_count")
    if token_count is None:
        return
    
    context_window = payload.get("context_window")  # This is the ctx_length!
    app._set_token_usage(token_count, context_window)
```

Add `get_token_status()` to `client.py`:

```python
async def get_token_status(self, context_id: str) -> dict[str, Any]:
    response = await self._post("token_status", {"context_id": context_id})
    if response.status_code >= 400:
        return {"ok": False, "message": self._response_message(response)}
    data = self._json(response)
    if "ok" not in data:
        data["ok"] = True
    return data
```

### Benefits

1. **No dependency on compaction plugin** for token counting
2. **Shows actual LLM context usage** (system + history + extras) not just history text
3. **Exposes `ctx_length`** (model's context window limit) for the first time — the ConnectionStatus widget can now show "12.4k / 128k tokens"
4. **Much cheaper server-side** — reads pre-computed `agent.data["ctx_window"]` instead of re-tokenizing the full history every poll
5. **`token_count` will be `None` before the first LLM call** (e.g., fresh chat). The widget should handle this gracefully (show nothing or "—")

### What About the Polling Interval?

The current 0.5s polling of `token_status` is reasonable because:
- `agent.get_data()` is a dict lookup (zero computation)
- `get_chat_model_config()` is also a fast config lookup
- The response is tiny (~100 bytes)

However, consider increasing the interval to 2-4s since token counts only change after each LLM call (not continuously). This reduces network traffic 4-8x.

### Note on `token_count` being `None`

`Agent.DATA_NAME_CTX_WINDOW` is only set during `_prepare_prompt()` which runs inside the message loop. For a freshly created chat with no messages yet, `agent.get_data("ctx_window")` returns `None`. The endpoint returns `token_count: null` in this case. The CLI should treat null as "no data yet" and display nothing or a placeholder.

After the first user message triggers an LLM call, the token count becomes available and updates on every subsequent loop iteration.

---

## Rules for the Implementing Agent

1. **Do NOT rewrite logic.** Move functions verbatim, only changing `self` to `app` parameter and `self.xxx` to `app.xxx`.
2. **Do NOT rename anything** except `self` -> `app` in extracted functions.
3. **Do NOT change the public interface** of `AgentZeroCLI`. All `_method` names stay as thin wrappers.
4. **Do NOT touch** `model_commands.py`, `model_config.py`, `rendering.py`, `commands.py`, `remote_exec.py`, `remote_files.py`, or any widget/screen files — UNLESS implementing Feature B (which touches `client.py` and `token_usage.py`).
5. **Run `pytest tests/ -v` after each phase** to catch regressions immediately.
6. **Import pattern**: Use `from __future__ import annotations` at top of every new module. Use `TYPE_CHECKING` guard for the `AgentZeroCLI` type hint to avoid circular imports:
   ```python
   from __future__ import annotations
   from typing import TYPE_CHECKING
   if TYPE_CHECKING:
       from agent_zero_cli.app import AgentZeroCLI
   ```
7. **Constants**: When moving constants out of `app.py`, define them in the most relevant new module (e.g., compaction poll intervals in `compaction.py`).
8. **Feature A and B** should be done AFTER the extraction phases, as a final phase. The extraction is a pure structural refactor; the features are behavioral changes.

---

## Updated Execution Order

### Phase 1: Extract pure logic (no UI coupling)
1. **`availability.py`** — Pure predicates, zero side effects
2. **`compaction.py`** — Self-contained async flow (initially with existing polling logic)

### Phase 2: Extract event processing
3. **`event_handlers.py`** — WebSocket event handlers + remote tree loop

### Phase 3: Extract command implementations
4. **`chat_commands.py`** — All slash-command implementations

### Phase 4: Extract connection + splash
5. **`connection.py`** — Connection lifecycle
6. **`splash_helpers.py`** — UI state management

### Phase 5: Cleanup
7. **Consolidate constants** — Remove duplicates from `app.py`; import from `client.py`
8. **Update `_build_command_registry`** — Point lambdas at new module functions
9. **Run tests** — `pytest tests/ -v`

### Phase 6: Feature improvements
10. **Feature B: Token Status** — Create `plugin/.../token_status.py`, update `client.py` and `token_usage.py`
11. **Feature A: Compaction without polling** — Update `compaction.py` and `event_handlers.py`
12. **Run tests** — `pytest tests/ -v`

---

## Key Files in Agent Zero Core (Reference)

These files are in the Agent Zero instance at `/home/eclypso/agentdocker/` and deployed at `/a0/` in Docker. They are READ-ONLY references for understanding the server-side behavior:

| File | What it does |
|------|-------------|
| `agent.py:348` | `Agent.DATA_NAME_CTX_WINDOW = "ctx_window"` |
| `agent.py:582-589` | Stores `{text, tokens}` in `ctx_window` every loop iteration |
| `agent.py:247-248` | `is_running()` checks `self.task.is_alive()` |
| `agent.py:271-279` | `run_task()` creates a `DeferredTask` |
| `models.py:74` | `ModelConfig.ctx_length: int = 0` |
| `helpers/tokens.py` | `count_tokens()` uses tiktoken, `approximate_tokens()` adds 1.1x buffer |
| `api/ctx_window_get.py` | Existing endpoint that reads `agent.data["ctx_window"]` |
| `plugins/_chat_compaction/helpers/compactor.py` | `run_compaction()` — resets log, emits response, calls `mark_dirty_all()` |
| `plugins/_chat_compaction/api/compact_chat.py` | Handler: `context.run_task(_run_compaction_task, ...)` |
| `plugins/_model_config/helpers/model_config.py` | `get_chat_model_config(agent)` returns dict with `ctx_length` |
