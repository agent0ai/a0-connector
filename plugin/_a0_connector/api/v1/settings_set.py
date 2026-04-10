"""POST /api/plugins/_a0_connector/v1/settings_set."""
from __future__ import annotations

from helpers.api import Request, Response
import plugins._a0_connector.api.v1.base as connector_base


class SettingsSet(connector_base.ProtectedConnectorApiHandler):
    async def process(self, input: dict, request: Request) -> dict | Response:
        from helpers import settings

        payload = input.get("settings", input)
        if not isinstance(payload, dict):
            return Response(
                response='{"error":"settings must be an object"}',
                status=400,
                mimetype="application/json",
            )

        backend = settings.convert_in(settings.Settings(**payload))
        backend = settings.set_settings(backend)
        return dict(settings.convert_out(backend))
