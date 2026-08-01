# Agent Zero CLI DOX

## Purpose

- Own the `agent_zero_cli` Textual application, headless frontend, connector transport/session client, slash commands, local state, host browser bridge, remote file/exec tools, model/profile/project commands, and computer-use orchestration.

## Ownership

- Root package files such as `app.py`, `client.py`, `config.py`, `connection.py`, `session.py`, `gateway.py`, `protocol.py`, `event_handlers.py`, `chat_commands.py`, `goal_commands.py`, `browser_commands.py`, `computer_use.py`, `computer_use_backend.py`, `host_browser*.py`, `remote_files.py`, `remote_exec.py`, `model_*.py`, `project_*.py`, `profile_commands.py`, `self_update.py`, and `textual_compat.py` are owned here.
- `headless/` is owned here and must remain importable without Textual.
- UI widgets, screens, and TCSS are owned by child docs in `widgets/`, `screens/`, and `styles/`.
- `assets/` is currently empty; keep it root-package owned until it becomes a durable asset boundary.

## Local Contracts

- `A0Client` owns HTTP, login/session cookies, Socket.IO setup, connector event registration, and the `a0-connector.v1` protocol constants.
- Keep `aiohttp.ClientWSTimeout` compatibility in `client.py` unless all supported aiohttp versions have been verified.
- Remote file, exec, computer-use, and browser operation handlers must emit their `connector_*_op_result` event before follow-up metadata refresh work starts.
- Use the client after-result callbacks for browser and computer-use status refreshes so server-side pending operations resolve before any nested `connector_hello` round trip.
- Refresh the active chat tab metadata after context completion so server-side automatic chat renames become visible in the TUI.
- Model switcher state must use the backend's effective preset for display and identify the configured settings preset when clearing a chat override. The runtime editor updates the global `Default` preset, preserves untouched Utility and Embedding selections, and clears the active chat override after saving.
- `/computer-use on` is a human approval command. It must force `ComputerUseManager.rearm()` immediately instead of silently validating a saved restore token first.
- Computer Use must preserve explicit `window_id` targets through snapshot and type operations, and accept `window_id` as the top-level target for an explicit focus operation. Backends that advertise target-verified keyboard input fail closed unless the named window is active or focused.
- Wayland top-level focus may fall back from AT-SPI to an unambiguous `wmctrl` PID/title activation for XWayland windows, but success still requires AT-SPI active/focused verification.
- Keep the Wayland helper compatibility copy behavior-aligned with the packaged `a0-computer-use-wayland` helper; package-only bootstrap imports are the intentional difference.
- Host-browser `open` must reuse an already-open tab with the same normalized URL before creating a new tab. Keep `list` and `set_active` workflows available for title-based or URL-based selection.
- Remote host-browser operations may report status while Host Browser is off, but `ensure` and every effectful action must fail closed. Local `/browser` commands and Launcher-authorized setup may still prepare the browser through the direct manager API.
- Host-browser metadata must advertise stable browser choices, and incoming `browser_selection` / `host_browser_selection` values must select that browser instead of falling back to the automatic profile picker.
- The packaged A0 runtime includes the Python Playwright client for local-profile
  launch without bundling Chromium. Keep automatic preparation and `/browser
  repair` able to restore it in older or damaged CLI environments. Browser
  metadata must advertise dependency repair separately from profile preparation,
  and a repair attempt must run before reporting that no supported browser is
  installed.
- Explicit host-browser endpoints may be `host:port`, HTTP(S) CDP discovery addresses, or full DevTools WebSocket URLs. Resolve discovery addresses through `/json/version` on the host, preserve WebSocket path/query case, and fail explicitly instead of selecting another browser.
- WebSocket recovery in `connection.py` retries with the bounded `_RECOVERY_DELAYS_SECONDS` backoff and then keeps retrying on the steady `_RECOVERY_STEADY_DELAY_SECONDS` cadence indefinitely; a container restart or long server outage must not leave the CLI permanently disconnected. Recovery exits quietly when the active context changes and aborts when the client's `base_url` changes, because a different connection owns the client then.
- Host-browser discovery covers major Chromium-family browsers with CDP-compatible profiles, including Chrome, Chromium, Edge, Brave, Opera, and Vivaldi.
- `/browser list`, `/browser auto`, and direct `/browser <number|id|host:port|ws://...>` own CLI-side host-browser target selection for the current Agent Zero project.
- `/goal <objective>` creates the active chat goal through the builtin `_goal` plugin and sends the objective to the agent; `/goal update <text>` stays silent for active goals but resends an edited complete or blocked goal so work resumes; `/goal delete` only mutates goal state.
- The connected TUI command palette must merge effective `_commands` entries for the active chat with its local command registry. Local commands win name collisions; only server-confirmed extension commands may be forwarded through the chat path for Core-side resolution.
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
