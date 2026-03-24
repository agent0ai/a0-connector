# A0 Connector Plugin Plan

## Summary

Build a dedicated **`a0-connector` plugin** that exposes a clean, CLI-friendly integration surface for Agent Zero over **HTTP + WebSocket**, using **API-key style authentication** instead of the frontend's **session + CSRF** contract.

This plugin should become the stable backend surface for:

- the `a0-connector` CLI
- future terminal/mobile/bot integrations
- external non-browser clients that need chat control and streaming

The plan is **local-first** for implementation and testing, with packaging that can later be published as a community plugin if desired.

---

## Why we need this

The current CLI connector works, but it is coupled to the same backend surfaces as the WebUI:

- `GET /csrf_token`
- `POST /login`
- `POST /chat_create`
- `POST /message_async`
- Socket.IO namespace `/state_sync`
- frontend-style session cookies
- runtime-scoped CSRF cookie injection
- manual `Origin` / `Referer` / cookie handling for websocket connect

That makes the CLI act like a second frontend, instead of a clean external connector.

### Problems with the current approach

1. **Browser-specific auth assumptions**
   - session cookies
   - CSRF token fetches
   - runtime-scoped CSRF cookies
   - redirect-to-login behavior

2. **Frontend protocol leakage**
   - CLI depends on web UI state sync semantics and snapshot shapes
   - websocket payloads are optimized for the browser, not connector clients

3. **Hard to extend safely**
   - every non-browser client must imitate frontend security behavior
   - auth rules are unclear for external consumers

4. **No clean streaming contract for integrators**
   - streaming currently means subscribing to frontend `state_push`
   - clients must understand UI-oriented snapshot deltas rather than connector-oriented events

---

## What we learned from the current A0 architecture

### Plugin system

From the plugin skill and plugin docs:

- plugins live in **`/a0/usr/plugins/<plugin_name>/`**
- every plugin must include **`plugin.yaml`**
- plugins can provide **API handlers** via `api/`
- plugin API routes are available under:
  - **`POST/GET /api/plugins/<plugin_name>/<handler>`**
- plugin settings can be scoped and stored via the existing plugin config system

### HTTP API support is already a good fit

`python/helpers/api.py` already supports per-handler security declarations:

- `requires_api_key()`
- `requires_auth()`
- `requires_csrf()`
- `requires_loopback()`

This is important because it means the new connector plugin can expose **HTTP endpoints that require API keys and do not require session auth or CSRF**.

### WebSocket support is close, but not fully ready

There is already a structured websocket system with:

- `WebSocketHandler`
- `WebSocketManager`
- namespace discovery tests
- `discover_websocket_namespaces(...)`
- `configure_websocket_namespaces(...)`

However, based on the inspected code and tests:

- websocket handlers clearly support `requires_auth()` and `requires_csrf()`
- websocket discovery exists
- **API-key auth is not currently a first-class websocket security mode**
- plugin websocket layout is not yet documented as a stable plugin authoring contract

### Existing external API

A0 already has a useful external HTTP API:

- `POST /api_message`
- `GET/POST /api_log_get`
- `POST /api_reset_chat`
- `POST /api_terminate_chat`

This proves there is already a valid **API-key-based external integration path**.

But it still does not provide the clean websocket streaming surface needed by the CLI connector.

---

## Goals

### Primary goals

1. Create a dedicated **connector plugin backend surface** for non-browser clients.
2. Remove CLI dependence on frontend CSRF/session handling.
3. Expose **stable HTTP endpoints** for chat lifecycle and message sending.
4. Expose **stable websocket streaming** for real-time updates.
5. Keep the connector protocol **narrower and simpler** than the web UI protocol.
6. Reuse as much existing Agent Zero core behavior as possible.

### Secondary goals

1. Make the protocol versioned from day one.
2. Make it reusable beyond the CLI.
3. Keep the MVP compatible with current A0 concepts:
   - contexts
   - projects
   - agent profiles
   - attachments
   - log-based rendering

### Non-goals

