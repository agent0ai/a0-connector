# a0-connector

Terminal connector for [Agent Zero](https://github.com/frdel/agent-zero). It pairs a Textual CLI with a small Agent Zero plugin so you can chat from the terminal, follow streaming events, and use the connector-specific remote editing/runtime features.

## Components

| Component | Location | Purpose |
|-----------|----------|---------|
| CLI (`agent-zero-cli`) | `src/agent_zero_cli/` | Terminal UI and session-aware transport client |
| Plugin (`_a0_connector`) | `plugin/_a0_connector/` | Builtin Agent Zero Core plugin that exposes the connector HTTP + Socket.IO surface |

The CLI requires an Agent Zero build that includes the builtin `_a0_connector` plugin.

## Install

### 1. Install the CLI

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

### 2. Use Agent Zero Core with builtin `_a0_connector`

No separate plugin install is required for users once Agent Zero Core ships `_a0_connector` as a builtin plugin.

For Core development, keep this repo's mirror in sync with the builtin plugin directory and restart Agent Zero after changes:

```bash
cd /path/to/agent-zero
mkdir -p plugins/_a0_connector
rsync -a /path/to/a0-connector/plugin/_a0_connector/ plugins/_a0_connector/
```

For Docker-based Agent Zero setups, the same builtin plugin path is `/a0/plugins/_a0_connector`.

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

- `404` on `/api/plugins/_a0_connector/v1/capabilities`: the running Agent Zero build does not include the builtin `_a0_connector` plugin, or the local Core checkout/runtime copy is out of sync.
- Browser UI works but `agentzero` does not: the core web UI can run without the connector plugin; the CLI cannot.
- `Connector contract mismatch`: the server is advertising an older connector auth contract. Update Agent Zero Core so its builtin `_a0_connector` plugin matches the CLI.
- WebSocket connection rejected: ensure proxies forward both `/socket.io` and `/api/plugins/` unchanged, and that `AGENT_ZERO_HOST` exactly matches the real host seen by Agent Zero. If Docker discovery shows `localhost`, prefer `localhost` over `127.0.0.1`.

## Docs

- [Configuration](docs/configuration.md)
- [Architecture](docs/architecture.md)
- [Development](docs/development.md)
- [TUI frontend](docs/tui-frontend.md)
