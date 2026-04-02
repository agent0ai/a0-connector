"""POST /api/plugins/a0_connector/v1/capabilities."""
from __future__ import annotations

from helpers.api import Request, Response
import usr.plugins.a0_connector.api.v1.base as connector_base


class Capabilities(connector_base.PublicConnectorApiHandler):
    """Return the connector discovery contract for current Agent Zero."""

    async def process(self, input: dict, request: Request) -> dict | Response:
        return {
            "protocol": "a0-connector.v1",
            "version": "0.1.0",
            "auth": ["api_key", "login"],
            "transports": ["http", "websocket"],
            "streaming": True,
            "websocket_namespace": "/ws",
            "websocket_handlers": ["plugins/a0_connector/ws_connector"],
            "attachments": {
                "mode": "base64",
                "max_files": 20,
            },
            "features": [
                "chat_create",
                "chats_list",
                "chat_get",
                "chat_reset",
                "chat_delete",
                "message_send",
                "log_tail",
                "projects_list",
                "text_editor_remote",
                "connector_login",
            ],
        }