1. Replacing the WebUI protocol.
2. Rebuilding all frontend state sync semantics.
3. Designing a generic public SDK in this phase.
4. Fully deprecating existing `/api_message` immediately.

---

## Target architecture

## 1) Plugin package

Planned runtime location:

- **`/a0/usr/plugins/a0-connector/`**

Suggested structure:

```text
/a0/usr/plugins/a0-connector/
├── plugin.yaml
├── default_config.yaml
├── README.md
├── api/
│   └── v1/
│       ├── capabilities.py
│       ├── chats_list.py
│       ├── chat_create.py
│       ├── chat_get.py
│       ├── chat_delete.py
│       ├── chat_reset.py
│       ├── message_send.py
│       ├── log_tail.py
│       └── projects_list.py
├── helpers/
│   ├── auth.py
│   ├── context_service.py
│   ├── attachments.py
│   ├── event_mapping.py
│   └── stream_bridge.py
├── websocket/
│   └── connector/
│       ├── main.py
│       └── subscription_state.py
└── webui/
    └── config.html
```

### `plugin.yaml`

Suggested initial manifest:

```yaml
title: A0 Connector
description: Clean HTTP and WebSocket connector surface for CLI and external clients.
version: 0.1.0
settings_sections:
  - external
  - developer
per_project_config: false
per_agent_config: false
```

Notes:

- `external` is appropriate because this is an integration surface.
- `developer` is helpful if we want protocol toggles or diagnostics.
- local-first is recommended initially, even if we later publish it.

---

## 2) Auth model

## Recommendation

Use **API-key-based auth** for both HTTP and websocket.

### Initial token strategy

Use the existing A0 external token model first:

- accept the same token used by `/api_message`
- source of truth: current external API token / `mcp_server_token`

### Optional later enhancement

Add plugin-specific token support:

- plugin config can override the default token source
- if unset, fallback to the existing global token

This gives us:

- zero-friction initial rollout
- compatibility with existing integrations
- clean future separation if connector-specific secrets are desired

### Why this is better than session + CSRF

Because the CLI and external clients are not browsers.
They should not need to:

- fetch CSRF tokens
- follow login redirects
- persist frontend cookies
- construct runtime-scoped CSRF cookie names

---

## 3) HTTP API surface

All endpoints should be versioned under the plugin route prefix.

### Base path

- **`/api/plugins/a0-connector/v1/...`**

This fits the existing plugin API dispatcher and keeps the contract isolated.

## Proposed endpoints

### `GET /api/plugins/a0-connector/v1/capabilities`

Returns:

- protocol version
- supported auth modes
- supported transport modes
- attachment limits
- available optional features

Example response:

```json
{
  "protocol": "a0-connector.v1",
  "auth": ["api_key"],
  "transports": ["http", "websocket"],
  "streaming": true,
  "attachments": {
    "mode": "base64",
    "max_files": 20
  }
}
```

### `GET /api/plugins/a0-connector/v1/chats_list`

Returns available contexts in connector-friendly form.

Fields:

- `id`
- `name`
- `created_at`
- `updated_at`
- `last_message_preview`
- `project_name`
- `agent_profile`

### `POST /api/plugins/a0-connector/v1/chat_create`

Creates a new chat/context.

Request fields:

- `project_name` optional
- `agent_profile` optional
- `name` optional
- `metadata` optional

Response:

- `context_id`
- resolved project/profile
- created timestamps

### `GET /api/plugins/a0-connector/v1/chat_get`

Input:

- `context_id`

Returns:

- context metadata
- current state summary
- latest log cursor / sequence info

### `POST /api/plugins/a0-connector/v1/message_send`

This becomes the main HTTP message entrypoint for connector clients.

Request fields:

- `context_id` optional
- `message` required
- `attachments` optional
- `project_name` optional on first message only
- `agent_profile` optional on first message only
- `stream` optional boolean
- `client_message_id` optional

Behavior:

- if `context_id` absent, create a new context
- if `stream=false`, wait for final response and return it
- if `stream=true`, queue the task and return an accepted response for websocket consumption

Possible response shapes:

### non-streaming

