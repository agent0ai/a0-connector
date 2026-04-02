# Development

## Repository layout

```text
a0-connector/
├── src/agent_zero_cli/     # CLI package (Textual, httpx, python-socketio)
├── plugin/a0_connector/    # Copy or symlink into Agent Zero usr/plugins/
├── tests/                    # pytest
└── docs/                     # This documentation
```

## Run Agent Zero with the plugin

From your **Agent Zero** checkout:

```bash
mkdir -p usr/plugins
ln -sfn /path/to/a0-connector/plugin/a0_connector usr/plugins/a0_connector
A0_SET_mcp_server_token=your-token python run_ui.py --host=127.0.0.1 --port=50001
```

Adjust paths and token to match your environment. The connector expects the plugin at **`usr/plugins/a0_connector`**.

## Install and run the CLI

From **this** repository:

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e .
pip install pytest          # optional, for tests
export AGENT_ZERO_HOST=http://127.0.0.1:50001
agentzero
```

See [Configuration](configuration.md) for env vars and interactive first-run behavior.

## Tests

```bash
pytest tests/ -v
```

Some Textual tests run only under the **asyncio** backend for `anyio`; if a **trio** variant fails with “no running event loop”, run with `pytest -k "not trio"` or configure `anyio` to asyncio only in `pyproject.toml` if you standardize on one backend.

## Import paths in the plugin

Agent Zero loads plugin modules by file path; handlers use imports like **`usr.plugins.a0_connector.api.v1.base`**. Avoid breaking that layout when refactoring—the test suite in `tests/test_plugin_backend.py` stubs the `usr.plugins` namespace to validate imports.

**Startup / preload:** Do not import **`initialize`** or pull in **`agent`** core at **module import time** in plugin code that loads during preload (for example `api/ws_connector.py`). That can deadlock startup while the UI shows “preload complete”. Import those modules **inside** the handler methods that run after initialization instead.
