# code_execution_remote tool

This tool runs Python TTY operations on the **remote machine where the CLI is running**.
Use it when the user wants code execution in the frontend machine context.

## Requirements
- A CLI client must be connected to this context via the shared `/ws` namespace.
- The CLI client must support `connector_exec_op`.

## Arguments
- `runtime`: one of `python`, `output`, `input`, `reset`
- `session`: integer session id (default `0`)

Runtime-specific fields:
- `python`: requires `code`
- `input`: requires `keyboard` (or `code` as fallback)
- `reset`: optional `reason`

## Usage

### Execute Python code
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
- Session state is frontend-local.
- `output` is for long-running operations where prior call returned before completion.
- The transport uses `connector_exec_op` and `connector_exec_op_result` with shared `op_id`.
