# code_execution_remote tool

This tool runs shell-backed execution on the **remote machine where the CLI is running**.
It converges onto Agent Zero Core's persistent local-shell model, so the frontend session
can execute terminal commands and shell-launched `python` / `nodejs` snippets while keeping
session ids stable across calls.

## Requirements
- A CLI client must be connected to this context via the shared `/ws` namespace.
- The CLI client must support `connector_exec_op`.
- Frontend execution may be locally disabled in the CLI session; in that case the result is
  a structured `{ok: false}` error and no fallback runtime is used.

## Arguments
- `runtime`: one of `terminal`, `python`, `nodejs`, `output`, `reset`
- `runtime=input` is a temporary deprecated compatibility alias for sending one line of
  keyboard input into a running shell session
- `session`: integer session id (default `0`)

Runtime-specific fields:
- `terminal`, `python`, `nodejs`: require `code`
- `input`: requires `keyboard` (or `code` as fallback)
- `reset`: optional `reason`

## Usage

### Execute a terminal command
```json
{
  "tool_name": "code_execution_remote",
  "tool_args": {
    "runtime": "terminal",
    "session": 0,
    "code": "pwd && ls -la"
  }
}
```

### Execute Python through the shell-backed runtime
```json
{
  "tool_name": "code_execution_remote",
  "tool_args": {
    "runtime": "python",
    "session": 0,
    "code": "import os\nprint(os.getcwd())"
  }
}
```

### Execute Node.js through the shell-backed runtime
```json
{
  "tool_name": "code_execution_remote",
  "tool_args": {
    "runtime": "nodejs",
    "session": 0,
    "code": "console.log(process.cwd())"
  }
}
```

### Poll output from a running session
```json
{
  "tool_name": "code_execution_remote",
  "tool_args": {
    "runtime": "output",
    "session": 0
  }
}
```

### Send keyboard input to a running session
```json
{
  "tool_name": "code_execution_remote",
  "tool_args": {
    "runtime": "input",
    "session": 0,
    "keyboard": "yes"
  }
}
```

### Reset a session
```json
{
  "tool_name": "code_execution_remote",
  "tool_args": {
    "runtime": "reset",
    "session": 0,
    "reason": "stuck process"
  }
}
```

## Notes
- Session state is frontend-local and shell-backed.
- `output` is for long-running operations where a prior call returned control before the
  shell reached a prompt.
- The transport uses `connector_exec_op` and `connector_exec_op_result` with shared `op_id`.
