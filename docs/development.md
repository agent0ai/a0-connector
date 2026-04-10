# Development

## Repo layout

```
a0-connector/
├── src/agent_zero_cli/     # CLI (Textual, httpx, python-socketio)
├── plugin/_a0_connector/    # Builtin plugin source mirrored into Agent Zero Core plugins/_a0_connector
├── tests/                  # pytest
└── docs/                   # You are here
```

## Runtime setup options

- Local Agent Zero checkout: builtin plugin path `<agent-zero>/plugins/_a0_connector`
- Dockerized Agent Zero: builtin plugin path `/a0/plugins/_a0_connector`

## Setup

### Plugin runtime

```bash
cd /path/to/agent-zero
mkdir -p plugins/_a0_connector
rsync -a /path/to/a0-connector/plugin/_a0_connector/ plugins/_a0_connector/
python run_ui.py --host=127.0.0.1 --port=50001
```

This sync step is for Core development only. End users should get `_a0_connector` from Agent Zero Core as a builtin plugin.

To test a protected instance, start Agent Zero with `AUTH_LOGIN` and `AUTH_PASSWORD` configured in its runtime `.env`.

### CLI

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .
export AGENT_ZERO_HOST=http://localhost:50001
agentzero
```

When you are developing against a Docker-detected local Agent Zero instance, prefer `localhost` over `127.0.0.1` so the saved host matches the discovered host exactly.

### Mirroring backend changes back into this repo

If you edit an external Core/runtime copy of the plugin, mirror changes back into this repo copy before testing or committing:

```bash
rsync -a --delete /path/to/agent-zero/plugins/_a0_connector/ plugin/_a0_connector/
```

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
