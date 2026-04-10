"""text_editor_remote tool — edit files on the CLI machine via `/ws`."""
from __future__ import annotations

import asyncio
import uuid
from typing import Any

from helpers.tool import Response, Tool
from helpers.ws import NAMESPACE
from helpers.ws_manager import ConnectionNotFoundError, get_shared_ws_manager

from plugins._a0_connector.helpers.ws_runtime import (
    clear_pending_file_op,
    select_target_sid,
    store_pending_file_op,
)


FILE_OP_TIMEOUT = 30.0
FILE_OP_EVENT = "connector_file_op"


class TextEditorRemote(Tool):
    """Send file-editing operations to the connected CLI machine."""

    async def execute(self, **kwargs: Any) -> Response:
        op = str(self.args.get("op") or self.args.get("operation", "")).strip().lower()
        if not op:
            return Response(
                message="op is required (read, write, or patch)",
                break_loop=False,
            )
        if op not in {"read", "write", "patch"}:
            return Response(
                message=f"Unknown operation: {op!r}. Use read, write, or patch.",
                break_loop=False,
            )

        path = str(self.args.get("path", "")).strip()
        if not path:
            return Response(message="path is required", break_loop=False)

        context_id = self.agent.context.id
        sid = select_target_sid(context_id)
        if not sid:
            return Response(
                message=(
                    "text_editor_remote: no CLI client connected to this context. "
                    "Make sure the CLI is connected and subscribed."
                ),
                break_loop=False,
            )

        op_id = str(uuid.uuid4())
        payload: dict[str, Any] = {
            "op_id": op_id,
            "op": op,
            "path": path,
            "context_id": context_id,
        }
        if op == "read":
            if self.args.get("line_from"):
                payload["line_from"] = int(self.args["line_from"])
            if self.args.get("line_to"):
                payload["line_to"] = int(self.args["line_to"])
        elif op == "write":
            content = self.args.get("content")
            if content is None:
                return Response(
                    message="content is required for write",
                    break_loop=False,
                )
            payload["content"] = content
        else:
            edits = self.args.get("edits")
            if not edits:
                return Response(
                    message="edits is required for patch",
                    break_loop=False,
                )
            payload["edits"] = edits

        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        store_pending_file_op(
            op_id,
            sid=sid,
            future=future,
            loop=loop,
            context_id=context_id,
        )

        try:
            await get_shared_ws_manager().emit_to(
                NAMESPACE,
                sid,
                FILE_OP_EVENT,
                payload,
                handler_id=f"{self.__class__.__module__}.{self.__class__.__name__}",
            )
            result = await asyncio.wait_for(future, timeout=FILE_OP_TIMEOUT)
        except ConnectionNotFoundError:
            clear_pending_file_op(op_id)
            return Response(
                message=(
                    "text_editor_remote: the selected CLI client disconnected before "
                    "the file operation could be delivered"
                ),
                break_loop=False,
            )
        except asyncio.TimeoutError:
            clear_pending_file_op(op_id)
            return Response(
                message=(
                    f"text_editor_remote: timed out waiting for CLI to respond "
                    f"to {op} on {path!r}"
                ),
                break_loop=False,
            )
        except Exception as exc:
            clear_pending_file_op(op_id)
            return Response(
                message=f"text_editor_remote: error sending file_op: {exc}",
                break_loop=False,
            )
        finally:
            clear_pending_file_op(op_id)

        return Response(
            message=self._extract_result(result, op, path),
            break_loop=False,
        )

    def _extract_result(self, result: Any, op: str, path: str) -> str:
        if not isinstance(result, dict):
            return f"Unexpected response format from CLI: {result!r}"

        ok = bool(result.get("ok"))
        data = result.get("result")
        error = result.get("error")

        if not ok:
            return f"Error ({op} {path!r}): {error or 'Unknown error'}"

        if not isinstance(data, dict):
            data = {}

        if op == "read":
            content = data.get("content", "")
            total_lines = data.get("total_lines", "?")
            return f"{path} {total_lines} lines\n>>>\n{content}\n<<<"
        if op == "write":
            return data.get("message") or f"{path} written successfully"
        if op == "patch":
            return data.get("message") or f"{path} patched successfully"
        return str(data)
