# Headless Mode

`a0 headless` runs the Agent Zero connector over plain stdin/stdout instead of
the Textual TUI. It still connects through `_a0_connector`, subscribes to a
chat, streams connector events, publishes the local workspace tree, and handles
host-side remote file and remote exec operations while it is running.

Use it for dumb terminals, SSH sessions, CI jobs, pipes, and automation that
wants to drive Agent Zero over stdio.

## Quick Start

```bash
a0 headless --host http://localhost:32080
```

One-shot prompt from an argument:

```bash
a0 headless --host http://localhost:32080 -p "what is 2+2"
```

One-shot prompt from stdin with machine-readable output:

```bash
echo "what is 2+2" | a0 headless --host http://localhost:32080 --print --output jsonl
```

Use `--workspace` to choose the local root exposed to remote file and exec
operations:

```bash
a0 headless --host http://localhost:32080 --workspace /home/eclypso/a0/a0-connector
```

## Connection And Auth

Host resolution:

1. `--host URL`
2. `AGENT_ZERO_HOST` or saved `~/.agent-zero/.env`
3. Docker discovery, only when exactly one local Agent Zero Web UI endpoint is found

If Docker finds zero or multiple instances, headless exits with code `2` and
asks for `--host`.

Protected Agent Zero instances use the same `/login` session as the TUI. The
headless auth order is:

1. persisted session cookie for the host
2. `A0_USERNAME` and `A0_PASSWORD`
3. TTY username/password prompt
4. non-TTY failure with an actionable error

## Output Modes

`--output text` is the default. It prints assistant messages as plain text and
status/tool activity as simple, append-only lines.

`--output jsonl` prints one JSON object per line on stdout. Connector events
use `{"type":"event", ...}` and runner lifecycle records include `ready`,
`complete`, `notice`, and `error`. Prompts and human diagnostics do not corrupt
JSONL stdout.

When stderr is attached to a terminal, completion emits one terminal-native
notification after final snapshot output has settled. It never writes the
notification to stdout, and `A0_TERMINAL_NOTIFY=0` disables it.

## Slash Commands

| Command | Action |
|---------|--------|
| `/status` | Print host, context, workspace, and feature state |
| `/chats` | List chats |
| `/chat <id>` | Switch to a chat context |
| `/new` | Create and switch to a new chat |
| `/pause` / `/resume` | Pause or resume the active agent run |
| `/nudge` | Nudge the current context |
| `/send` | Send all queued messages now |
| `/queue` | Show queued messages |
| `/queue send` | Send all queued messages now |
| `/queue clear` | Clear queued messages |
| `/queue remove <number\|id>` | Remove one queued message |
| `/goal <objective>` | Set the active chat goal and send the objective to the agent |
| `/goal update <text>` | Update the goal; if complete or blocked, reactivate it and send the edited objective |
| `/goal delete` | Delete the active chat goal |
| `/clear` | Reset the current chat through `chat_reset` |
| `/quit` / `/exit` | Shut down cleanly |

TUI-only commands such as `/browser`, `/computer-use`, model pickers, plugin
screens, and attachment helpers return a one-line unavailable message in
headless mode.

## Connector Duties

Headless mode advertises:

- remote files: enabled, read/write by default, scoped to `--workspace`
- remote exec: enabled by default, running in `--workspace`
- computer use: unsupported in headless mode
- host browser: unsupported in headless mode

If a server sends a browser or computer-use operation anyway, headless replies
with a structured unsupported result so the server-side operation does not hang.

## Exit Codes

| Code | Meaning |
|------|---------|
| `0` | Completed successfully |
| `1` | Agent, command, disconnect, or connector runtime error |
| `2` | Connection, host discovery, capability, or authentication failure |
