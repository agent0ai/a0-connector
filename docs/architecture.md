# Architecture

## Components

```
┌─────────────────┐     HTTP POST + X-API-KEY     ┌────────────────────────────┐
│  agentzero CLI  │ ────────────────────────────► │  Agent Zero                │
│                 │                               │  + a0_connector plugin     │
│                 │     Socket.IO /ws namespace   │                            │
│                 │ ◄──────────────────────────── │                            │
└─────────────────┘     connector_* events        └────────────────────────────┘
```

- **CLI** (`agent-zero-cli`): Textual TUI, installed via `pip install -e .`
- **Plugin** (`a0_connector`): source in `plugin/a0_connector/`, deployed into Agent Zero `usr/plugins/a0_connector`

## Startup flow

1. **Discover** — `POST /api/plugins/a0_connector/v1/capabilities` (public, no key)
2. **Authenticate** — resolve API key from env, dotenv, or `connector_login`
3. **Verify** — test key against `chats_list`
4. **Connect** — Socket.IO to `/ws` with `auth: {api_key, handlers: ["plugins/a0_connector/ws_connector"]}`
5. **Chat** — create context, subscribe, stream events

## Protocol

**Version:** `a0-connector.v1` — advertised by `capabilities`, validated by the CLI.

**Transport:** Engine.IO at `/socket.io`, Socket.IO namespace `/ws`.

**Auth:** `auth.api_key` (= `mcp_server_token`) + `auth.handlers` to activate the connector on the shared namespace.

## HTTP routes

All routes: `POST /api/plugins/a0_connector/v1/<endpoint>`

| Endpoint | Auth | Purpose |
|----------|------|---------|
| `capabilities` | Public | Discovery — protocol, features, auth modes |
| `connector_login` | Public | Exchange username/password for API key |
| `chat_create` | API key | Create a new chat context |
| `chats_list` | API key | List existing contexts |
| `chat_get` | API key | Get a single context |
| `chat_reset` | API key | Reset a context |
| `chat_delete` | API key | Delete a context |
| `message_send` | API key | Send a message (with optional base64 attachments) |
| `log_tail` | API key | Paginated context log entries |
| `projects_list` | API key | List available projects |

## WebSocket events

All events are `connector_`-prefixed to avoid collisions on the shared `/ws` namespace.

### Client → Server

| Event | Purpose |
|-------|---------|
| `connector_hello` | Handshake — returns protocol version + features |
| `connector_subscribe_context` | Subscribe to a context's event stream |
| `connector_unsubscribe_context` | Unsubscribe from a context |
| `connector_send_message` | Send user message (async; returns `accepted`) |
| `connector_file_op_result` | Return result of a local file operation |
| `connector_remote_tree_update` | Publish frontend workspace tree snapshots |
| `connector_exec_op_result` | Return result of a frontend Python TTY execution operation |

### Server → Client

| Event | Purpose |
|-------|---------|
| `connector_context_snapshot` | Batch of historical events on subscribe |
| `connector_context_event` | Single streamed event from a running agent |
| `connector_context_complete` | Agent finished responding |
| `connector_error` | Application-level error for a context |
| `connector_file_op` | Request a local file operation (read/write/patch) |
| `connector_exec_op` | Request a frontend Python TTY execution operation |

### Event bridge

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
| `agent`, `hint`, `info`, `progress`, `subagent`, `util` | `status` |

## Remote file operations

The `text_editor_remote` tool (agent-side) emits `connector_file_op` to the subscribed CLI client. The CLI performs the file read/write/patch on the **local machine** and returns `connector_file_op_result`. Operations are correlated by `op_id`.

`ws_runtime.py` manages the in-memory state: SID-to-context subscriptions, pending file operation futures, pending execution futures, and latest remote tree snapshots per SID, all thread-safe via `threading.RLock`.

## Remote execution operations

The `code_execution_remote` tool emits `connector_exec_op` to the subscribed CLI client, which runs a Python TTY session on the frontend machine and returns `connector_exec_op_result`.

Supported runtimes:
- `python` (execute code)
- `input` (send follow-up stdin)
- `output` (poll more output for long-running jobs)
- `reset` (discard a session)

Each operation is correlated by `op_id` and scoped to a numeric `session` id.

## Prompt context injection

The frontend publishes a bounded remote workspace tree via `connector_remote_tree_update` (immediate on connect + periodic changed-only updates).  
During prompt build, extension `extensions/python/message_loop_prompts_after/_76_include_remote_file_structure.py` injects a `remote_file_structure` prompt block into `loop_data.extras_temporary` when a snapshot is fresh (<= 90 seconds old) for the active context subscription.

## Security

- **API key** = Agent Zero's `mcp_server_token`. Gates all protected HTTP routes and validates `auth.api_key` on WebSocket connect.
- **Login** is optional: `connector_login` compares against `AUTH_LOGIN`/`AUTH_PASSWORD` from Agent Zero's `.env`. If no UI auth is configured, returns `mcp_server_token` directly.
