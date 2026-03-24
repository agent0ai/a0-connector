"""text_editor_remote tool — edit files on the remote machine where the CLI is running.

This tool sends file-operation requests over the /connector WebSocket namespace
to the connected CLI client. The CLI executes the requested file operations
locally on its machine and returns the results.

Supported operations (matching text_editor):
  read   — read file contents with line numbers
  write  — create or overwrite a file
  patch  — apply line-range patches to an existing file

The CLI (TUI) must handle `file_op` WebSocket events and respond with results.

Event protocol (server → client):
  file_op  { op_id, op, [path, content, line_from, line_to, edits] }

Client response (inside Socket.IO ack on the `file_op` event):
  { ok: bool, result?: str, error?: str }
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Any

from helpers.tool import Tool, Response


# Timeout waiting for CLI to respond to a file_op request (seconds)
FILE_OP_TIMEOUT = 30.0


class TextEditorRemote(Tool):
    """Send file-editing operations to the connected CLI machine."""

    async def execute(self, **kwargs: Any) -> Response:
        op = self.args.get("op") or self.args.get("operation", "")
        if not op:
            return Response(message="op is required (read, write, or patch)", break_loop=False)

        op = op.strip().lower()
        if op not in ("read", "write", "patch"):
            return Response(message=f"Unknown operation: {op!r}. Use read, write, or patch.", break_loop=False)

        path = self.args.get("path", "")
        if not path:
            return Response(message="path is required", break_loop=False)

        # Build the file_op request payload
        op_id = str(uuid.uuid4())
        payload: dict[str, Any] = {"op_id": op_id, "op": op, "path": path}

        if op == "read":
            if self.args.get("line_from"):
                payload["line_from"] = int(self.args["line_from"])
            if self.args.get("line_to"):
                payload["line_to"] = int(self.args["line_to"])

        elif op == "write":
            content = self.args.get("content")
            if content is None:
                return Response(message="content is required for write", break_loop=False)
            payload["content"] = content

        elif op == "patch":
            edits = self.args.get("edits")
            if not edits:
                return Response(message="edits is required for patch", break_loop=False)
            payload["edits"] = edits

        # Find the connector handler singleton and the subscribed SID(s)
        try:
            handler = self._get_connector_handler()
        except Exception as e:
            return Response(
                message=f"text_editor_remote: could not get connector handler: {e}",
                break_loop=False,
            )

        context_id = self.agent.context.id
        sids = handler.get_sids_for_context(context_id)
        if not sids:
            return Response(
                message="text_editor_remote: no CLI client connected to this context. Make sure the CLI is connected and subscribed.",
                break_loop=False,
            )

        # Send to the first available SID (typically only one CLI connects per context)
        sid = next(iter(sids))

        try:
            result = await asyncio.wait_for(
                handler.request(
                    sid,
                    "file_op",
                    payload,
                    timeout_ms=int(FILE_OP_TIMEOUT * 1000),
                ),
                timeout=FILE_OP_TIMEOUT + 1,
            )
        except asyncio.TimeoutError:
            return Response(
                message=f"text_editor_remote: timed out waiting for CLI to respond to {op} on {path!r}",
                break_loop=False,
            )
        except Exception as e:
            return Response(
                message=f"text_editor_remote: error sending file_op: {e}",
                break_loop=False,
            )

        # Parse the aggregated request result
        response_text = self._extract_result(result, op, path)
        return Response(message=response_text, break_loop=False)

    def _get_connector_handler(self):
        """Return the ConnectorHandler singleton instance."""
        # Import here to avoid circular imports and because the handler is only
        # available after the websocket namespace has been registered.
        from usr.plugins.a0_connector.websocket.connector.main import ConnectorHandler
        from helpers.websocket import WebSocketHandler

        instance = WebSocketHandler._instances.get(ConnectorHandler)
        if instance is None:
            raise RuntimeError(
                "ConnectorHandler is not loaded. Make sure the a0_connector plugin is "
                "enabled and the /connector websocket namespace is registered."
            )
        return instance

    def _extract_result(self, result: Any, op: str, path: str) -> str:
        """Extract a human-readable result string from the request() response."""
        # result is shaped as: { correlationId, results: [ { ok, data, ... } ] }
        if not isinstance(result, dict):
            return f"Unexpected response format from CLI: {result!r}"

        results_list = result.get("results", [])
        if not results_list:
            return f"CLI returned no results for {op} on {path!r}"

        first = results_list[0]
        ok = first.get("ok", False)
        data = first.get("data") or {}
        error = first.get("error") or {}

        if not ok:
            err_msg = error.get("error") or error.get("message") or str(error)
            return f"Error ({op} {path!r}): {err_msg}"

        if op == "read":
            content = data.get("content", "")
            total_lines = data.get("total_lines", "?")
            return f"{path} {total_lines} lines\n>>>\n{content}\n<<<"

        elif op == "write":
            lines = data.get("lines_written", "?")
            return f"{path} written {lines} lines"

        elif op == "patch":
            return data.get("message") or f"{path} patched successfully"

        return str(data)
