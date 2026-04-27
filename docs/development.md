# Development

## Repo layout

```
a0-connector/
├── src/agent_zero_cli/     # CLI (Textual, httpx, python-socketio)
├── packages/               # Embedded computer-use backend source and metadata
├── tests/                  # pytest
└── docs/                   # You are here
```

The builtin `_a0_connector` plugin is not vendored in this repository. Backend
changes happen directly in Agent Zero Core under `plugins/_a0_connector` (or
`/a0/plugins/_a0_connector` in Docker).

## Runtime setup options

- Local Agent Zero checkout: builtin plugin path `<agent-zero>/plugins/_a0_connector`
- Dockerized Agent Zero: builtin plugin path `/a0/plugins/_a0_connector`

## Setup

### Plugin runtime

```bash
cd /path/to/agent-zero
python run_ui.py --host=127.0.0.1 --port=50001
```

Edit the builtin `_a0_connector` plugin in that Agent Zero checkout directly, then restart Agent Zero. End users should get `_a0_connector` from Agent Zero Core as a builtin plugin.

To test a protected instance, start Agent Zero with `AUTH_LOGIN` and `AUTH_PASSWORD` configured in its runtime `.env`.

### CLI

The root editable install includes the embedded computer-use backends from
`packages/`, matching the release wheel model where the CLI and local
computer-use support update as one unit.

Windows PowerShell:

```powershell
py -3.10 -m venv .venv
. .\.venv\Scripts\Activate.ps1
pip install -e .
$env:AGENT_ZERO_HOST = "http://localhost:50001"
a0
```

Linux / Wayland:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .
export AGENT_ZERO_HOST=http://localhost:50001
a0
```

Linux / X11:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .
export AGENT_ZERO_HOST=http://localhost:50001
a0
```

One-off host overrides can also be passed directly:

```bash
a0 --host http://localhost:50001
```

macOS:

```bash
uv venv --python 3.11 .venv && source .venv/bin/activate
pip install -e .
export AGENT_ZERO_HOST=http://localhost:50001
a0
```

When you are developing against a Docker-detected local Agent Zero instance, prefer `localhost` over `127.0.0.1` so the saved host matches the discovered host exactly.

For connection-flow testing, `a0 --no-auto-connect` keeps the picker open when a single Docker instance is detected, and `a0 --no-docker-discovery` opens the manual URL path without inspecting Docker.

The published `a0` wheel embeds the Wayland, X11, macOS, and Windows backend modules. Environment markers install only the third-party runtime libraries relevant to the current platform. Linux includes both Wayland and X11 backend code; the backend resolver picks Wayland for Wayland sessions and X11 for Xorg/X11 sessions.

The sibling `packages/a0-computer-use-*` manifests remain useful for isolated backend package development, but end-user installs should use the root `a0` package.

The standalone installers and `a0 update` default to a managed CPython 3.11
runtime via `uv`, so end users do not need a preinstalled Python 3.10+ on the
host to get a consistent tool environment.

### Backend source of truth

There is no repo-local mirror to sync. The source of truth for backend work is
your Agent Zero Core/runtime copy of `plugins/_a0_connector`. The tests in this
repo resolve that plugin from `A0_CONNECTOR_PLUGIN_ROOT` when set, otherwise
from a sibling `../agent-zero/plugins/_a0_connector` checkout if present.

## Tests

```bash
pip install pytest
pytest tests/ -v
```

Uses `anyio` with the **asyncio** backend. If you see trio-related errors:

```bash
pytest -p anyio --anyio-backends=asyncio
```

## Dev patterns

### Plugin import paths

Agent Zero loads plugins by file path. All imports use the full path:

```python
import plugins._a0_connector.api.v1.base as connector_base
```

`test_plugin_backend.py` stubs the `plugins` namespace to validate these imports work.

### Lazy imports

Never import `initialize`, `agent`, or `helpers.projects` at module level in plugin code.

```python
# BAD
from agent import AgentContext

# GOOD
async def process(self, ...):
    from agent import AgentContext
```

### aiohttp compatibility shim

`client.py` patches `aiohttp.ClientWSTimeout` if missing. This keeps the Engine.IO WebSocket transport working across supported aiohttp versions.
