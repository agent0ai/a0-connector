# a0-connector

Terminal chat client and connector plugin for **Agent Zero**—chat with a running instance from the shell, with streaming output and remote file tooling.

This repo contains **two separate pieces**:

| Part | Package / path | Role |
|------|----------------|------|
| **CLI** | `agent-zero-cli` → `agentzero` | Textual TUI; HTTP + Socket.IO to your Agent Zero URL |
| **Plugin** | `plugin/a0_connector` | Must live **inside** the Agent Zero checkout as `usr/plugins/a0_connector` |

Installing or upgrading the CLI alone does **not** install the plugin on your Agent Zero server. If the CLI reports **HTTP 404** on `.../api/plugins/a0_connector/v1/capabilities`, the web UI may still work—the connector API is missing until you symlink the plugin and restart Agent Zero.

**Documentation:** [docs/](docs/README.md) — architecture, configuration, and local development.

---

## Quick start

**1. Agent Zero** — symlink the plugin and start the UI (from your Agent Zero checkout):

```bash
mkdir -p usr/plugins
ln -sfn /path/to/a0-connector/plugin/a0_connector usr/plugins/a0_connector
A0_SET_mcp_server_token=your-token python run_ui.py --host=127.0.0.1 --port=50001
```

**2. CLI** — install from this repo:

```bash
pip install -e .
export AGENT_ZERO_HOST=http://127.0.0.1:50001
agentzero
```

If `AGENT_ZERO_HOST` or `AGENT_ZERO_API_KEY` are unset, the app prompts once and can save them to `~/.agent-zero/.env`. Details: [docs/configuration.md](docs/configuration.md).

---

## Connector at a glance

- **HTTP:** `POST /api/plugins/a0_connector/v1/...` — `capabilities` is public; other routes use `X-API-KEY` (Agent Zero `mcp_server_token`).
- **WebSocket:** Socket.IO namespace `/ws` over the Engine.IO transport path `/socket.io`, with `auth.handlers` including `plugins/a0_connector/ws_connector`.
- **Login:** Optional `connector_login` to exchange username/password for the API key when no key is configured—see [docs/architecture.md](docs/architecture.md).

---

## Development

```bash
pip install -e .
pip install pytest
pytest tests/ -v
```

Full setup, test notes, and repo layout: [docs/development.md](docs/development.md).
