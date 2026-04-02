# Development

## Repo layout

```
a0-connector/
├── src/agent_zero_cli/     # CLI (Textual, httpx, python-socketio)
├── plugin/a0_connector/    # Plugin — symlink into Agent Zero usr/plugins/
├── tests/                  # pytest
└── docs/                   # You are here
```

## Setup

### Plugin (in Agent Zero checkout)

```bash
mkdir -p usr/plugins
ln -sfn /path/to/a0-connector/plugin/a0_connector usr/plugins/a0_connector
A0_SET_mcp_server_token=your-token python run_ui.py --host=127.0.0.1 --port=50001
```

### CLI (in this repo)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .
pip install aiohttp>=3.11.0  # required by python-engineio for WebSocket transport
export AGENT_ZERO_HOST=http://127.0.0.1:50001
agentzero
```

> `aiohttp` is a transitive dependency of `python-engineio` but not declared in `pyproject.toml`. You must install it explicitly or WebSocket connections will fail.

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
import usr.plugins.a0_connector.api.v1.base as connector_base
```

`test_plugin_backend.py` stubs the `usr.plugins` namespace to validate these imports work.

### Lazy imports (deadlock prevention)

**Never** import `initialize`, `agent`, or `helpers.projects` at module level in plugin code. Agent Zero preloads plugin modules before initialization completes — top-level imports of these modules will deadlock.

```python
# BAD — module-level import
from agent import AgentContext

# GOOD — inside handler method
async def process(self, ...):
    from agent import AgentContext
```

### aiohttp compatibility shim

`client.py` patches `aiohttp.ClientWSTimeout` if missing (older aiohttp versions). This keeps the Engine.IO WebSocket transport working across versions.
