"""Socket.IO handler for the /connector namespace.

This handler provides a clean, CLI-friendly WebSocket interface for Agent Zero.
It authenticates via API key (no CSRF, no session cookies) and exposes connector-
native events rather than frontend state-sync snapshots.

Namespace:  /connector
Auth:       API key (auth.api_key, Authorization: Bearer, or X-API-KEY header)
Security:   No origin restriction; no session/CSRF required.

Client → Server events:
  hello              { protocol, client, client_version }
  subscribe_context  { context_id, from: int }
  unsubscribe_context{ context_id }
  send_message       { context_id, message, attachments?, client_message_id? }

Server → Client events:
  hello_ok           { protocol, features }
  context_snapshot   { context_id, events, last_sequence }
  message_accepted   { context_id, client_message_id?, status }
  context_event      { context_id, sequence, event, timestamp, data }
  context_complete   { context_id, sequence }
  file_op_result     { op_id, ok, result?, error? }  ← response to text_editor_remote requests
  error              { code, message, details? }

Server → Client requests (expects response):
  file_op            { op_id, op, path, ... }  ← used by text_editor_remote tool
"""
from __future__ import annotations

import asyncio
from typing import Any, ClassVar

from helpers.print_style import PrintStyle
from helpers.websocket import WebSocketHandler, WebSocketResult


PROTOCOL_VERSION = "a0-connector.v1"


