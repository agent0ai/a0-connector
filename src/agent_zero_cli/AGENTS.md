# Agent Zero CLI DOX

## Purpose

- Own the `agent_zero_cli` Textual application, headless frontend, connector transport/session client, slash commands, local state, host browser bridge, remote file/exec tools, model/profile/project commands, and computer-use orchestration.

## Ownership

- Root package files such as `app.py`, `client.py`, `config.py`, `connection.py`, `session.py`, `gateway.py`, `protocol.py`, `event_handlers.py`, `chat_commands.py`, `goal_commands.py`, `browser_commands.py`, `computer_use.py`, `computer_use_backend.py`, `host_browser*.py`, `remote_files.py`, `remote_exec.py`, `model_*.py`, `project_*.py`, `profile_commands.py`, `permissions_commands.py`, `self_update.py`, `textual_compat.py`, and `media_refs.py` are owned here.
- `headless/` is owned here and must remain importable without Textual.
- UI widgets, screens, and TCSS are owned by child docs in `widgets/`, `screens/`, and `styles/`.
- `assets/` is currently empty; keep it root-package owned until it becomes a durable asset boundary.

## Local Contracts

- `A0Client` owns HTTP, login/session cookies, Socket.IO setup, connector event registration, and the `a0-connector.v1` protocol constants.
- Keep `aiohttp.ClientWSTimeout` compatibility in `client.py` unless all supported aiohttp versions have been verified.
- Remote file, exec, computer-use, and browser operation handlers must emit their `connector_*_op_result` event before follow-up metadata refresh work starts.
- Use the client after-result callbacks for browser and computer-use status refreshes so server-side pending operations resolve before any nested `connector_hello` round trip.
- Refresh the active chat tab metadata after context completion so server-side automatic chat renames become visible in the TUI.
- Completed TUI runs show a muted elapsed-time line immediately above the final response; the goal bar uses the same hour-aware duration format.
- `/clear` and F5 clear the visible conversation without resetting the current context; keep the initial Core greeting and Agent Zero banner visible.
- Model switcher state must use the backend's effective preset for display and identify the configured settings preset when clearing a chat override. The runtime editor updates the global `Default` preset, preserves untouched Utility and Embedding selections, and clears the active chat override after saving.
- `/computer-use on` is a human approval command. It must force `ComputerUseManager.rearm()` immediately instead of silently validating a saved restore token first.
- Computer Use must preserve explicit `window_id` targets through snapshot and type operations, and accept `window_id` as the top-level target for an explicit focus operation. Backends that advertise target-verified keyboard input fail closed unless the named window is active or focused.
- Wayland top-level focus may fall back from AT-SPI to an unambiguous `wmctrl` PID/title activation for XWayland windows, but success still requires AT-SPI active/focused verification.
- Keep the Wayland helper compatibility copy behavior-aligned with the packaged `a0-computer-use-wayland` helper; package-only bootstrap imports are the intentional difference.
- Host-browser `open` must reuse an already-open tab with the same normalized URL before creating a new tab. Keep `list` and `set_active` workflows available for title-based or URL-based selection.
- Remote host-browser operations may report status while Host Browser is off, but `ensure` and every effectful action must fail closed. Local `/browser` commands and Launcher-authorized setup may still prepare the browser through the direct manager API.
- Host-browser metadata must advertise stable browser choices, including discovered CDP browser IDs that survive `DevToolsActivePort` port/GUID changes. Incoming `browser_selection` / `host_browser_selection` values must select that browser instead of falling back to the automatic profile picker. Browser preparation may briefly wait for an existing browser's active-port file, and a failed discovered-CDP attach may retry only after that file advertises a different endpoint; explicit custom endpoints remain exact. Consent failures must tell the user to choose a Chrome profile and click Allow before retrying.
- The packaged A0 runtime includes the Python Playwright client for local-profile
  launch without bundling Chromium. Keep automatic preparation and `/browser
  repair` able to restore it in older or damaged CLI environments. Browser
  metadata must advertise dependency repair separately from profile preparation,
  and a repair attempt must run before reporting that no supported browser is
  installed.
