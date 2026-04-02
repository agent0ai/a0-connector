# Architecture

The a0-connector project has two parts that ship in one repository:

1. **CLI** (`agent-zero-cli`) — a Textual terminal app that talks to Agent Zero over HTTP and Socket.IO.
2. **Plugin** (`plugin/a0_connector`) — Python code that runs *inside* the Agent Zero process. It exposes HTTP routes and a WebSocket handler on the shared `/ws` namespace.

Agent Zero loads the plugin from `usr/plugins/a0_connector` in its checkout. The CLI is installed separately (`pip install -e .` from this repo).

## High-level flow

```text
┌─────────────────┐     HTTP (REST)      ┌──────────────────────────────┐
│  agentzero CLI  │ ──────────────────► │ Agent Zero + a0_connector   │
│  (this repo)    │     X-API-KEY       │ plugin                       │
└────────┬────────┘                     └──────────────┬───────────────┘
         │                                             │
         │     Socket.IO /ws + auth.handlers           │
         └────────────────────────────────────────────►│
                                                       │
         ◄──────── connector_* events ───────────────┘
```

1. The CLI discovers the server via **POST** `/api/plugins/a0_connector/v1/capabilities` (public, no key).
2. Protected calls use **POST** to the same base path with header **`X-API-KEY`** set to Agent Zero’s **`mcp_server_token`** (or a value obtained via **`connector_login`**).
3. Streaming and interactive behavior use **Socket.IO** on namespace **`/ws`**. The handshake sends **`auth`** including **`api_key`** and **`handlers`: `["plugins/a0_connector/ws_connector"]`** so Agent Zero activates the connector handler on that connection.

## Protocol version

Capabilities advertise **`protocol`: `a0-connector.v1`**. The CLI validates protocol, namespace (`/ws`), handler id, and advertised auth modes before continuing.

## HTTP surface

| Area | Role |
|------|------|
| **Public** | `capabilities` — discovery; may also include **`connector_login`** for credential exchange |
| **Protected** | Chat CRUD, message send, log tail, projects list, etc. — all require API key only (no session cookies, no CSRF for these handlers) |

Plugin handlers live under `plugin/a0_connector/api/v1/`. Routes are registered by Agent Zero’s plugin API mechanism at **`/api/plugins/a0_connector/v1/<endpoint>`**.

## WebSocket surface

Events are **connector-prefixed** so multiple handlers can share `/ws`:

- `connector_hello`, `connector_subscribe_context`, `connector_unsubscribe_context`, `connector_send_message`
- `connector_context_snapshot`, `connector_context_event`, `connector_context_complete`
- `connector_file_op` / `connector_file_op_result` (paired by `op_id`)
- `connector_error`

The plugin maps Agent Zero runtime state (e.g. context logs) into these events; see `helpers/event_bridge.py` and `api/ws_connector.py` in the plugin tree.

## Remote file operations

The **`text_editor_remote`** tool runs on the agent side. When it needs a local file, it emits **`connector_file_op`** to the subscribed CLI client. The CLI performs read/write/patch on the **local machine** and returns **`connector_file_op_result`**. There is no separate server→client request channel beyond this event pair and `op_id` correlation.

## Security model (summary)

- **API key** (`mcp_server_token`) gates protected HTTP and validates the WebSocket `auth.api_key` path.
- **Login** is optional at the HTTP layer: the **`connector_login`** endpoint can return the same token after validating **`AUTH_LOGIN` / `AUTH_PASSWORD`** (same as the web UI), or the token when no UI auth is configured—see [Configuration](configuration.md).

For Agent Zero core behavior (handler activation, bad key edge cases), refer to the Agent Zero `helpers/ws.py` documentation in that repository.