class ConnectorHandler(WebSocketHandler):
    """WebSocket handler for the /connector namespace."""

    # Class-level registry: context_id -> set of connected SIDs
    # Used by text_editor_remote tool to route file operations to the right client
    _context_subscriptions: ClassVar[dict[str, set[str]]] = {}

    # Background streaming tasks: (sid, context_id) -> Task
    _streaming_tasks: ClassVar[dict[tuple[str, str], asyncio.Task]] = {}

    # Per-SID set of subscribed context_ids
    _sid_contexts: ClassVar[dict[str, set[str]]] = {}

    @classmethod
    def requires_auth(cls) -> bool:
        return False

    @classmethod
    def requires_csrf(cls) -> bool:
        return False

    @classmethod
    def requires_api_key(cls) -> bool:
        return True

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def on_connect(self, sid: str) -> None:
        ConnectorHandler._sid_contexts[sid] = set()
        PrintStyle.debug(f"[a0-connector] /connector connected: {sid}")

    async def on_disconnect(self, sid: str) -> None:
        # Cancel all streaming tasks for this SID
        contexts = ConnectorHandler._sid_contexts.pop(sid, set())
        for ctx_id in list(contexts):
            self._cancel_streaming(sid, ctx_id)
            subs = ConnectorHandler._context_subscriptions.get(ctx_id, set())
            subs.discard(sid)
            if not subs:
                ConnectorHandler._context_subscriptions.pop(ctx_id, None)
        PrintStyle.debug(f"[a0-connector] /connector disconnected: {sid}")

    # ------------------------------------------------------------------
    # Event dispatch
    # ------------------------------------------------------------------

    async def process_event(
        self,
        event_type: str,
        data: dict[str, Any],
        sid: str,
    ) -> dict[str, Any] | WebSocketResult | None:
        handler = {
            "hello": self._handle_hello,
            "subscribe_context": self._handle_subscribe_context,
            "unsubscribe_context": self._handle_unsubscribe_context,
            "send_message": self._handle_send_message,
        }.get(event_type)

        if handler is None:
            return self.result_error(
                code="UNKNOWN_EVENT",
                message=f"Unknown event type: {event_type}",
                correlation_id=data.get("correlationId"),
            )

        try:
            return await handler(data, sid)
        except Exception as e:
            PrintStyle.error(f"[a0-connector] event={event_type} error: {e}")
            return self.result_error(
                code="INTERNAL_ERROR",
                message=str(e),
                correlation_id=data.get("correlationId"),
            )

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    async def _handle_hello(
        self, data: dict[str, Any], sid: str
    ) -> WebSocketResult:
        client = data.get("client", "unknown")
        client_version = data.get("client_version", "unknown")
        PrintStyle.debug(
            f"[a0-connector] hello from {client} {client_version} (sid={sid})"
        )
        await self.emit_to(
            sid,
            "hello_ok",
            {
                "protocol": PROTOCOL_VERSION,
                "features": [
                    "subscribe_context",
                    "send_message",
                    "text_editor_remote",
                ],
            },
        )
        return self.result_ok(correlation_id=data.get("correlationId"))

    async def _handle_subscribe_context(
        self, data: dict[str, Any], sid: str
    ) -> WebSocketResult:
        from usr.plugins.a0_connector.helpers.event_bridge import get_context_log_entries
        from agent import AgentContext

        context_id: str = data.get("context_id", "")
        from_seq: int = int(data.get("from", 0))

        if not context_id:
            return self.result_error(
                code="MISSING_CONTEXT_ID",
                message="context_id is required",
                correlation_id=data.get("correlationId"),
            )

        context = AgentContext.get(context_id)
        if context is None:
            return self.result_error(
                code="CONTEXT_NOT_FOUND",
                message=f"Context {context_id!r} not found",
                correlation_id=data.get("correlationId"),
            )

        # Register subscription
        ConnectorHandler._context_subscriptions.setdefault(context_id, set()).add(sid)
        ConnectorHandler._sid_contexts.setdefault(sid, set()).add(context_id)

        # Send snapshot of existing events
        events, last_seq = get_context_log_entries(context_id, after=from_seq)
        await self.emit_to(
            sid,
            "context_snapshot",
            {
                "context_id": context_id,
                "events": events,
                "last_sequence": last_seq,
            },
        )

        # Start background streaming task
        self._start_streaming(sid, context_id, from_sequence=last_seq)

        return self.result_ok(
            {"context_id": context_id, "subscribed": True},
            correlation_id=data.get("correlationId"),
        )

    async def _handle_unsubscribe_context(
        self, data: dict[str, Any], sid: str
    ) -> WebSocketResult:
        context_id: str = data.get("context_id", "")
        if not context_id:
            return self.result_error(
                code="MISSING_CONTEXT_ID",
                message="context_id is required",
                correlation_id=data.get("correlationId"),
            )

        self._cancel_streaming(sid, context_id)
        subs = ConnectorHandler._context_subscriptions.get(context_id, set())
        subs.discard(sid)
        ConnectorHandler._sid_contexts.get(sid, set()).discard(context_id)

        return self.result_ok(
            {"context_id": context_id, "unsubscribed": True},
            correlation_id=data.get("correlationId"),
        )

    async def _handle_send_message(
        self, data: dict[str, Any], sid: str
    ) -> WebSocketResult:
        from agent import AgentContext, AgentContextType, UserMessage
        from initialize import initialize_agent

        context_id: str | None = data.get("context_id")
        message: str = data.get("message", "")
        client_message_id: str | None = data.get("client_message_id")
        attachments: list = data.get("attachments", [])

        if not message:
            return self.result_error(
                code="MISSING_MESSAGE",
                message="message is required",
                correlation_id=data.get("correlationId"),
            )

        # Get or create context
        if context_id:
            context = AgentContext.get(context_id)
            if context is None:
                return self.result_error(
                    code="CONTEXT_NOT_FOUND",
                    message=f"Context {context_id!r} not found",
                    correlation_id=data.get("correlationId"),
                )
        else:
            config = initialize_agent()
            context = AgentContext(config=config, type=AgentContextType.USER)
            AgentContext.use(context.id)
            context_id = context.id
            # Auto-subscribe to the new context
            ConnectorHandler._context_subscriptions.setdefault(context_id, set()).add(sid)
            ConnectorHandler._sid_contexts.setdefault(sid, set()).add(context_id)
            self._start_streaming(sid, context_id, from_sequence=0)

        # Ack immediately
        await self.emit_to(
            sid,
            "message_accepted",
            {
                "context_id": context_id,
                "client_message_id": client_message_id,
                "status": "accepted",
            },
        )

        # Log user message
        context.log.log(type="user", heading="", content=message, kvps={})

        # Send message asynchronously
        asyncio.create_task(
            self._run_message(context, context_id, message, attachments, sid)
        )

        return self.result_ok(
            {"context_id": context_id, "status": "accepted"},
            correlation_id=data.get("correlationId"),
        )

    # ------------------------------------------------------------------
    # Background tasks
    # ------------------------------------------------------------------

    async def _run_message(
        self,
        context: Any,
        context_id: str,
        message: str,
        attachments: list,
        sid: str,
    ) -> None:
        """Run the agent message and notify subscribers on completion."""
        from agent import UserMessage

        try:
            task = context.communicate(
                UserMessage(message=message, attachments=attachments)
            )
            result = await task.result()
            sids = ConnectorHandler._context_subscriptions.get(context_id, set())
            for subscriber_sid in list(sids):
                try:
                    await self.emit_to(
                        subscriber_sid,
                        "context_complete",
                        {
                            "context_id": context_id,
                            "response": result or "",
                        },
                    )
                except Exception:
                    pass
        except Exception as e:
            PrintStyle.error(f"[a0-connector] _run_message error: {e}")
            sids = ConnectorHandler._context_subscriptions.get(context_id, set())
            for subscriber_sid in list(sids):
                try:
                    await self.emit_to(
                        subscriber_sid,
                        "error",
                        {"code": "AGENT_ERROR", "message": str(e), "context_id": context_id},
                    )
                except Exception:
                    pass

    def _start_streaming(self, sid: str, context_id: str, from_sequence: int = 0) -> None:
        """Start a background task that streams new context events to sid."""
        key = (sid, context_id)
        if key in ConnectorHandler._streaming_tasks:
            existing = ConnectorHandler._streaming_tasks[key]
            if not existing.done():
                return  # Already streaming

        task = asyncio.create_task(
            self._stream_events(sid, context_id, from_sequence)
        )
        ConnectorHandler._streaming_tasks[key] = task

    def _cancel_streaming(self, sid: str, context_id: str) -> None:
        key = (sid, context_id)
        task = ConnectorHandler._streaming_tasks.pop(key, None)
        if task and not task.done():
            task.cancel()

    async def _stream_events(
        self, sid: str, context_id: str, from_sequence: int = 0
    ) -> None:
        """Background streaming loop: poll context log and emit new events."""
        from usr.plugins.a0_connector.helpers.event_bridge import get_context_log_entries

        last_seq = from_sequence
        try:
            while True:
                # Check if SID is still subscribed
                if sid not in ConnectorHandler._sid_contexts:
                    break
                if context_id not in ConnectorHandler._sid_contexts.get(sid, set()):
                    break

                events, new_seq = get_context_log_entries(context_id, after=last_seq)
                for event in events:
                    try:
                        await self.emit_to(sid, "context_event", event)
                    except Exception:
                        return

                if new_seq > last_seq:
                    last_seq = new_seq

                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            PrintStyle.error(f"[a0-connector] stream error sid={sid} ctx={context_id}: {e}")
        finally:
            ConnectorHandler._streaming_tasks.pop((sid, context_id), None)

    # ------------------------------------------------------------------
    # Class-level helpers for text_editor_remote tool
    # ------------------------------------------------------------------

    @classmethod
    def get_sids_for_context(cls, context_id: str) -> set[str]:
        """Return all connected SIDs subscribed to a context.

        Used by text_editor_remote to route file-op requests to the right client.
        """
        return set(cls._context_subscriptions.get(context_id, set()))
