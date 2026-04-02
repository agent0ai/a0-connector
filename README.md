# a0-connector

CLI connector for the current Agent Zero runtime.

This repository holds the terminal client and the documentation for the connector plugin contract. The supported runtime is the sibling `agent-zero` checkout, with the connector plugin installed into:

```text
agent-zero/usr/plugins/a0_connector
```

The connector model is:

1. HTTP requests go to the plugin API under `/api/plugins/a0_connector/v1/...`
2. Protected HTTP endpoints use `X-API-KEY`
3. Socket.IO connects on `/ws`
4. Connector websocket activation is sent through `auth.handlers = ["plugins/a0_connector/ws_connector"]`
5. The connector secret is the Agent Zero `mcp_server_token`

## Ubuntu / Bash Setup

These commands match the supported local development flow.

```bash
cd ~/src/agent-zero
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -r requirements2.txt
mkdir -p usr/plugins
ln -sfn ~/src/a0-connector/plugin/a0_connector usr/plugins/a0_connector
A0_SET_mcp_server_token=dev-a0-connector python run_ui.py --host=127.0.0.1 --port=50001
```

In a second shell:

```bash
cd ~/src/a0-connector
git fetch origin
git checkout origin/development
python -m venv .venv
source .venv/bin/activate
pip install -e .
pip install pytest
export AGENT_ZERO_HOST=http://127.0.0.1:50001
agentzero
```

The CLI reads `AGENT_ZERO_HOST` and `AGENT_ZERO_API_KEY` from environment variables,
falling back to `~/.agent-zero/.env`. If neither is set, the CLI prompts interactively
for the host URL and login credentials. Acquired values are persisted to
`~/.agent-zero/.env` for subsequent runs.

## Connector contract

The connector is intentionally narrow:

- `capabilities` is public
- all other HTTP handlers require an API key
- the websocket handler uses the shared `/ws` namespace, not a custom namespace
- the plugin emits connector-prefixed events so it can coexist with other `/ws` handlers
- `text_editor_remote` round-trips file operations by `op_id` rather than assuming a transport-level ack primitive

The goal is to keep the CLI and plugin aligned with the current Agent Zero architecture. The CLI authenticates by exchanging credentials via the `connector_login` endpoint to obtain an API key, avoiding session-cookie coupling.
