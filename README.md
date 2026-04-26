# a0-connector

Terminal connector for [Agent Zero](https://github.com/frdel/agent-zero). It pairs a Textual CLI with a small Agent Zero plugin so you can chat from the terminal, follow streaming events, and use the connector-specific remote editing/runtime features.

## Components

| Component | Location | Purpose |
|-----------|----------|---------|
| CLI (`a0`) | `src/agent_zero_cli/` | Terminal UI and session-aware transport client |
| Plugin (`_a0_connector`) | Agent Zero Core `plugins/_a0_connector` | Builtin plugin that exposes the connector HTTP + Socket.IO surface |

The CLI requires an Agent Zero build that includes the builtin `_a0_connector` plugin.

## Install

### 1. Install on macOS / Linux

```bash
curl -LsSf https://raw.githubusercontent.com/agent0ai/a0-connector/main/install.sh | sh
```

### 2. Install on Windows PowerShell

```powershell
irm https://raw.githubusercontent.com/agent0ai/a0-connector/main/install.ps1 | iex
```

### 3. Run

```bash
a0
```

Computer-use backends are embedded in the `a0` wheel, so the CLI and local computer-use support install and update together. Linux backend selection is automatic: Wayland sessions use the portal backend, while Xorg/X11 sessions use the X11 backend.

## Manual install

If you already use `uv`, you can install the stable `a0` release directly from
the GitHub release archive.
The installer and update flow default to a managed CPython 3.11 tool
environment across macOS, Linux, and Windows, and `uv` can download it
automatically without requiring `git` to be installed:

```bash
uv tool install --python 3.11 --managed-python --upgrade \
  "a0 @ https://github.com/agent0ai/a0-connector/archive/refs/tags/v1.5.zip"
```

Set `A0_PYTHON_SPEC` if you need to override that interpreter request, or `A0_PACKAGE_SPEC` if you want a different package source. Advanced one-off runs with `uvx` also work, but they are intentionally not the primary install path for this project.

## Update

If you installed `a0` with the standard `uv tool` flow, update it in place with:

```bash
a0 update
```

By default `a0 update` follows the same GitHub release archive and managed CPython 3.11 tool runtime used by the installer. For advanced cases you can override the package source with `A0_PACKAGE_SPEC` or the interpreter request with `A0_PYTHON_SPEC` before running `a0 update`.

`a0 update` requires `uv` to be available on your `PATH`.

## Agent Zero Core

No separate plugin install is required for users once Agent Zero Core ships `_a0_connector` as a builtin plugin.

This repo does not contain a vendored plugin copy. For Core development, edit the builtin plugin directly in your Agent Zero checkout/runtime copy and restart Agent Zero after changes:

- Local Agent Zero checkout: `<agent-zero>/plugins/_a0_connector`
- Docker-based Agent Zero runtime: `/a0/plugins/_a0_connector`

## Connect

```bash
a0
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
a0
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
- Browser UI works but `a0` does not: the core web UI can run without the connector plugin; the CLI cannot.
- `Connector contract mismatch`: the server is advertising an older connector auth contract. Update Agent Zero Core so its builtin `_a0_connector` plugin matches the CLI.
- WebSocket connection rejected: ensure proxies forward both `/socket.io` and `/api/plugins/` unchanged, and that `AGENT_ZERO_HOST` exactly matches the real host seen by Agent Zero. If Docker discovery shows `localhost`, prefer `localhost` over `127.0.0.1`.
- `a0 update` says `uv` is required: Install `uv` or rerun the existing installer.

## Docs

- [Configuration](https://github.com/agent0ai/a0-connector/blob/main/docs/configuration.md)
- [Architecture](https://github.com/agent0ai/a0-connector/blob/main/docs/architecture.md)
- [Development](https://github.com/agent0ai/a0-connector/blob/main/docs/development.md)
- [TUI frontend](https://github.com/agent0ai/a0-connector/blob/main/docs/tui-frontend.md)
