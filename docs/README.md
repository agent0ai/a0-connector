# Documentation

| Document | Covers |
|----------|--------|
| [Architecture](architecture.md) | Protocol, HTTP/WebSocket surfaces, event model, remote file ops, security |
| [Configuration](configuration.md) | Environment variables, dotenv, first-run behavior, login |
| [Development](development.md) | Repo layout, local setup, tests, import gotchas |
| [TUI frontend](tui-frontend.md) | Which files define the Textual UI (widgets, styles, screens) |

## Troubleshooting

**404 on `/api/plugins/a0_connector/v1/capabilities`**
The plugin isn't loaded. In your Agent Zero checkout:

```bash
mkdir -p usr/plugins
ln -sfn /path/to/a0-connector/plugin/a0_connector usr/plugins/a0_connector
# restart Agent Zero
```

On a remote host, repeat the symlink in *that* checkout.

**Works in browser, fails in CLI**
Same cause — the browser uses Agent Zero's built-in UI, not the connector API.

**WebSocket connection rejected**
Ensure reverse proxies forward both `/api/plugins/` and `/socket.io` to Agent Zero unchanged. Check that `AGENT_ZERO_HOST` matches the actual URL (e.g. `localhost` vs `127.0.0.1`).
