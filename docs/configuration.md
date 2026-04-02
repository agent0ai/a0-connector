# Configuration

The CLI resolves **host** and **API key** in a fixed order. Values you enter in the TUI stay in memory for the current run unless you explicitly opt in to saving them.

## Resolution order

For both **`AGENT_ZERO_HOST`** and **`AGENT_ZERO_API_KEY`**:

1. **Process environment** — if set, this wins.
2. **`~/.agent-zero/.env`** — simple `KEY=VALUE` lines (comments with `#` are ignored).

There is no `JSON` config file; legacy **`.cli-config.json`** is not used.

## Environment variables

| Variable | Meaning |
|----------|---------|
| `AGENT_ZERO_HOST` | Base URL of Agent Zero, e.g. `http://127.0.0.1:50001` (no trailing slash required; the client normalizes). |
| `AGENT_ZERO_API_KEY` | Same value as Agent Zero **`mcp_server_token`** for protected HTTP and `/ws` auth. |

## First-run behavior

1. **Host** — If `AGENT_ZERO_HOST` is empty after env + dotenv, the TUI shows a host input screen with default **`http://127.0.0.1:5080`**. The chosen value is used for the current CLI process immediately, but is not written to disk on its own.

2. **API key** — If `AGENT_ZERO_API_KEY` is still empty and the server advertises **`login`** in capabilities, the TUI shows **username / password** plus a checkbox to save credentials. On success, the CLI calls **`connector_login`**, receives **`api_key`**, and sets it on the client for the current run. If the save checkbox is selected, the CLI writes both **`AGENT_ZERO_HOST`** and **`AGENT_ZERO_API_KEY`** to **`~/.agent-zero/.env`**.

3. **Verification** — If an API key is present, the CLI verifies it against a protected endpoint (`chats_list`) before opening the WebSocket.

## Persisted file

Path: **`~/.agent-zero/.env`**

- Created on demand only when you explicitly choose to save credentials from the TUI.
- Not a shell `export` in the parent process: the CLI **writes** this file and reads it on the next launch. A child TUI process cannot export environment variables back into the shell that launched it. To use values only for the current shell session, set **`export AGENT_ZERO_*`** in your shell instead and avoid writing the file (or remove keys from the file after editing manually).

## Server-side login

The plugin’s **`connector_login`** compares credentials to Agent Zero’s **`AUTH_LOGIN`** / **`AUTH_PASSWORD`** (from `.env` in the Agent Zero tree). If no UI login user is configured, the endpoint may still return **`mcp_server_token`** so open local instances can be used without typing a password—treat that as appropriate for your deployment model.
