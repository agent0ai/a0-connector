# a0-connector

Terminal chat client + server plugin for [Agent Zero](https://github.com/frdel/agent-zero). Chat with a running Agent Zero instance from the command line, with streaming output and remote file editing.

| Component | Location | What it does |
|-----------|----------|--------------|
| **CLI** (`agent-zero-cli`) | `src/agent_zero_cli/` | Textual TUI — connects over HTTP + Socket.IO |
| **Plugin** (`a0_connector`) | `plugin/a0_connector/` | Runs inside Agent Zero — exposes HTTP routes + WebSocket handler |

> The CLI and plugin are **installed separately**. If `agentzero` returns 404 on `/api/plugins/a0_connector/v1/capabilities`, the plugin isn't loaded — see [Troubleshooting](docs/README.md#troubleshooting).

## Quick start

**1. Install the plugin** (in your Agent Zero checkout):

```bash
mkdir -p usr/plugins
ln -sfn /path/to/a0-connector/plugin/a0_connector usr/plugins/a0_connector
A0_SET_mcp_server_token=your-token python run_ui.py --host=127.0.0.1 --port=50001
```

**2. Install and run the CLI** (in this repo):

```bash
pip install -e .
pip install aiohttp>=3.11.0   # transitive runtime dep — must be installed explicitly
export AGENT_ZERO_HOST=http://127.0.0.1:50001
agentzero
```

If `AGENT_ZERO_HOST` or `AGENT_ZERO_API_KEY` are unset, the TUI prompts interactively. Values stay in memory unless you opt in to saving them to `~/.agent-zero/.env`. See [Configuration](docs/configuration.md).

## How it works

1. CLI calls `POST /api/plugins/a0_connector/v1/capabilities` (public)
2. If needed, exchanges credentials via `connector_login` for an API key
3. Opens Socket.IO on namespace `/ws` with `auth: {api_key, handlers: ["plugins/a0_connector/ws_connector"]}`
4. Creates a chat, subscribes to its event stream, and starts streaming

Protected HTTP routes use the `X-API-KEY` header (value = Agent Zero's `mcp_server_token`). All WebSocket events are `connector_`-prefixed. Full protocol details: [Architecture](docs/architecture.md).

## Key bindings & commands

| Key | Action | | Command | Action |
|-----|--------|-|---------|--------|
| Ctrl+C | Quit | | `/help` | Show help |
| F5 | Clear chat | | `/chats` | List chats |
| F6 | List chats | | `/new` | New chat |
| F7 | Nudge agent | | `/exit` | Quit |
| F8 | Pause agent | | | |

## Development

```bash
pip install -e . && pip install aiohttp>=3.11.0 pytest
pytest tests/ -v
```

Full details: [Development](docs/development.md)
