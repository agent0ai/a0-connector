# Documentation

| Document | Covers |
|----------|--------|
| [Architecture](architecture.md) | Protocol, HTTP/WebSocket surfaces, event model, remote file ops, security |
| [Configuration](configuration.md) | Environment variables, dotenv, first-run behavior, login |
| [Development](development.md) | Repo layout, local setup, tests, import gotchas |
| [TUI frontend](tui-frontend.md) | Which files define the Textual UI (widgets, styles, screens) |

## Troubleshooting

**404 on `/api/plugins/_a0_connector/v1/capabilities`**
The running Agent Zero build does not currently expose the builtin `_a0_connector` plugin.
Update Agent Zero Core to a version that includes it, or for local Core development sync this repo's mirror into
`<agent-zero>/plugins/_a0_connector` (Docker: `/a0/plugins/_a0_connector`) and restart Agent Zero.

```bash
cd /path/to/agent-zero
mkdir -p plugins/_a0_connector
rsync -a /path/to/a0-connector/plugin/_a0_connector/ plugins/_a0_connector/
# restart Agent Zero
```

**Works in browser, fails in CLI**
Same cause — the browser uses Agent Zero's built-in UI, not the connector API.

**WebSocket connection rejected**
Ensure reverse proxies forward both `/api/plugins/` and `/socket.io` to Agent Zero unchanged. Check that `AGENT_ZERO_HOST` exactly matches the actual URL (for example `localhost` vs `127.0.0.1`). If the CLI discovered the server through Docker as `localhost`, keep using `localhost`.
