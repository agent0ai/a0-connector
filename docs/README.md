# Documentation

| Document | Covers |
|----------|--------|
| [Architecture](architecture.md) | Protocol, HTTP/WebSocket surfaces, event model, remote file ops, security |
| [Configuration](configuration.md) | Environment variables, dotenv, first-run behavior, login |
| [Development](development.md) | Repo layout, local setup, tests, import gotchas |
| [TUI frontend](tui-frontend.md) | Which files define the Textual UI (widgets, styles, screens) |

## Troubleshooting

**404 on `/api/plugins/a0_connector/v1/capabilities`**
The plugin isn't loaded in the target Agent Zero instance.
For local development, place this repo's plugin into:
`<agent-zero>/usr/plugins/a0_connector`, then restart Agent Zero.
For Docker, place it in the mapped `/a0/usr/plugins/a0_connector` path and restart the container.

```bash
cd /path/to/agent-zero
mkdir -p usr/plugins/a0_connector
rsync -a /path/to/a0-connector/plugin/a0_connector/ usr/plugins/a0_connector/
# restart Agent Zero
```

**Works in browser, fails in CLI**
Same cause — the browser uses Agent Zero's built-in UI, not the connector API.

**WebSocket connection rejected**
Ensure reverse proxies forward both `/api/plugins/` and `/socket.io` to Agent Zero unchanged. Check that `AGENT_ZERO_HOST` exactly matches the actual URL (for example `localhost` vs `127.0.0.1`). If the CLI discovered the server through Docker as `localhost`, keep using `localhost`.
