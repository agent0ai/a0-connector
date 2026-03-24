"""GET /api/plugins/a0_connector/v1/capabilities

Returns the connector protocol version, auth modes, transports, and available
features. This is the discovery endpoint for CLI and external clients.
"""
from __future__ import annotations

from helpers.api import ApiHandler, Request, Response


class Capabilities(ApiHandler):
    """Return connector capabilities and protocol version."""

    @classmethod
    def requires_auth(cls) -> bool:
        return False

    @classmethod
    def requires_csrf(cls) -> bool:
        return False

    @classmethod
    def requires_api_key(cls) -> bool:
        return False  # Capabilities endpoint is public (no secrets exposed)

    async def process(self, input: dict, request: Request) -> dict | Response:
        return {
            "protocol": "a0-connector.v1",
            "version": "0.1.0",
            "auth": ["api_key"],
            "transports": ["http", "websocket"],
            "streaming": True,
            "websocket_namespace": "/connector",
            "attachments": {
                "mode": "base64",
                "max_files": 20,
            },
            "features": [
                "chat_create",
                "chat_list",
                "chat_get",
                "chat_reset",
                "chat_delete",
                "message_send",
                "log_tail",
                "projects_list",
                "text_editor_remote",
            ],
        }