```json
{
  "context_id": "ctx-123",
  "message_id": "msg-456",
  "status": "completed",
  "response": "..."
}
```

### streaming handoff

```json
{
  "context_id": "ctx-123",
  "message_id": "msg-456",
  "status": "accepted",
  "stream_namespace": "/connector",
  "stream_channel": "ctx-123"
}
```

### `POST /api/plugins/a0-connector/v1/chat_reset`

Request:

- `context_id`

Resets the chat safely.

### `POST /api/plugins/a0-connector/v1/chat_delete`

Request:

- `context_id`

Deletes/removes a context.

### `GET /api/plugins/a0-connector/v1/log_tail`

Fallback polling endpoint for non-websocket clients.

Request:

- `context_id`
- `after` optional cursor
- `limit` optional

Returns normalized connector events, not frontend snapshots.

### `GET /api/plugins/a0-connector/v1/projects_list`

Useful for future CLI flows where the user selects project context.

---

## 4) WebSocket streaming surface

## Recommendation

Expose a dedicated connector namespace, separate from frontend `/state_sync`.

### Namespace

- **`/connector`**

Alternative acceptable naming:

- `/connector_v1`

Preferred approach:

- keep namespace stable as `/connector`
- include protocol version in handshake/capabilities payload

## Why not reuse `/state_sync`

Because `/state_sync` is optimized for frontend synchronization, not external connector clients.

It carries browser-era assumptions:

- session/csrf handshake behavior
- UI snapshot semantics
- state projection logic specific to the WebUI

The connector websocket should instead expose **connector events**.

## Client -> server events

### `hello`

Used immediately after connect.

Payload:

```json
{
  "protocol": "a0-connector.v1",
  "client": "agent-zero-cli",
  "client_version": "0.1.0"
}
```

### `subscribe_context`

```json
{
  "context_id": "ctx-123",
  "from": 0
}
```

### `unsubscribe_context`

```json
{
  "context_id": "ctx-123"
}
```

### `send_message`

```json
{
  "context_id": "ctx-123",
  "message": "Hello",
  "attachments": [],
  "client_message_id": "local-001"
}
```

### `ping`

Simple keepalive / latency probe.

## Server -> client events

### `hello_ok`

Capabilities and negotiated protocol.

### `context_snapshot`

Initial normalized snapshot for a subscribed context.

### `message_accepted`

Acknowledge send request.

### `context_event`

Normalized event stream item.

Proposed shape:

```json
{
  "context_id": "ctx-123",
  "sequence": 42,
  "event": "assistant_delta",
  "timestamp": "2026-03-12T07:00:00Z",
  "data": {
    "text": "partial output"
  }
}
```

### `context_complete`

Marks agent completion for a specific message/task.

### `error`

Structured error event.

---

## 5) Event model

The connector websocket should not expose raw frontend snapshots.

Instead, create a normalized connector event model derived from A0 logs and state.

## Recommended event types

- `user_message`
- `assistant_delta`
- `assistant_message`
- `tool_start`
- `tool_output`
- `tool_end`
- `code_start`
- `code_output`
- `warning`
- `error`
- `status`
- `message_complete`
- `context_updated`

## Important design rule

The plugin should translate internal log entries into connector events.

That gives us:

- a stable external protocol
- less leakage of frontend state internals
- freedom to refactor the WebUI later without breaking the CLI

---

## 6) What can be implemented purely as a plugin vs what needs core changes

## Can be implemented as a plugin today

### HTTP connector endpoints

Yes.

Reason:

- plugin API handlers already exist
- `ApiHandler` already supports `requires_api_key()`
- plugin routes already exist under `/api/plugins/<name>/<handler>`

This means the HTTP side of the connector plugin can be built with little or no core refactoring.

## Likely needs minimal core changes

### A. WebSocket API-key auth

Current websocket handlers expose auth/csrf concepts, but not a first-class API-key security mode comparable to `ApiHandler.requires_api_key()`.

### Needed change

Add websocket auth support parallel to HTTP API handlers:

