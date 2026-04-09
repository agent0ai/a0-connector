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

1. **No host?** — TUI prompts with default `http://127.0.0.1:5080`. Used for current session only.
2. **No API key + server supports login?** — TUI shows username/password screen with a "Save credentials" checkbox. On success, calls `connector_login` to get the key.
3. **Save checkbox selected?** — Writes both `AGENT_ZERO_HOST` and `AGENT_ZERO_API_KEY` to `~/.agent-zero/.env`.
4. **API key present?** — Verified against `chats_list` before connecting WebSocket.

## Persisted file

**Path:** `~/.agent-zero/.env`

- Created only when you explicitly save from the TUI
- Read on next launch; does **not** export to parent shell
- For shell-only values, use `export AGENT_ZERO_HOST=...` instead

## Server-side login

`connector_login` compares credentials against Agent Zero's `AUTH_LOGIN`/`AUTH_PASSWORD` (from `.env` in the Agent Zero checkout). If no login user is configured, it returns `mcp_server_token` directly — suitable for local development, not production.
