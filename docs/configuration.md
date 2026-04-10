# Configuration

## Environment variables

| Variable | Purpose | Default |
|----------|---------|---------|
| `AGENT_ZERO_HOST` | Agent Zero base URL | `http://127.0.0.1:5080` (TUI default) |
| `AGENT_ZERO_API_KEY` | API key (= Agent Zero's `mcp_server_token`) | *(none)* |
| `AGENT_ZERO_CORE_ROOT` | Override path used to runtime-import the local Agent Zero Core `_code_execution` helpers for frontend remote exec | Tried first, then `/home/eclypso/agentdocker`, then `/a0` |

## Resolution order

For `AGENT_ZERO_HOST` and `AGENT_ZERO_API_KEY`:

1. **Process environment** — wins if set
2. **`~/.agent-zero/.env`** — `KEY=VALUE` lines (supports `#` comments)

No JSON config. `.cli-config.json` is legacy and unused.

For `AGENT_ZERO_CORE_ROOT`:

1. **Process environment** — if set, tried first as the local Agent Zero Core root for runtime imports
2. **Fallback paths** — `/home/eclypso/agentdocker`, then `/a0`

If none of those paths contains a valid Core `_code_execution` tree, the CLI still starts normally for chat and remote-file features, but `code_execution_remote` returns an unavailable error instead of using a connector-local fallback runtime.

## First-run behavior

1. **Every launch starts at the picker** — The CLI lands on the host stage first and starts Docker-only local discovery in the background.
2. **Exactly one local Docker instance found?** — If there is a single detected Agent Zero endpoint and no conflicting saved manual host, the CLI auto-connects to it.
3. **Multiple local Docker instances found?** — The splash lists detected Agent Zero WebUI endpoints and preselects the saved host when it matches one of them; otherwise it selects the first discovered instance.
4. **No instance found or saved host is outside Docker?** — Manual URL entry stays available in the same panel and auto-expands when needed.
5. **No API key + server supports login?** — After you choose a host, the TUI shows the username/password screen with a "Save credentials" checkbox. When the host came from Docker discovery, the sign-in screen explicitly identifies the detected local instance you are authenticating against.
6. **Change URL from login?** — Returns to the host picker and refreshes the Docker-discovered instance list before you choose again.
7. **Save checkbox selected?** — Writes both `AGENT_ZERO_HOST` and `AGENT_ZERO_API_KEY` to `~/.agent-zero/.env`.
8. **API key present?** — Verified against `chats_list` before connecting WebSocket.

## Local discovery

- The startup picker only inspects Docker. It does **not** probe arbitrary localhost ports.
- A container is considered an Agent Zero candidate only when it is running, publishes `80/tcp`, and exposes at least one Agent Zero signal such as:
  - an image name containing `agent-zero`
  - a command or entrypoint containing `/exe/initialize.sh` or `run_ui.py`
  - a bind mount targeting `/a0`
- Wildcard Docker bindings such as `0.0.0.0`, `::`, or empty host bindings are shown as `http://localhost:<port>`.
- If Docker discovery shows `localhost`, prefer keeping `AGENT_ZERO_HOST` on `localhost` too. Mixing `localhost` and `127.0.0.1` can trigger host/origin mismatches for the connector login or WebSocket flow.

## Persisted file

**Path:** `~/.agent-zero/.env`

- Created only when you explicitly save from the TUI
- Read on next launch to seed the picker/manual URL and single-instance auto-connect decisions; does **not** export to parent shell
- For shell-only values, use `export AGENT_ZERO_HOST=...` instead

## Server-side login

`connector_login` compares credentials against Agent Zero's `AUTH_LOGIN`/`AUTH_PASSWORD` (from `.env` in the Agent Zero checkout). If no login user is configured, it returns `mcp_server_token` directly — suitable for local development, not production.
