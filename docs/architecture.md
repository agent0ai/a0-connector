# Architecture

## Components

```
+-------------+     HTTP POST /login + session cookie     +------------------------------+
|    a0 CLI   | ----------------------------------------> | Agent Zero + _a0_connector   |
|             |                                           | plugin                        |
|             |     Socket.IO /ws namespace               |                              |
|             | <---------------------------------------- |                              |
+-------------+          connector_* events               +------------------------------+
```

- CLI (`a0`): Textual TUI, published as the `a0` package and installed as the `a0` command.
- Plugin (`_a0_connector`): builtin Agent Zero Core plugin.

## Startup flow

1. Discover: `POST /api/plugins/_a0_connector/v1/capabilities`
2. Validate: confirm protocol, `/ws`, handler activation, `auth == ["session"]`, and boolean `auth_required`
3. Authenticate if needed: for protected instances, reuse any valid in-memory session or `POST /login` with form data
4. Verify: probe `chats_list` to confirm the session is valid
5. Connect: Socket.IO to `/ws` with `auth: {handlers: ["plugins/_a0_connector/ws_connector"]}` and the current session cookie forwarded in headers
6. Hello: send `connector_hello` and receive protocol, features, and `exec_config`
7. Chat: create context, subscribe, stream events

Open instances (`AUTH_LOGIN` unset) skip step 3 entirely.

## Protocol

- Version: `a0-connector.v1`
- Transport: Engine.IO at `/socket.io`, Socket.IO namespace `/ws`
- Auth contract: `auth == ["session"]`
- Capability flag: `auth_required: bool` derived from Agent Zero core web-auth state
- WebSocket activation: `auth.handlers` contains `plugins/_a0_connector/ws_connector`

## HTTP routes

All routes: `POST /api/plugins/_a0_connector/v1/<endpoint>`

| Endpoint | Auth | Purpose |
|----------|------|---------|
| `capabilities` | Public | Discovery: protocol, features, session contract, `auth_required` |
| `chat_create` | Session | Create a new chat context |
| `chats_list` | Session | List existing contexts |
| `chat_get` | Session | Get a single context |
| `chat_reset` | Session | Reset a context |
| `chat_delete` | Session | Delete a context |
| `pause` | Session | Pause the currently running context |
| `nudge` | Session | Continue a stopped or paused context run |
| `message_send` | Session | Send a message with optional base64 attachments |
| `log_tail` | Session | Paginated context log entries |
| `projects` | Session | Project list, activate, deactivate, load, update |
| `settings_get` | Session | Optional runtime settings surface |
| `settings_set` | Session | Optional runtime settings surface |
| `agents_list` | Session | Optional agent-profile list |
| `skills_list` | Session | Optional installed-skill list |
| `skills_delete` | Session | Optional installed-skill delete |
| `model_presets` | Session | Optional model preset surface |
| `model_switcher` | Session | Optional per-chat model override surface |
| `compact_chat` | Session | Optional chat compaction surface |
| `token_status` | Session | Optional token usage surface |

## WebSocket events

All events are `connector_`-prefixed to avoid collisions on the shared `/ws` namespace.

### Client -> Server

| Event | Purpose |
|-------|---------|
| `connector_hello` | Handshake: returns protocol version, features, and `exec_config` |
| `connector_subscribe_context` | Subscribe to a context event stream |
| `connector_unsubscribe_context` | Unsubscribe from a context |
| `connector_send_message` | Send user message asynchronously |
| `connector_file_op_result` | Return result of a local file operation |
| `connector_remote_tree_update` | Publish frontend workspace tree snapshots |
| `connector_exec_op_result` | Return result of a shell-backed frontend execution operation |

### Server -> Client

| Event | Purpose |
|-------|---------|
| `connector_context_snapshot` | Batch of historical events on subscribe |
| `connector_context_event` | Single streamed event from a running agent |
| `connector_context_complete` | Agent finished responding |
| `connector_error` | Application-level error for a context |
| `connector_file_op` | Request a local file operation |
| `connector_exec_op` | Request a shell-backed frontend execution operation |

## Event bridge

`helpers/event_bridge.py` translates Agent Zero log entry types into normalized connector events:

| Agent Zero log type | Connector event |
|---------------------|-----------------|
| `user`, `input` | `user_message` |
| `response`, `ai_response` | `assistant_message` |
| `tool`, `mcp` | `tool_start` |
| `tool_output`, `browser` | `tool_output` |
| `code` | `code_start` |
| `code_exe`, `code_output` | `code_output` |
| `error` | `error` |
| `warning` | `warning` |
| `agent`, `hint`, `progress`, `subagent`, `util` | `status` |
| `info` | `info` |

## Remote file operations

The `text_editor_remote` tool emits `connector_file_op` to the subscribed CLI client. The CLI performs the file read, write, or patch on the local machine and returns `connector_file_op_result`.

Supported public tool operations:
- `read`
- `write`
- `patch`

Internal transport-only operation:
- `stat` - used by the plugin to fetch canonical CLI-side file metadata before a freshness-checked patch. This is not exposed as a public `text_editor_remote` tool method.

Successful `read`, `write`, and `patch` results now include:

```json
{
  "file": {
    "realpath": "C:/absolute/canonical/path.py",
    "mtime": 1713182400.123,
    "total_lines": 42
  }
}
```

`read` still returns the same numbered text content, and `write` / `patch` still return the same success message strings as before; the metadata block is additive.

Freshness semantics for `patch`:
- The plugin stores per-agent remote file state in `agent.data`, keyed by the CLI-reported `realpath`.
- A prior successful `read` or `write` is required before `patch`.
- `patch` first issues an internal `stat` and compares the current CLI `mtime` against the stored state.
- No stored state produces the same prompt behavior as `_text_editor` `patch_need_read`.
- A changed `mtime` produces the same prompt behavior as `_text_editor` `patch_stale_read`.
- Line-preserving in-place patches refresh the stored metadata and may be chained without rereading.
- Insertions, deletions, or any line-count-changing patch deliberately mark the file state stale so the next patch requires a reread.
- If the connected CLI does not support internal `stat`, the plugin returns a single explicit compatibility error (`unsupported_cli_freshness`) and does not fall back to blind patching.

Structured freshness failure codes may be returned in remote patch flows:
- `patch_need_read`
- `patch_stale_read`
- `unsupported_cli_freshness`

`ws_runtime.py` manages SID-to-context subscriptions, pending file futures, pending execution futures, and the latest remote tree snapshot per SID.

## Remote execution operations

The `code_execution_remote` tool emits `connector_exec_op` to the subscribed CLI client, which runs a persistent local shell session and returns `connector_exec_op_result`.

Supported runtimes:
- `terminal`
- `python`
- `nodejs`
- `output`
- `reset`
- `input`

`connector_hello` includes `exec_config` with:
- `code_exec_timeouts`
- `output_timeouts`
- `prompt_patterns`
- `dialog_patterns`

The backend owns that execution policy. The CLI owns the local shell session and all platform-specific TTY behavior.

## Security

- Public discovery stays unauthenticated.
- Protected connector HTTP handlers use Agent Zero's existing web session check: `requires_auth=True`, `requires_csrf=False`, `requires_api_key=False`.
- The connector `/ws` handler uses the same session policy.
- Connector access is independent from MCP enablement. `mcp_server_enabled` does not affect CLI access.