- `WebSocketHandler.requires_api_key()`
- handshake validator that accepts token from one of:
  - `auth.api_key`
  - `Authorization: Bearer ...`
  - `X-API-KEY`
- connector namespace should validate API key without session or CSRF

### B. Formal plugin websocket contribution path

Namespace discovery clearly exists in the codebase and tests, but plugin docs do not yet define a stable plugin layout for websocket namespaces.

### Needed change

Formalize plugin websocket loading, for example:

```text
/a0/usr/plugins/<plugin_name>/websocket/<namespace>/...
```

or

```text
/a0/usr/plugins/<plugin_name>/extensions/python/websocket/<namespace>/...
```

and have `run_ui.py` load websocket namespaces from plugin roots in addition to core handlers.

### C. Reusable context/event streaming bridge

The connector should not copy the frontend `state_sync` logic.

### Needed change

Add a small reusable backend helper that can:

- observe context log growth / progress changes
- translate them into ordered connector events
- deliver those events to plugin websocket handlers

This helper should live in core, because multiple future integrations may need it.

Suggested helper name:

- `python/helpers/context_event_stream.py`

---

## 7) Proposed minimal core changes

These are the smallest core changes that unlock a proper connector plugin.

## Change 1: add API-key auth mode to `WebSocketHandler`

Add methods and handshake enforcement similar to HTTP handlers:

- `requires_api_key()` default `False`
- if `True`, validate API key during namespace connect
- if API-key mode is active:
  - do **not** require session auth
  - do **not** require CSRF

### Acceptance criteria

- websocket handlers can opt into API-key auth
- connector namespace works without cookies or CSRF token fetches
- existing frontend namespaces continue working unchanged

## Change 2: formalize plugin websocket discovery

Update docs and loading logic so plugins can contribute websocket namespaces intentionally.

### Acceptance criteria

- plugin websocket namespace directory layout is documented
- plugin namespaces are discovered at startup
- plugin namespaces are registered beside core namespaces
- namespace collisions are rejected clearly

## Change 3: add reusable context streaming helper

Provide a small core helper that converts live context/log updates into transport-neutral events.

### Acceptance criteria

- helper can subscribe to a context
- helper can emit ordered events with sequence numbers
- helper is not tied to WebUI snapshot schema
- plugin websocket handler can consume it directly

## Change 4: optional helper for token resolution

To avoid duplicated auth logic between connector plugin and future integrations, add a small shared helper for external token resolution.

Potential behavior:

- read plugin override token if configured
- otherwise fallback to core external token

---

## 8) Connector plugin implementation plan

## Phase 1 — Spec + contracts

Deliverables:

- this plan
- versioned JSON schemas for HTTP requests/responses
- websocket event schema draft

Output:

- clear boundary between frontend protocol and connector protocol

## Phase 2 — HTTP MVP in plugin

Implement plugin endpoints first because they are already well-supported.

### Work

1. Create plugin skeleton in `/a0/usr/plugins/a0-connector/`
2. Add `plugin.yaml`
3. Add API handlers under `api/v1/`
4. Implement:
   - `capabilities`
   - `chats_list`
   - `chat_create`
   - `message_send`
   - `chat_reset`
   - `chat_delete`
   - `log_tail`
5. Use `requires_api_key=True`, `requires_auth=False`, `requires_csrf=False`
6. Reuse `AgentContext`, `initialize_agent`, and project activation helpers internally

### Result

CLI can stop depending on:

- `/csrf_token`
- `/login`
- `/chat_create`
- `/message_async`

for its core HTTP actions.

## Phase 3 — Core websocket extensions

Implement the minimal core changes listed above.

### Work

1. add websocket API-key auth mode
2. formalize plugin websocket discovery path
3. add reusable context event streaming helper

### Result

Plugin websocket can stream connector-native events.

## Phase 4 — Plugin websocket implementation

Implement `/connector` namespace inside the plugin.

### Work

1. handshake with API key
2. `hello` negotiation
3. context subscription state
4. message send via websocket
5. live event emission
6. completion/error events

### Result

CLI can use connector-native websocket streaming rather than `/state_sync`.

