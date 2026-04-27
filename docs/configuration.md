# Configuration

## Environment variables

| Variable | Purpose | Default |
|----------|---------|---------|
| `AGENT_ZERO_HOST` | Agent Zero base URL | `http://localhost:5080` |

## Resolution order

For `AGENT_ZERO_HOST`:

1. Process environment
2. `~/.agent-zero/.env`

`AGENT_ZERO_API_KEY` is ignored. The CLI no longer reads, writes, or uses it.

For frontend remote execution, the CLI no longer runtime-imports a local Agent Zero Core checkout. The backend sends execution settings in the WebSocket `connector_hello` payload, and the CLI keeps the platform-specific shell and TTY logic locally.

## First-run behavior

1. Every launch starts at the picker and begins Docker-only local discovery in the background.
2. If there is exactly one detected local Agent Zero endpoint and no conflicting saved manual host, the CLI auto-enters it.
3. Open instances connect immediately.
4. Protected instances advance to login unless an in-memory session is already valid.
5. Manual entry follows the same host rules.
6. With `Remember this host` enabled, a successful connection writes only `AGENT_ZERO_HOST` to `~/.agent-zero/.env` and removes any stale `AGENT_ZERO_API_KEY`.
7. Explicit disconnect clears the in-memory session cookie jar, attempts `/logout`, and returns to login for protected hosts or host selection for open hosts.

## Local discovery

- The startup picker only inspects Docker. It does not probe arbitrary localhost ports.
- A container is considered an Agent Zero candidate only when it is running, publishes `80/tcp`, and exposes at least one Agent Zero signal such as:
  - an image name containing `agent-zero`
  - a command or entrypoint containing `/exe/initialize.sh` or `run_ui.py`
  - a bind mount targeting `/a0`
- Wildcard Docker bindings such as `0.0.0.0`, `::`, or empty host bindings are shown as `http://localhost:<port>`.
- If Docker discovery shows `localhost`, prefer keeping `AGENT_ZERO_HOST` on `localhost` too. Mixing `localhost` and `127.0.0.1` can trigger host and origin mismatches for the session login or WebSocket flow.

## Persisted file

Path: `~/.agent-zero/.env`

- Created only when `Remember this host` is enabled
- Read on next launch to seed the picker, manual URL, and single-instance auto-enter decisions
- Stores only `AGENT_ZERO_HOST`
- Never stores usernames, passwords, session cookies, or tokens
