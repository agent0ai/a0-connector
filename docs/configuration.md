# Configuration

## Environment variables

| Variable | Purpose | Default |
|----------|---------|---------|
| `AGENT_ZERO_HOST` | Agent Zero base URL | `http://localhost:5080` |
| `AGENT_ZERO_REMEMBER_HOST` | Persist the splash “Remember this host” preference for the saved host | disabled |
| `AGENT_ZERO_DEFAULT_CONTEXT_ID` / `A0_DEFAULT_CHAT` | Chat context to open after connecting | Last remembered chat for the host, then a new chat |
| `AGENT_ZERO_REMOTE_EXEC_ENABLED` / `A0_REMOTE_EXEC` | Start with host-side remote execution enabled | disabled |
| `A0_CLI_IMAGE_MODE` | Interactive terminal image renderer: `auto`, `tgp`, `sixel`, `halfcell`, or `off` | `auto` |
| `A0_UPDATE_CHECK` | Startup check for a newer CLI release. Set to `0`, `false`, `no`, or `off` to disable. | enabled |

## Resolution order

For `AGENT_ZERO_HOST`:

1. `a0 --host URL`
2. Process environment
3. `~/.agent-zero/.env`
4. Builtin default `http://localhost:5080`

`AGENT_ZERO_API_KEY` is ignored. The CLI no longer reads, writes, or uses it.

For the initial chat:

1. `a0 --chat CONTEXT_ID`
2. `AGENT_ZERO_DEFAULT_CONTEXT_ID` or `A0_DEFAULT_CHAT`
3. The last remembered chat for the connected host
4. A new chat

`a0 --chat-last` skips any configured default chat and uses the last remembered
chat for the host.

For frontend remote execution, the CLI no longer runtime-imports a local Agent Zero Core checkout. The backend sends execution settings in the WebSocket `connector_hello` payload, and the CLI keeps the platform-specific shell and TTY logic locally.

## Terminal image rendering

`A0_CLI_IMAGE_MODE=auto` is the normal user path. Before the Textual app starts,
it combines reliable terminal capability advertisements, live protocol probes,
and compatibility guards to select native TGP or Sixel. If neither complete
rendering path is available, `auto` omits image entries and preserves the
ordinary transcript. `tgp` and `sixel` also disable image rendering with one
notice when their requested path is unavailable. `halfcell` explicitly forces
the low-resolution renderer; `off` preserves the pre-image transcript and
makes no image-loading attempt. `a0 headless` and `a0 gateway` remain
text/JSONL-only regardless of this setting.

Selection follows terminal capability, not the command shell. Bash, Zsh, and
PowerShell all render images only when their hosting terminal provides the
complete native protocol.

Some terminals implement only part of a graphics protocol. Warp accepts the
basic Kitty capability query but does not implement the Unicode virtual
placements used by the Textual TGP widget, so automatic image rendering stays
off there rather than printing broken or pixelated output. A direct iTerm
session may advertise Sixel through `TERM_FEATURES` and use native raster
output; inside tmux, the CLI relies on live probing because protocol
pass-through depends on the multiplexer configuration.

Transcript images open in their expanded complete-aspect view, capped at 96 by
32 terminal cells and the available transcript width. Click the image, or focus
it and press `Enter` or `Space`, to collapse it to a 36-by-12-cell thumbnail or
expand it again.

Browser preview and SVG snapshots deliberately force half-cell rendering:
xterm.js does not validate native protocol output or cleanup. A forced TGP or
Sixel run is evidence only in a terminal verified to support that protocol.
In particular, Apple Terminal remains image-free in automatic mode unless a
capable native-protocol path has been verified separately.

### Image troubleshooting

- Leave `A0_CLI_IMAGE_MODE` unset (or set it to `auto`) to adapt to the active
  terminal automatically.
- Force `A0_CLI_IMAGE_MODE=halfcell` only when diagnosing the low-resolution renderer.
- If a forced TGP or Sixel mode reports unsupported, use `auto` or explicit `halfcell`;
  terminal multiplexers such as tmux may need protocol pass-through enabled.
- An `image unavailable` placeholder means the authenticated `/api/image_get`
  request, source validation, or image limits rejected the source; it does not
  expose URLs, cookies, or cached file paths.
- Use `A0_CLI_IMAGE_MODE=off` to require the pre-image transcript behavior.

## First-run behavior

1. Every launch starts at the picker and begins Docker-only local discovery in the background.
2. If there is exactly one detected local Agent Zero endpoint and no conflicting saved manual host, the CLI auto-enters it.
3. Open instances connect immediately.
4. Protected instances advance to login unless a valid remembered session cookie or in-memory session is already available.
5. Manual entry follows the same host rules.
6. With `Remember this host` enabled, a successful connection writes `AGENT_ZERO_HOST` and `AGENT_ZERO_REMEMBER_HOST=1` to `~/.agent-zero/.env`, removes any stale `AGENT_ZERO_API_KEY`, and for protected hosts stores the reusable session cookie jar in `~/.agent-zero/session_cookies.json`.
7. Successful chat selection remembers the active chat for that host.
8. Explicit disconnect clears the in-memory and remembered session cookie jars, attempts `/logout`, and returns to login for protected hosts or host selection for open hosts.

## Local discovery

- The startup picker only inspects Docker. It does not probe arbitrary localhost ports.
- On Windows, discovery can use the local Docker API bridge or WSL-hosted
  `docker` command even when `docker.exe` is not installed on the host PATH.
- A container is considered an Agent Zero candidate only when it is running, publishes `80/tcp`, and exposes at least one Agent Zero signal such as:
  - an image name containing `agent-zero`
  - a command or entrypoint containing `/exe/initialize.sh` or `run_ui.py`
  - a bind mount targeting `/a0`
- Wildcard Docker bindings such as `0.0.0.0`, `::`, or empty host bindings are shown as `http://localhost:<port>`.
- If Docker discovery shows `localhost`, prefer keeping `AGENT_ZERO_HOST` on `localhost` too. Mixing `localhost` and `127.0.0.1` can trigger host and origin mismatches for the session login or WebSocket flow.
- `a0 --no-auto-connect` keeps the picker open even when Docker finds exactly one local instance.
- `a0 --no-docker-discovery` skips Docker inspection and opens manual URL entry immediately.

## Persisted files

Path: `~/.agent-zero/.env`

- Created only when the CLI needs persisted settings
- Read on next launch to seed the picker, manual URL, and single-instance auto-enter decisions
- Stores `AGENT_ZERO_HOST` and `AGENT_ZERO_REMEMBER_HOST` when the host is remembered, plus the last active chat host/context after chat selection
- Never stores usernames or passwords

Path: `~/.agent-zero/session_cookies.json`

- Created only after a successful authenticated connection with `Remember this host` enabled
- Stores host-scoped session cookies so protected hosts can reconnect without prompting on every launch
- Written with owner-only permissions on Linux (`0600`)
- Cleared for that host on explicit disconnect/logout or when the server rejects the remembered session
