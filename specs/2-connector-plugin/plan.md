# A0 Connector Plugin Plan

## Goal

Port the connector to the current Agent Zero runtime model:

- plugin installed at `agent-zero/usr/plugins/a0_connector`
- HTTP API under `/api/plugins/a0_connector/v1/...`
- websocket integration on the shared `/ws` namespace
- Socket.IO auth payload includes `api_key` and `handlers`
- `mcp_server_token` is the v1 connector secret

The connector must not depend on:

- `/connector`
- `helpers.websocket`
- session-cookie login flows
- CSRF bootstrap
- browser-only startup assumptions

## Runtime contract

### HTTP

Use the plugin API surface for all connector operations except `capabilities`.

- `capabilities` remains public
- all protected handlers require `X-API-KEY`
- protected handlers do not require session auth
- protected handlers do not require CSRF
- handlers remain POST-only

The capabilities response should advertise:

- `protocol: "a0-connector.v1"`
- `auth: ["api_key"]`
- `websocket_namespace: "/ws"`
- `websocket_handlers: ["plugins/a0_connector/ws_connector"]`

### WebSocket

The connector websocket lives on the shared `/ws` namespace.

Client auth payload:

```json
{
  "api_key": "dev-a0-connector",
  "handlers": ["plugins/a0_connector/ws_connector"]
}
```

Event names are connector-prefixed to avoid collisions on the shared namespace:

- `connector_hello`
- `connector_subscribe_context`
- `connector_unsubscribe_context`
- `connector_send_message`
- `connector_context_snapshot`
- `connector_context_event`
- `connector_context_complete`
- `connector_file_op`
- `connector_file_op_result`
- `connector_error`

The plugin should keep shared runtime state for:

- context -> subscribed SID set
- SID -> subscribed contexts
- pending file-op futures keyed by `op_id`

Do not assume a first-class server->client request/ack primitive. `text_editor_remote` should emit `connector_file_op` and resolve when the CLI returns `connector_file_op_result`.

## Implementation shape

### Plugin package

The plugin stays in the current repository layout and is installed by symlink or copy into the sibling Agent Zero checkout.

### Manifest

`plugin/a0_connector/plugin.yaml` should keep `name: a0_connector` and describe the actual connector target: HTTP plus `/ws`, API-key auth, and current Agent Zero compatibility.

### CLI

The CLI should:

1. POST `capabilities`
2. verify protected HTTP behavior with `X-API-KEY`
3. connect to `/ws`
4. send `connector_hello`
5. create or select a chat

The config file remains `.cli-config.json` and should include:

- `instance_url`
- `api_key`
- `theme`

## Ubuntu / Bash runbook

The supported setup is:

```bash
# Agent Zero runtime
cd ~/src/agent-zero
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -r requirements2.txt
mkdir -p usr/plugins
ln -sfn ~/src/a0-connector/plugin/a0_connector usr/plugins/a0_connector
A0_SET_mcp_server_token=dev-a0-connector python run_ui.py --host=127.0.0.1 --port=50001
```

```bash
# Connector repo
cd ~/src/a0-connector
git fetch origin
git checkout origin/development
python -m venv .venv
source .venv/bin/activate
pip install -e .
pip install pytest
cat > .cli-config.json <<'JSON'
{
  "instance_url": "http://127.0.0.1:50001",
  "api_key": "dev-a0-connector",
  "theme": "dark"
}
JSON
agentzero
```

## Acceptance criteria

- `capabilities` advertises the connector protocol and websocket contract
- protected HTTP endpoints require API key auth only
- the CLI connects through `/ws` with `auth.handlers`
- connector events stream over the shared namespace without collisions
- `text_editor_remote` can round-trip a file op through `op_id`
- no connector path depends on login UI, cookies, or CSRF
