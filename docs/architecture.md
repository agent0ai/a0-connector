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

- CLI (`a0`): Textual TUI plus headless stdin/stdout frontend, published as the `a0` package and installed as the `a0` command.
- Plugin (`_a0_connector`): builtin Agent Zero Core plugin.

## Startup flow

Both the Textual TUI and `a0 headless` use the same connector protocol. The
TUI still owns its historical connection orchestration; headless uses the
UI-neutral `ConnectorSession` core for transport, context subscription, remote
file operations, remote exec operations, and workspace-tree publishing.

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
| `capabilities` | Public | Discovery: protocol, Agent Zero version, features, session contract, `auth_required` |
| `chat_create` | Session | Create a new chat context |
| `chats_list` | Session | List existing contexts |
| `chat_get` | Session | Get a single context |
| `chat_reset` | Session | Reset a context |
| `chat_delete` | Session | Delete a context |
| `pause` | Session | Pause the currently running context |
| `nudge` | Session | Continue a stopped or paused context run |
| `message_send` | Session | Send a message with optional path/URL attachments; HTTP base64 uploads are saved to files before agent history |
| `log_tail` | Session | Paginated context log entries |
| `projects` | Session | Project list, activate, deactivate, load, update |
| `settings_get` | Session | Optional runtime settings surface |
| `settings_set` | Session | Optional runtime settings surface |
| `agent_profile_set` | Session | Context-scoped Agent Zero Core profile switch |
| `agent_editor` | Session | Context-scoped profile load/create/edit, sparse save, and Tool/MCP/Skill policy updates |
| `agents_list` | Session | Optional agent-profile list |
| `skills_list` | Session | Optional installed-skill list |
| `skills_activate` | Session | Optional context-scoped skill activation |
| `skills_delete` | Session | Optional installed-skill delete |
| `installed_plugins` | Session | Optional installed-only plugin list and enable/disable toggle; no marketplace install/delete path |
| `model_presets` | Session | Optional model preset surface |
| `model_switcher` | Session | Optional per-chat model override surface |
| `compact_chat` | Session | Optional chat compaction surface |
| `token_status` | Session | Optional token usage surface |

## WebSocket events

All events are `connector_`-prefixed to avoid collisions on the shared `/ws` namespace.

### Client -> Server

| Event | Purpose |
|-------|---------|
| `connector_hello` | Handshake/metadata refresh: returns protocol version, Agent Zero version, features, `exec_config`, and remote-tool state |
| `connector_subscribe_context` | Subscribe to a context event stream |
| `connector_unsubscribe_context` | Unsubscribe from a context |
| `connector_send_message` | Send user message asynchronously |
| `connector_file_op_result` | Return result of a local file operation; large results may be sent as JSON/base64 chunks |
| `connector_remote_tree_update` | Publish frontend workspace tree snapshots |
| `connector_exec_op_result` | Return result of a shell-backed frontend execution operation |
| `connector_computer_use_op_result` | Return result of a frontend computer-use operation |
| `connector_browser_op_result` | Return result of a host-browser operation |

### Server -> Client

| Event | Purpose |
|-------|---------|
| `connector_context_snapshot` | Batch of historical events on subscribe |
| `connector_context_event` | Single streamed event from a running agent |
| `connector_context_complete` | Agent finished responding |
| `connector_settings_updated` | Optional canonical settings snapshot for CLI rehydration |
| `connector_error` | Application-level error for a context |
| `connector_file_op` | Request a local file operation |
| `connector_exec_op` | Request a shell-backed frontend execution operation |
| `connector_computer_use_op` | Request a frontend computer-use operation |
| `connector_browser_op` | Request a host-browser operation |

`connector_hello` is also the canonical permission metadata refresh. The CLI
sends current `remote_files`, `remote_exec`, `computer_use`, and `host_browser`
metadata on connect and whenever gated permissions change. When a chat is
active, the payload includes `context_id`; the backend re-associates that SID
with the context before the next prompt is built so gated stubs such as
`code_execution_remote` and host-backed Browser routing are exposed for the
correct chat.

Both public `capabilities` and `connector_hello` include `agent_zero_version`.
The CLI compares that value with its own package version and surfaces a warning
when the connected Agent Zero Core is newer than the installed CLI.

The interactive TUI subscribes with `history: "tail"`, which returns the newest
100 log-output entries and a `history_before` cursor. When the user reaches the
top of the visible transcript, it requests the preceding page with that cursor;
the snapshot includes `has_more_history` until the beginning of the chat. The
headless frontend omits the hint and retains its complete replay behavior.

## Headless frontend

`a0 headless` is a plain stdin/stdout connector frontend. It does not import
Textual, but it still registers as the host-side connector client for the
subscribed chat.