- Explicit host-browser endpoints may be `host:port`, HTTP(S) CDP discovery addresses, or full DevTools WebSocket URLs. Resolve discovery addresses through `/json/version` on the host, preserve WebSocket path/query case, and fail explicitly instead of selecting another browser.
- Host-browser discovery covers major Chromium-family browsers with CDP-compatible profiles, including Chrome, Chromium, Edge, Brave, Opera, and Vivaldi.
- `/browser list`, `/browser auto`, and direct `/browser <number|id|host:port|ws://...>` own CLI-side host-browser target selection for the current Agent Zero project.
- `/goal <objective>` creates the active chat goal through the builtin `_goal` plugin and sends the objective to the agent; `/goal update <text>` stays silent for active goals but resends an edited complete or blocked goal so work resumes; `/goal delete` only mutates goal state.
- The connected TUI command palette must merge effective `_commands` entries for the active chat with its local command registry. Local commands win name collisions; only server-confirmed extension commands may be forwarded through the chat path for Core-side resolution.
- Composer `@` completion reuses the Textual command palette and inserts plain references only. `@./` lists the bounded local workspace while `@/` lists only the active chat's container workspace; profile, skill, and MCP rows come from the scoped Agent Editor/tool-policy state.
- `/profile` keeps direct profile selection, adds quoted name plus instructions
  for quick creation, and uses the connector `agent_editor` capability for its
  create/edit screen rather than writing Agent Zero profile files locally. The
  exact `default` utility profile is not selectable or editable; an existing
  chat may still report it as current, and Create remains available when it is
  the only profile returned by older Core versions.
- `/permissions` opens a Connector-native Tools, MCPs, and Skills policy editor
  for the current profile and persists through Core's `agent_editor` API.
- Clipboard image paste uses `wl-paste` or `xclip` on Linux and the conditionally installed Pillow native reader on macOS and Windows.
- The CLI may remember host/context and computer-use settings, and protected web sessions may persist browser-style session cookies through the remembered-host/session flow. It may consume ephemeral `A0_USERNAME` and `A0_PASSWORD` environment variables for non-interactive login, but it must not persist usernames, passwords, connector tokens, API keys, or other secrets.
- Local Docker instance discovery should prefer launcher-owned friendly names
  from the `a0.launcher.instanceName` container label over generated Docker
  container or clone image names in visible picker/login text.
- Local Docker instance discovery should try reachable Unix-socket Docker API
  endpoints from `DOCKER_HOST`, Docker contexts, and known local runtimes such as
  Colima profiles before declaring the runtime unavailable.
- On Windows, local Docker instance discovery must not require `docker.exe` on
  the host PATH. Try reachable local Docker API endpoints such as the
  launcher/WSL Engine bridge before falling back to WSL-hosted Docker commands
  through `wsl.exe`.
- Remote workspace tools must respect their write/exec enablement flags and must not widen filesystem access accidentally.
- Textual compatibility guards live in `textual_compat.py`. Install them only on the interactive TUI startup path so `a0 headless` remains Textual-free.
- `image_render.py` owns the interactive-only terminal-image adapter. It probes and captures terminal rendering capabilities before `App.run()`, while headless and gateway startup paths must not import it or `textual-image`. Automatic selection may trust a direct terminal's authoritative capability advertisement, but must reject known partial implementations such as Warp's Kitty support without the Unicode virtual placements required by `textual-image`; terminal multiplexers do not inherit that trust. If no complete TGP or Sixel path is available, automatic and unsupported forced native modes select `off`; only explicit or preview-forced half-cell mode constructs the real half-cell widget factory. Ordinary pytest launches keep their library-free half-cell renderer before native probes. Widget callers fit a `CellBox` before `create_widget()`, and native renderer failures become unavailable placeholders rather than pixelated fallback images. Visible Sixel widget trees are redrawn together after transcript viewport changes because Sixel pixels are not terminal-retained; other renderers no-op that hook. Cleanup accepts `None` and suppresses protocol release/removal failures after caller bookkeeping is cleared.
- `image_store.py` owns streamed, authenticated same-origin image loading and memory-only display-surface caching. It uses the existing `A0Client` HTTP session, fetches only traversal-free `/a0/` paths, accepts only validated raster payloads, limits encoded data to 25 MiB and decoded dimensions to 32 million pixels, permits four concurrent fetch/load operations while serializing full-resolution Pillow decode, downsamples before EXIF transpose and RGBA/RGB composition, holds the decoder permit until a canceled worker thread completes, and maintains a 64 MiB LRU of independently-owned Pillow surfaces.
- `ImageAsset` is the mutable six-field `cache_key`, `mime_type`, `image`, `width`, `height`, and `cost_bytes` contract. Clones preserve every field with an independently owned loaded surface, and `ImageStore.cache_bytes` is read-only accounting.
- `media_refs.py` owns immutable image-reference extraction. It only normalizes supported connector metadata, attachments, Markdown, bounded data URIs, and same-origin `/api/image_get` references; it performs no I/O and never accepts arbitrary external origins or paths. Browser media is limited to browser-marked `tool_start`/`tool_output` events, recognizing persisted Core `_tool_name` metadata and the existing `tool_name` compatibility shape while rejecting tool thoughts and other tools.
- `event_handlers.py` attaches normalized image references only after each event has rendered its primary/status entry. `AgentZeroCLI` owns `ImageEntry` load workers, applying an asset only while the entry generation remains mounted in its source context; stale assets are closed. Workers capture context, client, store, host, and lifecycle epoch before scheduling and revalidate before both fetch and apply.
- Clear and context switches advance the image-load epoch and cancel pending image loads while retaining the same-host cache. A host change, disconnect (including login disconnect before its first await), and exit advance it before cleanup; host change clears the store before replacing `client.base_url`, while disconnect and exit clear the chat log before clearing the store. Headless and gateway launch paths remain renderer-free.
- Transcript image ownership is browser tool metadata for browser screenshots,
  user messages for attachments, and assistant messages for assistant metadata
  or Markdown. Keep the connector event schema unchanged and do not create a
  duplicate browser screenshot event.
