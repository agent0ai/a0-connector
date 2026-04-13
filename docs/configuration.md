# Configuration

## Environment variables

| Variable | Purpose | Default |
|----------|---------|---------|
| `AGENT_ZERO_HOST` | Agent Zero base URL | `http://127.0.0.1` (TUI placeholder; use Docker discovery or set the real URL) |
| `AGENT_ZERO_CORE_ROOT` | Override path used to runtime-import the local Agent Zero Core `_code_execution` helpers for frontend remote exec | Tried first, then `/a0` |

## Resolution order

For `AGENT_ZERO_HOST`:

1. **Process environment**
2. **`~/.agent-zero/.env`**

`AGENT_ZERO_API_KEY` is ignored. The CLI no longer reads, writes, or uses it.

For `AGENT_ZERO_CORE_ROOT`:

1. **Process environment**
2. **Fallback paths** — `/a0`

If none of those paths contains a valid Core `_code_execution` tree, the CLI still starts normally for chat and remote-file features, but `code_execution_remote` returns an unavailable error instead of using a connector-local fallback runtime.

## First-run behavior

1. **Every launch starts at the picker** — The CLI lands on the host stage first and starts Docker-only local discovery in the background.
2. **Exactly one local Docker instance found?** — If there is a single detected Agent Zero endpoint and no conflicting saved manual host, the CLI auto-enters it.
3. **Open instance?** — The CLI connects immediately.
4. **Protected instance?** — The CLI advances to the login stage unless an in-memory session is already valid.
5. **Manual entry** — The same host rules apply to manually entered URLs.
6. **Remember this host enabled?** — After a successful connection, the CLI writes only `AGENT_ZERO_HOST` to `~/.agent-zero/.env` and removes any stale `AGENT_ZERO_API_KEY` line from that file.
7. **Disconnect** — Explicit disconnect clears the in-memory session cookie jar, attempts `/logout`, and returns to login for protected hosts or host selection for open hosts.

## Local discovery

- The startup picker only inspects Docker. It does not probe arbitrary localhost ports.
- A container is considered an Agent Zero candidate only when it is running, publishes `80/tcp`, and exposes at least one Agent Zero signal such as:
  - an image name containing `agent-zero`
  - a command or entrypoint containing `/exe/initialize.sh` or `run_ui.py`
  - a bind mount targeting `/a0`
- Wildcard Docker bindings such as `0.0.0.0`, `::`, or empty host bindings are shown as `http://localhost:<port>`.
- If Docker discovery shows `localhost`, prefer keeping `AGENT_ZERO_HOST` on `localhost` too. Mixing `localhost` and `127.0.0.1` can trigger host/origin mismatches for the session login or WebSocket flow.

## Persisted file

**Path:** `~/.agent-zero/.env`

- Created only when you explicitly enable `Remember this host`
- Read on next launch to seed the picker/manual URL and single-instance auto-enter decisions
- Stores only `AGENT_ZERO_HOST`
- Never stores usernames, passwords, session cookies, or tokens
