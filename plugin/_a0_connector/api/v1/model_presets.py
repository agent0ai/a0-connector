"""POST /api/plugins/_a0_connector/v1/model_presets."""
from __future__ import annotations

from helpers.api import Request, Response
import plugins._a0_connector.api.v1.base as connector_base


class ModelPresets(connector_base.ProtectedConnectorApiHandler):
    async def process(self, input: dict, request: Request) -> dict | Response:
        from plugins._model_config.helpers import model_config

        action = str(input.get("action", "get")).strip() or "get"

        if action == "get":
            presets = model_config.get_presets()
            return {"ok": True, "presets": presets}

        if action == "save":
            presets = input.get("presets")
            if not isinstance(presets, list):
                return Response(status=400, response="presets must be an array")
            model_config.save_presets(presets)
            return {"ok": True, "presets": presets}

        if action == "reset":
            presets = model_config.reset_presets()
            return {"ok": True, "presets": presets}

        return Response(status=400, response=f"Unknown action: {action}")