Headless `connector_hello` metadata advertises remote files and remote exec as
available, scoped to the selected local workspace. It advertises computer-use
and host-browser support as unavailable; if the backend sends those operations
anyway, the client returns structured unsupported operation results instead of
leaving server-side futures pending.

Text output is append-only and pipe-safe. JSONL output emits connector events
and synthetic lifecycle records (`ready`, `complete`, `notice`, `error`) as one
JSON object per stdout line.

## Interactive transcript images

Only the interactive TUI renders images. The CLI extracts eligible references
from Browser tool output, user attachment metadata, and assistant metadata or
Markdown. A Browser `Screenshot: img://<path>&t=...` or `browser_snapshot`
belongs under the existing Browser tool metadata. User attachments belong under
the user message; assistant metadata and Markdown images belong under the
assistant message. The extractor is pure and preserves the connector event
schema, so a screenshot never becomes a duplicate sequence entry.

For an Agent Zero path, the client uses its authenticated session to `GET`
same-origin `/api/image_get`; arbitrary external URLs and filesystem paths are
not accepted. Raster PNG, JPEG, GIF, WebP, and BMP are supported (GIF uses the
first frame). SVG is represented by a stable placeholder. Sources are limited
to 25 MiB encoded input and 32 megapixels decoded; the store runs up to four
fetch/load tasks at once, admits only one full-resolution decoder at a time,
and downsamples before applying orientation and color conversion. It retains a
memory-only 64 MiB LRU of independent display surfaces. Invalid,
unauthenticated, unavailable, oversized, or unsupported images become stable
placeholders, and copied transcript text contains semantic image labels rather
than bytes or cache paths.

Browser screenshot materialization already exists in Agent Zero Core: browser
history metadata carries `Screenshot: img://<path>&t=...` plus
`browser_snapshot`. The separate Core deployment boundary is the builtin
`_a0_connector` WebSocket user-message handler correction that records sanitized
uploaded filenames on the user log, matching the HTTP message path so live and
replayed user attachments retain resolvable metadata. Updating this CLI does not
deploy that Core correction.

## Host browser operations

Host browser mode keeps the public agent API as Agent Zero's existing
`browser` tool. The Browser plugin decides whether a call uses the container
Patchright runtime or emits `connector_browser_op` to the subscribed CLI:

```json
{
  "op_id": "uuid",
  "context_id": "ctx-...",
  "action": "open",
  "url": "https://example.com"
}
```

The CLI returns:

```json
{
  "op_id": "uuid",
  "ok": true,
  "result": {
    "id": 1,
    "state": {
      "id": 1,
      "runtime": "host",
      "currentUrl": "https://example.com/"
    }
  }
}
```

Screenshots are transferred as artifact payloads rather than inline tool
output. The CLI sends base64 bytes in `result.artifact`; explicit `path`
requests remain user-owned host artifacts. The verified Core connector runtime
materializes the default host artifact and records browser history metadata as
`Screenshot: img://<path>&t=...` together with `browser_snapshot`; it is not an
ephemeral-registry-only architecture.

`connector_hello.host_browser` advertises:
- `supported`, `enabled`, and `status`
- `can_prepare` when the CLI can repair or launch host control on first use
- `browser_family`
- `profile_label` and `profile_path`
- `cdp_endpoint` when a browser has exposed a user-authorized DevTools endpoint
- `browser_id`, `browser_label`, and `available_browsers` for explicit Browser target selection
- `features`
- `support_reason`

The CLI detects installed Chromium-family browsers and profile roots per OS, but
it never silently seizes a locked profile. If a browser is already using the
selected profile, operations fail with `HOST_BROWSER_RELAUNCH_REQUIRED` until
the user explicitly closes that browser. When Browser settings request host
mode, Agent Zero may send an idempotent `ensure` browser operation before the
first user-facing browser action; the CLI then enables host browser control and
launches the selected profile when it is not locked. `/browser host on` and
`/browser relaunch` remain manual diagnostics rather than required happy-path
steps; `/browser list` shows advertised targets, while `/browser auto`,
`/browser <number>`, `/browser <id>`, and `/browser ws://...` sync the selected
target to the current Agent Zero project.

Chrome 136+ does not allow `--remote-debugging-port` or
`--remote-debugging-pipe` against the default personal Chrome data directory.
For those browsers, the CLI advertises an A0-controlled local profile such as
`chrome-a0 Default` under the user's data directory. Cookies and site data stay
inside that separate browser profile on the host; A0 does not copy them out.

