# text_editor_remote tool

This tool allows you to read, write, and patch files on the **remote machine where the CLI is running**.
This is different from `text_editor` which operates on the Agent Zero server's filesystem.

Use `text_editor_remote` when the user asks you to edit files on their local machine while connected via the CLI.

## Requirements
- A CLI client must be connected to this context via the shared `/ws` namespace.
- The CLI client must have enabled remote file editing support.

## Operations

### Read a file
```json
{
  "tool_name": "text_editor_remote",
  "tool_args": {
    "op": "read",
    "path": "/path/on/remote/machine/file.py",
    "line_from": 1,
    "line_to": 50
  }
}
```
Returns file content with line numbers. `line_from` and `line_to` are optional.

### Write a file
```json
{
  "tool_name": "text_editor_remote",
  "tool_args": {
    "op": "write",
    "path": "/path/on/remote/machine/file.py",
    "content": "import os\nprint('hello')\n"
  }
}
```
Creates or overwrites the file on the remote machine.

### Patch a file
```json
{
  "tool_name": "text_editor_remote",
  "tool_args": {
    "op": "patch",
    "path": "/path/on/remote/machine/file.py",
    "edits": [
      {"from": 5, "to": 5, "content": "    if x == 2:\n"}
    ]
  }
}
```
Applies line-range patches to the file. Use the same format as the standard `text_editor:patch` tool.

## Notes
- Always read the file first before patching to get current line numbers.
- Paths are evaluated on the **remote machine's filesystem**, not the Agent Zero server.
- If no CLI is connected, the tool will return an error message.
- The transport uses `connector_file_op` and `connector_file_op_result` with a shared `op_id`.
