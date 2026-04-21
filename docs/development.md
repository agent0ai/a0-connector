# Development

## Repo layout

```
a0-connector/
├── src/agent_zero_cli/     # CLI (Textual, httpx, python-socketio)
├── packages/               # Published backend package scaffolding and metadata
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

Repo-local editable installs need the matching backend package from `packages/`
in the same `pip` invocation. The published `a0` wheel can pull the platform
backend from an index, but a workspace checkout cannot infer that sibling
package automatically.

Windows PowerShell:

```powershell
py -3.10 -m venv .venv
. .\.venv\Scripts\Activate.ps1
pip install -e .\packages\a0-computer-use-windows -e .
$env:AGENT_ZERO_HOST = "http://localhost:50001"
a0
```

Linux / Wayland:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ./packages/a0-computer-use-wayland -e .
export AGENT_ZERO_HOST=http://localhost:50001
a0
```

macOS:

```bash
uv venv --python 3.11 .venv && source .venv/bin/activate
pip install -e ./packages/a0-computer-use-macos -e .
export AGENT_ZERO_HOST=http://localhost:50001
a0
```

When you are developing against a Docker-detected local Agent Zero instance, prefer `localhost` over `127.0.0.1` so the saved host matches the discovered host exactly.

The published `a0` wheel uses environment markers to pull the matching computer-use backend automatically. Linux installs `a0-computer-use-wayland`, macOS installs `a0-computer-use-macos`, Windows installs `a0-computer-use-windows`, and `a0-computer-use-x11` remains reserved for a future X11-specific backend.

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
