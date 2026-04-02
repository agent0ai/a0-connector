# Configuration

## Environment variables

| Variable | Purpose | Default |
|----------|---------|---------|
| `AGENT_ZERO_HOST` | Agent Zero base URL | `http://127.0.0.1:5080` (TUI default) |
| `AGENT_ZERO_API_KEY` | API key (= Agent Zero's `mcp_server_token`) | *(none)* |

## Resolution order

For both variables:

1. **Process environment** — wins if set
2. **`~/.agent-zero/.env`** — `KEY=VALUE` lines (supports `#` comments)

No JSON config. `.cli-config.json` is legacy and unused.

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