- `a0 gateway` is also Textual-free. Its `ConnectorSession` branch authenticates
  and publishes `connector_hello` without a chat context, handles reconnects
  without creating chats, and serves file, exec, browser, and Computer Use
  operations. It requires both Launcher gateway capabilities before announcing
  readiness.
- Gateway control uses JSONL stdin/stdout for status, scope replacement,
  browser preparation, Computer Use setup/rearm, error, and shutdown messages.
  Commands that expect a result carry `request_id`, and every nested manager
  failure must become a failed correlated gateway result rather than a success
  wrapper. `computer_use_setup_v1` gates the staged setup command independently
  from the base Launcher gateway contract. Saved
  web sessions are preferred, then ephemeral `A0_USERNAME`/`A0_PASSWORD` login;
  secrets must never appear in arguments or JSONL output. Gateway scope state
  must not overwrite interactive CLI preferences. A Launcher gateway maps the
  copied Computer Use `allow` mode to `persistent` so a natural desktop action
  may keep the platform approval prompt open without changing the interactive
  CLI's saved mode. On macOS, permission checks and polling must use fresh helper
  processes, prompt Accessibility before Screen Recording at most once per
  attempt, and finish within 120 seconds so the original Agent Zero operation
  can continue under its existing timeout. Preserve an HTTP(S) host's
  reverse-proxy base path while rejecting embedded URL credentials.
- Gateway shutdown owns complete cleanup of remote process groups, host-browser
  sessions, Computer Use sessions, and the Socket.IO connection. Emergency
  disconnect ends the current lease and exits cleanly rather than reconnecting.
- Disabling the gateway master switch or an individual long-lived capability
  closes its active execution, browser, or Computer Use sessions before the
  control acknowledgement is returned.
- Launcher gateway scopes expose file reading and writing separately. File
  writing requires file reading, and Code execution requires file writing;
  older gateway payloads with only `files` retain their previous read/write
  meaning. The command-line contract uses `file_read` for the new read-only
  selection and keeps legacy `files` read/write so a CLI update cannot silently
  downgrade an older Launcher.

## Work Guidance

- Query widgets with typed `query_one` calls, for example `self.query_one("#message-input", ChatInput)`.
- Route activity state through app-level helpers such as `_set_activity(...)` and `_set_idle()` rather than reaching into `ChatInput` from scattered event handlers.
- Keep `AgentZeroCLI` as the composition/orchestration surface; put command behavior in the focused command modules when that pattern already exists.
- Normalize server payloads defensively. The connector must tolerate older or partially-capable Agent Zero Core builds with user-facing errors.
- Keep command names, footer shortcuts, slash commands, and README/docs in sync when user-facing behavior changes.

## Verification

- Broad CLI checks: `./.venv/bin/python -m pytest tests/test_app.py tests/test_client.py -v`.
- Remote tools: `./.venv/bin/python -m pytest tests/test_remote_files.py tests/test_remote_exec.py -v`.
- Browser bridge: `./.venv/bin/python -m pytest tests/test_host_browser.py -v`.
- Computer use orchestration: `./.venv/bin/python -m pytest tests/test_computer_use.py tests/test_computer_use_contract.py -v`.
- Install/update/config paths: `./.venv/bin/python -m pytest tests/test_entrypoint.py tests/test_installers.py tests/test_self_update.py tests/test_instance_discovery.py -v`.

## Child DOX Index

- `widgets/AGENTS.md` - Reusable Textual widgets and chat rendering surfaces.
- `screens/AGENTS.md` - Modal and full-screen Textual screen contracts.
- `styles/AGENTS.md` - TCSS layout and visual styling rules.
