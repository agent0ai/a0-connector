"""Connector WebSocket handler for the shared `/ws` namespace."""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, ClassVar

from helpers.print_style import PrintStyle
from helpers.ws import WsHandler
from helpers.ws_manager import WsResult

from plugins._a0_connector.helpers.event_bridge import get_context_log_entries
from plugins._a0_connector.helpers.ws_runtime import (
    clear_remote_tree_snapshot,
    fail_pending_file_ops_for_sid,
    fail_pending_exec_ops_for_sid,
    register_sid,
    resolve_pending_file_op,
    resolve_pending_exec_op,
    store_remote_tree_snapshot,
    subscribe_sid_to_context,
    subscribed_contexts_for_sid,
    subscribed_sids_for_context,
    unsubscribe_sid_from_context,
    unregister_sid,
)

if TYPE_CHECKING:
    from agent import AgentContext, AgentContextType, UserMessage


PROTOCOL_VERSION = "a0-connector.v1"
WS_FEATURES = [
    "connector_subscribe_context",
    "connector_send_message",
    "text_editor_remote",
    "remote_file_tree",
    "code_execution_remote",
]


class WsConnector(WsHandler):
    _streaming_tasks: ClassVar[dict[tuple[str, str], asyncio.Task[None]]] = {}

    @classmethod
    def requires_auth(cls) -> bool:
        return True

    @classmethod
    def requires_csrf(cls) -> bool:
        return False

    @classmethod
    def requires_api_key(cls) -> bool:
        return False

    async def on_connect(self, sid: str) -> None:
        register_sid(sid)
        PrintStyle.debug(f"[a0-connector] /ws connected: {sid}")

    async def on_disconnect(self, sid: str) -> None:
        contexts = unregister_sid(sid)
        for context_id in contexts:
            self._cancel_streaming(sid, context_id)
        clear_remote_tree_snapshot(sid)
        fail_pending_file_ops_for_sid(
            sid,
            error="CLI disconnected before completing the requested file operation",
        )
        fail_pending_exec_ops_for_sid(
            sid,
            error="CLI disconnected before completing the requested remote execution",
        )
        PrintStyle.debug(f"[a0-connector] /ws disconnected: {sid}")

    async def process(
        self,
        event: str,
        data: dict[str, Any],
        sid: str,
    ) -> dict[str, Any] | WsResult | None:
        if event == "connector_hello":
            return {
                "protocol": PROTOCOL_VERSION,
                "features": WS_FEATURES,
            }

        if event == "connector_subscribe_context":
            return await self._handle_subscribe_context(data, sid)

        if event == "connector_unsubscribe_context":
            return self._handle_unsubscribe_context(data, sid)

        if event == "connector_send_message":
            return await self._handle_send_message(data, sid)

        if event == "connector_file_op_result":
            return self._handle_file_op_result(data, sid)

        if event == "connector_remote_tree_update":
            return self._handle_remote_tree_update(data, sid)

        if event == "connector_exec_op_result":
            return self._handle_exec_op_result(data, sid)

        if event.startswith("connector_"):
            return WsResult.error(
                code="UNKNOWN_EVENT",
                message=f"Unknown connector event: {event}",
                correlation_id=data.get("correlationId"),
            )

        return None

    async def _handle_subscribe_context(
        self,
        data: dict[str, Any],
        sid: str,
    ) -> dict[str, Any] | WsResult:
        from agent import AgentContext

        context_id = str(data.get("context_id", "")).strip()
        from_sequence = int(data.get("from", 0) or 0)

        if not context_id:
            return WsResult.error(
                code="MISSING_CONTEXT_ID",
                message="context_id is required",
                correlation_id=data.get("correlationId"),
            )

        context = AgentContext.get(context_id)
        if context is None:
            return WsResult.error(
                code="CONTEXT_NOT_FOUND",
                message=f"Context '{context_id}' not found",
                correlation_id=data.get("correlationId"),
            )

        subscribe_sid_to_context(sid, context_id)
        events, last_sequence = get_context_log_entries(context_id, after=from_sequence)
        await self.emit_to(
            sid,
            "connector_context_snapshot",
            {
                "context_id": context_id,
                "events": events,
                "last_sequence": last_sequence,
            },
            correlation_id=data.get("correlationId"),
        )
        self._start_streaming(sid, context_id, from_sequence=last_sequence)

        return {
            "context_id": context_id,
            "subscribed": True,
            "last_sequence": last_sequence,
        }

    def _handle_unsubscribe_context(
        self,
        data: dict[str, Any],
        sid: str,
    ) -> dict[str, Any] | WsResult:
        context_id = str(data.get("context_id", "")).strip()
        if not context_id:
            return WsResult.error(
                code="MISSING_CONTEXT_ID",
                message="context_id is required",
                correlation_id=data.get("correlationId"),
            )

        self._cancel_streaming(sid, context_id)
        unsubscribe_sid_from_context(sid, context_id)
        return {"context_id": context_id, "unsubscribed": True}

    async def _handle_send_message(
        self,
        data: dict[str, Any],
        sid: str,
    ) -> dict[str, Any] | WsResult:
        from plugins._a0_connector.helpers.chat_context import ConnectorContextError

        message = str(data.get("message", "")).strip()
        if not message:
            return WsResult.error(
                code="MISSING_MESSAGE",
                message="message is required",
                correlation_id=data.get("correlationId"),
        )

        context_id = str(data.get("context_id", "")).strip() or None
        current_context_id = (
            str(data.get("current_context", data.get("current_context_id", ""))).strip()
            or None
        )
        client_message_id = str(data.get("client_message_id", "")).strip()
        attachments = list(data.get("attachments", [])) if isinstance(data.get("attachments"), list) else []
        project_name = str(data.get("project_name", "")).strip() or None
        agent_profile = str(data.get("agent_profile", "")).strip() or None

        try:
            context, context_id = await self._resolve_context(
                context_id=context_id,
                current_context_id=current_context_id,
                agent_profile=agent_profile,
                project_name=project_name,
            )
        except ConnectorContextError as exc:
            return WsResult.error(
                code=exc.code,
                message=str(exc),
                correlation_id=data.get("correlationId"),
            )
        except Exception as exc:
            return WsResult.error(
                code="BAD_REQUEST",
                message=str(exc),
                correlation_id=data.get("correlationId"),
            )
        if context is None or context_id is None:
            return WsResult.error(
                code="CONTEXT_NOT_FOUND",
                message="Unable to resolve or create the requested context",
                correlation_id=data.get("correlationId"),
            )

        if context_id not in subscribed_contexts_for_sid(sid):
            subscribe_sid_to_context(sid, context_id)
            events, last_sequence = get_context_log_entries(context_id, after=0)
            await self.emit_to(
                sid,
                "connector_context_snapshot",
                {
                    "context_id": context_id,
                    "events": events,
                    "last_sequence": last_sequence,
                },
                correlation_id=data.get("correlationId"),
            )
            self._start_streaming(sid, context_id, from_sequence=last_sequence)

        message_id = client_message_id or data.get("correlationId") or ""
        context.log.log(
            type="user",
            heading="",
            content=message,
            kvps={},
            id=message_id,
        )

        asyncio.create_task(
            self._run_message(
                context=context,
                context_id=context_id,
                message=message,
                attachments=attachments,
            )
        )

        return {
            "context_id": context_id,
            "status": "accepted",
            "client_message_id": client_message_id or None,
        }

    def _handle_file_op_result(
        self,
        data: dict[str, Any],
        sid: str,
    ) -> dict[str, Any] | WsResult:
        op_id = str(data.get("op_id", "")).strip()
        if not op_id:
            return WsResult.error(
                code="MISSING_OP_ID",
                message="op_id is required",
                correlation_id=data.get("correlationId"),
            )

        if not resolve_pending_file_op(op_id, sid=sid, payload=data):
            return WsResult.error(
                code="UNKNOWN_OP_ID",
                message=f"No pending file operation for op_id '{op_id}'",
                correlation_id=data.get("correlationId"),
            )

        return {"op_id": op_id, "accepted": True}

    def _handle_remote_tree_update(
        self,
        data: dict[str, Any],
        sid: str,
    ) -> dict[str, Any] | WsResult:
        tree = data.get("tree")
        root_path = data.get("root_path")
        tree_hash = data.get("tree_hash")

        if not isinstance(tree, str) or not tree.strip():
            return WsResult.error(
                code="INVALID_TREE_PAYLOAD",
                message="tree is required",
                correlation_id=data.get("correlationId"),
            )

        if not isinstance(root_path, str) or not root_path.strip():
            return WsResult.error(
                code="INVALID_TREE_PAYLOAD",
                message="root_path is required",
                correlation_id=data.get("correlationId"),
            )

        if not isinstance(tree_hash, str) or not tree_hash.strip():
            return WsResult.error(
                code="INVALID_TREE_PAYLOAD",
                message="tree_hash is required",
                correlation_id=data.get("correlationId"),
            )

        snapshot = store_remote_tree_snapshot(sid, data)
        return {
            "accepted": True,
            "sid": sid,
            "tree_hash": tree_hash,
            "updated_at": snapshot.updated_at,
        }

    def _handle_exec_op_result(
        self,
        data: dict[str, Any],
        sid: str,
    ) -> dict[str, Any] | WsResult:
        op_id = str(data.get("op_id", "")).strip()
        if not op_id:
            return WsResult.error(
                code="MISSING_OP_ID",
                message="op_id is required",
                correlation_id=data.get("correlationId"),
            )

        if not resolve_pending_exec_op(op_id, sid=sid, payload=data):
            return WsResult.error(
                code="UNKNOWN_OP_ID",
                message=f"No pending exec operation for op_id '{op_id}'",
                correlation_id=data.get("correlationId"),
            )

        return {"op_id": op_id, "accepted": True}

    async def _resolve_context(
        self,
        *,
        context_id: str | None,
        current_context_id: str | None,
        agent_profile: str | None,
        project_name: str | None,
    ) -> tuple[AgentContext | None, str | None]:
        from plugins._a0_connector.helpers.chat_context import (
            create_context,
            get_existing_context,
        )

        if context_id:
            context = get_existing_context(
                context_id,
                agent_profile=agent_profile,
                project_name=project_name,
            )
            return context, context_id

        context = create_context(
            lock=self.lock,
            current_context_id=current_context_id,
            agent_profile=agent_profile,
            project_name=project_name,
        )
        context_id = context.id
        return context, context_id

    async def _run_message(
        self,
        *,
        context: AgentContext,
        context_id: str,
        message: str,
        attachments: list[Any],
    ) -> None:
        from agent import AgentContext, UserMessage

        try:
            AgentContext.use(context_id)
            task = context.communicate(
                UserMessage(message=message, attachments=attachments)
            )
            result = await task.result()
        except Exception as exc:
            PrintStyle.error(f"[a0-connector] connector_send_message error: {exc}")
            await self._emit_context_error(
                context_id=context_id,
                code="AGENT_ERROR",
                message=str(exc),
            )
            await self._emit_context_complete(
                context_id=context_id,
                payload={"status": "error", "error": str(exc)},
            )
            return

        await self._emit_context_complete(
            context_id=context_id,
            payload={"status": "completed", "response": result},
        )

    async def _emit_context_error(
        self,
        *,
        context_id: str,
        code: str,
        message: str,
    ) -> None:
        payload = {
            "context_id": context_id,
            "code": code,
            "message": message,
        }
        for target_sid in subscribed_sids_for_context(context_id):
            try:
                await self.emit_to(target_sid, "connector_error", payload)
            except Exception as exc:
                PrintStyle.error(
                    f"[a0-connector] failed to emit connector_error to {target_sid}: {exc}"
                )

    async def _emit_context_complete(
        self,
        *,
        context_id: str,
        payload: dict[str, Any],
    ) -> None:
        event_payload = {"context_id": context_id, **payload}
        for target_sid in subscribed_sids_for_context(context_id):
            try:
                await self.emit_to(
                    target_sid,
                    "connector_context_complete",
                    event_payload,
                )
            except Exception as exc:
                PrintStyle.error(
                    f"[a0-connector] failed to emit connector_context_complete to {target_sid}: {exc}"
                )

    def _start_streaming(self, sid: str, context_id: str, *, from_sequence: int) -> None:
        key = (sid, context_id)
        task = self._streaming_tasks.get(key)
        if task is not None and not task.done():
            return

        task = asyncio.create_task(
            self._stream_events(sid, context_id, from_sequence=from_sequence)
        )
        self._streaming_tasks[key] = task

    def _cancel_streaming(self, sid: str, context_id: str) -> None:
        task = self._streaming_tasks.pop((sid, context_id), None)
        if task is not None and not task.done():
            task.get_loop().call_soon_threadsafe(task.cancel)

    async def _stream_events(
        self,
        sid: str,
        context_id: str,
        *,
        from_sequence: int,
    ) -> None:
        # `from_sequence` is a log-output cursor (not an event sequence number).
        cursor = max(int(from_sequence or 0), 0)
        try:
            while context_id in subscribed_contexts_for_sid(sid):
                events, next_cursor = get_context_log_entries(context_id, after=cursor)
                for event in events:
                    await self.emit_to(sid, "connector_context_event", event)
                cursor = max(cursor, int(next_cursor or cursor))
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            PrintStyle.error(
                f"[a0-connector] stream error sid={sid} context={context_id}: {exc}"
            )
        finally:
            self._streaming_tasks.pop((sid, context_id), None)
