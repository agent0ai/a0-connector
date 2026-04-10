# a0-connector

Terminal connector for [Agent Zero](https://github.com/frdel/agent-zero). It pairs a Textual CLI with a small Agent Zero plugin so you can chat from the terminal, follow streaming events, and use the connector-specific remote editing/runtime features.

## Components

| Component | Location | Purpose |
|-----------|----------|---------|
| CLI (`agent-zero-cli`) | `src/agent_zero_cli/` | Terminal UI and session-aware transport client |
| Plugin (`a0_connector`) | `plugin/a0_connector/` | Agent Zero plugin that exposes the connector HTTP + Socket.IO surface |

Both parts are required for a live session.

## Install

### 1. Install the CLI

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

### 2. Install the plugin into Agent Zero

Copy `plugin/a0_connector/` into the Agent Zero runtime at `usr/plugins/a0_connector`, then restart Agent Zero.

```bash
cd /path/to/agent-zero
mkdir -p usr/plugins/a0_connector
rsync -a /path/to/a0-connector/plugin/a0_connector/ usr/plugins/a0_connector/
```

For Docker-based Agent Zero setups, mount the same plugin directory at `/a0/usr/plugins/a0_connector` inside the container.

### 3. Connect

```bash
agentzero
```

On every launch the CLI opens the host picker first. It checks Docker for local Agent Zero containers, lists any detected WebUI endpoints as friendly URLs such as `http://localhost:50001`, and lets you connect explicitly with Enter or the `Connect` button.

If Docker finds exactly one local Agent Zero endpoint and there is no conflicting saved manual host, the CLI auto-enters that instance:
- open instance: it connects immediately
- protected instance: it advances directly to the login stage

Manual URL entry is available from the same panel for remote hosts or anything Docker cannot see. `AGENT_ZERO_HOST` still seeds the picker/manual URL instead of forcing an immediate connection.

Protected instances use the same web login as Agent Zero itself. The CLI posts to `/login`, keeps the resulting session cookie in memory for the current process, and forwards that session to `/ws`. Open instances skip the login stage entirely.

If you want to prefill a host, export it before starting the CLI:

```bash
export AGENT_ZERO_HOST=http://localhost:50001
agentzero
```

You can optionally remember only the chosen host in `~/.agent-zero/.env` from inside the app. The CLI never stores usernames, passwords, session cookies, or connector tokens.

## Usage

### Key bindings

| Key | Action |
|-----|--------|
| `Ctrl+C` | Quit |
| `F5` | Clear chat |
| `F6` | List chats |
| `F7` | Nudge agent |
| `F8` | Pause / resume |
| `Ctrl+P` | Command palette |

### Slash commands

| Command | Action |
|---------|--------|
| `/help` | Show available commands |
| `/chats` | Switch chats |
| `/new` | Start a new chat |
| `/compact` | Compact the current chat when supported |
| `/presets` | Pick a model preset |
| `/models` | Override runtime models for the current chat |
| `/disconnect` | Disconnect and return to the current host connection flow |
| `/keys` | Toggle key help |
| `/quit` | Exit |

## Troubleshooting

- `404` on `/api/plugins/a0_connector/v1/capabilities`: the plugin is not loaded in the target Agent Zero runtime.
- Browser UI works but `agentzero` does not: the core web UI can run without the connector plugin; the CLI cannot.
- `Connector contract mismatch`: the server is advertising an older connector auth contract. Update the installed `a0_connector` plugin to the session-only v1 version.
- WebSocket connection rejected: ensure proxies forward both `/socket.io` and `/api/plugins/` unchanged, and that `AGENT_ZERO_HOST` exactly matches the real host seen by Agent Zero. If Docker discovery shows `localhost`, prefer `localhost` over `127.0.0.1`.

## Docs

- [Configuration](docs/configuration.md)
- [Architecture](docs/architecture.md)
- [Development](docs/development.md)
- [TUI frontend](docs/tui-frontend.md)
