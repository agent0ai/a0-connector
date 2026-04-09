# Development

## Repo layout

```
a0-connector/
├── src/agent_zero_cli/     # CLI (Textual, httpx, python-socketio)
├── plugin/a0_connector/    # Plugin source for deployment to Agent Zero usr/plugins/a0_connector
├── tests/                  # pytest
└── docs/                   # You are here
```

## Runtime setup options

- Local Agent Zero checkout (recommended for plugin iteration): deploy to
  `<agent-zero>/usr/plugins/a0_connector`.
- Dockerized Agent Zero: deploy to the mapped container path
  `/a0/usr/plugins/a0_connector`.

## Setup

### Plugin runtime (in Agent Zero checkout)

```bash
cd /path/to/agent-zero
mkdir -p usr/plugins/a0_connector
rsync -a /path/to/a0-connector/plugin/a0_connector/ usr/plugins/a0_connector/
A0_SET_mcp_server_token=your-token python run_ui.py --host=127.0.0.1 --port=50001
```

### CLI (in this repo)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .
export AGENT_ZERO_HOST=http://127.0.0.1:50001
agentzero
```

### Mirroring backend changes back into this repo

If you edit an external runtime copy of the plugin, mirror changes back into
this repo copy before testing/committing:

```bash
rsync -a --delete /path/to/agent-zero/usr/plugins/a0_connector/ plugin/a0_connector/
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
