# a0-connector documentation

This folder describes how the **Agent Zero CLI** and **connector plugin** work together.

| Document | What it covers |
|----------|----------------|
| [Architecture](architecture.md) | Components, protocols, and how data moves between CLI, HTTP, and WebSocket |
| [Configuration](configuration.md) | Environment variables, `~/.agent-zero/.env`, host and login flows |
| [Development](development.md) | Running Agent Zero locally, installing the CLI, tests |

Start with [Architecture](architecture.md) if you are new to the project.

## Troubleshooting

- **404 on `/api/plugins/a0_connector/v1/capabilities`** — Agent Zero is reachable but the **plugin is not loaded**. From your Agent Zero tree: `mkdir -p usr/plugins` and `ln -sfn <path-to-this-repo>/plugin/a0_connector usr/plugins/a0_connector`, then restart. Repeat on a **remote** VPS in *its* checkout.
- **Works in browser, fails in CLI** — Same cause: the browser does not use the connector HTTP API; the CLI does.
- **Reverse proxies** — Ensure both `/api/plugins/` and `/socket.io` are forwarded to Agent Zero unchanged.