## Phase 5 — CLI migration

Update `a0-connector` repo client to use the plugin surface.

### New behavior

HTTP:

- `/api/plugins/a0-connector/v1/...`

WebSocket:

- `/connector`

### Remove old behavior

- no login page flow for connector mode
- no CSRF fetch
- no manual cookie header construction
- no `/state_sync` dependency
- no frontend `state_push` parsing

## Phase 6 — Deprecation and compatibility layer

After the CLI is stable on the plugin surface:

- keep existing frontend paths working for the browser
- optionally keep legacy connector mode behind a feature flag for one release
- document plugin surface as the recommended integration API for streaming clients

---

## 9) Migration strategy for the existing CLI repo

## Short term

Add a transport abstraction in the CLI:

- `FrontendCompatTransport` = current `/message_async` + `/state_sync` implementation
- `ConnectorPluginTransport` = new plugin HTTP + websocket implementation

Default to:

- plugin transport when connector plugin is available
- fallback to legacy frontend transport only if explicitly requested or plugin is absent

## Medium term

Make plugin transport the default and mark legacy transport as deprecated.

## Long term

Remove frontend-compat mode from the CLI once the plugin is stable.

---

## 10) Testing plan

## Plugin HTTP tests

Add tests for:

- API key required
- no session required
- no CSRF required
- create context
- resume context
- project assignment only on first message
- agent profile validation
- attachments handling
- log tail cursor behavior

## Plugin websocket tests

Add tests for:

- connect with valid API key
- reject missing/invalid API key
- subscribe/unsubscribe context
- send message and receive normalized event stream
- context completion event
- multiple clients on same context
- namespace isolation from frontend `/state_sync`

## Regression tests

Keep existing frontend namespace tests unchanged.

Important regression requirement:

- the connector plugin must not weaken current browser security for existing namespaces

---

## 11) Proposed acceptance criteria

The work is done when all of the following are true:

1. A plugin can expose connector HTTP endpoints under `/api/plugins/a0-connector/v1/...`.
2. Those endpoints authenticate using API keys only.
3. The CLI can send messages without using `/csrf_token`, `/login`, `/message_async`, or `/state_sync`.
4. A connector websocket namespace exists and authenticates without CSRF/session coupling.
5. The websocket emits connector-native events, not frontend snapshots.
6. The CLI can create chats, switch chats, send messages, and stream updates through the plugin surface.
7. Existing browser functionality remains unchanged.

---

## 12) Recommended implementation order

1. **Write schemas and endpoint contracts**
2. **Build plugin HTTP MVP**
3. **Refactor CLI to support transport abstraction**
4. **Add websocket API-key auth support in core**
5. **Formalize plugin websocket discovery**
6. **Implement plugin websocket namespace**
7. **Migrate CLI streaming**
8. **Document and deprecate legacy connector path**

---

## 13) Biggest risks

### Risk 1: duplicating too much existing logic

Mitigation:

- reuse `AgentContext` and existing external API patterns
- keep connector protocol narrow
- add a reusable core streaming helper instead of copying frontend sync logic

### Risk 2: accidentally broadening websocket attack surface

Mitigation:

- namespace-specific auth
- explicit API-key-only handshake path
- no fallback to unauthenticated behavior
- keep existing frontend namespace validation intact

### Risk 3: protocol churn

Mitigation:

- version everything under `v1`
- define stable event schemas before implementation
- add contract tests early

### Risk 4: token confusion between external services

Mitigation:

- start with existing external token for simplicity
- document it clearly
- add plugin-specific token override later if needed

---

## 14) Final recommendation

Proceed with a **plugin-first connector architecture**:

- use the existing plugin API system immediately for the HTTP surface
- make **small, targeted core changes** to support plugin websocket streaming with API-key auth
- migrate the CLI to the plugin transport behind a transport abstraction
- keep frontend and connector protocols separate

This is the cleanest path to a proper A0 connector because it:

- removes browser-specific CSRF/session coupling
- preserves current WebUI behavior
- gives the CLI a stable backend contract
- creates a reusable integration surface for future external clients