When the browser itself exposes a user-authorized debugging server, the CLI
prefers that explicit consent path. The user opens the browser's Remote
debugging page, such as `chrome://inspect/#remote-debugging` or
`opera://inspect/#remote-debugging`, and allows remote debugging for the current
browser instance. The browser writes `DevToolsActivePort` in its user data
directory; the CLI reads that file, advertises a `*-cdp` profile, and uses a
built-in DevTools Protocol WebSocket helper. Discovery never opens a probe
connection, so status/profile checks do not trigger extra browser **Allow**
prompts. The first real Browser operation opens one long-lived connection for
that chat. A0 disconnect does not close the user's browser tabs; explicit
Browser close actions still act on tabs the agent can see.

Explicit selections accept `host:port`, HTTP(S) CDP discovery addresses, and
full DevTools WebSocket URLs. The connector resolves discovery addresses via
`/json/version` on the host before opening the WebSocket, so Agent Zero Core
does not need direct network access to the host browser.

The local-profile launch path uses the Python Playwright client installed as a
normal A0 CLI runtime dependency. It does not install a separate Chromium
binary. The Patchright runtime under the Agent Zero Docker container,
including `/a0/tmp/playwright`, powers the container browser backend and cannot
control a host Chromium-family profile from inside Docker. User-authorized
remote debugging does not require the Chrome DevTools MCP package or Playwright
CDP attach; the connector carries the small CDP helper directly. If an older or
damaged A0 installation is missing the host Python dependency, Launcher Browser
setup, `/browser host on`, `/browser relaunch`, and `/browser repair` run the
same repair with `uv pip install --python <a0-python> playwright` when uv is
available. Manual/non-uv installs fall back to `python -m pip install
playwright`, bootstrapping `pip` with `ensurepip` if the interpreter supports it.
This matters for uv-managed tool environments, which may not include a `pip`
module inside the tool Python.

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

Large file-operation results are split into multiple `connector_file_op_result` frames before crossing Socket.IO. Each frame includes `chunked: true`, `chunk_index`, `chunk_count`, `encoding: "json+base64"`, and a base64 `data` slice of the original JSON result. The Agent Zero plugin reassembles all chunks by `op_id` before resolving the pending file operation, so tool behavior still receives the same result shape as a small read.

All requested paths are resolved relative to the CLI-advertised local workspace and must remain inside that workspace after canonicalization. Absolute paths, `..` traversal, different Windows drives, and symlinks that escape the workspace are rejected before any read or write occurs.

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
- `patch` with `edits` is line-number based. A prior successful `read` or `write` is required before this form.
- `patch` with `edits` first issues an internal `stat` and compares the current CLI `mtime` against the stored state.
- No stored state produces the same prompt behavior as `_text_editor` `patch_need_read`.
- A changed `mtime` produces the same prompt behavior as `_text_editor` `patch_stale_read`.
- Line-preserving in-place patches refresh the stored metadata and may be chained without rereading.
- Insertions, deletions, or any line-count-changing patch deliberately mark the file state stale so the next patch requires a reread.
- If the connected CLI does not support internal `stat`, the plugin returns a single explicit compatibility error (`unsupported_cli_freshness`) and does not fall back to blind patching.
- `patch` with `patch_text` is context based. It accepts PseudoPatch-style update hunks and applies them against current CLI-side file content, so it does not require a prior line-number read. Successful context patches mark any stored line-number state stale.
- In `patch_text`, an insert-only hunk can use a single `@@ existing line` anchor followed by `+new line`; the insert lands immediately after the anchor.

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

`terminal`, `python`, and `nodejs` payloads may include `reset: true`. The CLI
must terminate the existing session process tree before running the replacement
command, so stuck child processes cannot keep the session or CLI shutdown path
blocked.

Execution payloads may include a `timeouts` object with the same keys used by
Agent Zero's `_code_execution` plugin settings:
- `first_output_timeout`
- `between_output_timeout`
- `max_exec_timeout`
- `dialog_timeout`

The backend selects `code_exec_timeouts` for `terminal`, `python`, `nodejs`, and
`input`, and `output_timeouts` for `output`. The CLI merges operation-level
timeouts over its latest `connector_hello.exec_config` values before monitoring
the local shell.

`connector_hello` includes `exec_config` with:
- `code_exec_timeouts`
- `output_timeouts`
- `prompt_patterns`
- `dialog_patterns`

The backend owns that execution policy. The CLI owns the local shell session and all platform-specific TTY behavior.

## Settings rehydration

Agent Zero remains the canonical settings source. On connect and at a low-frequency interval, the CLI refreshes `settings_get` plus the current `model_switcher` state and repaints only when the canonical payload changes. If a newer connector backend emits `connector_settings_updated`, the CLI applies that same snapshot path immediately.

## Security

- Public discovery stays unauthenticated.
- Protected connector HTTP handlers use Agent Zero's existing web session check: `requires_auth=True`, `requires_csrf=False`, `requires_api_key=False`.
- The connector `/ws` handler uses the same session policy.
- Connector access is independent from MCP enablement. `mcp_server_enabled` does not affect CLI access.
