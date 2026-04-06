"""POST /api/plugins/a0_connector/v1/model_switcher."""
from __future__ import annotations

from helpers.api import Request, Response
import usr.plugins.a0_connector.api.v1.base as connector_base


def _model_payload(config: dict | None) -> dict[str, str]:
    config = config or {}
    provider = str(config.get("provider") or "").strip()
    name = str(config.get("name") or "").strip()
    return {
        "provider": provider,
        "name": name,
        "label": f"{provider}/{name}" if provider and name else (name or provider or "—"),
    }


class ModelSwitcher(connector_base.ProtectedConnectorApiHandler):
    async def process(self, input: dict, request: Request) -> dict | Response:
        from agent import AgentContext
        from helpers.persist_chat import save_tmp_chat
        from plugins._model_config.helpers import model_config

        action = str(input.get("action", "get")).strip() or "get"
        context_id = str(input.get("context_id", "")).strip()
        context = AgentContext.get(context_id) if context_id else None
        agent = getattr(context, "agent0", None) if context is not None else None

        def build_state() -> dict[str, object]:
            override = context.get_data("chat_model_override") if context is not None else None
            return {
                "ok": True,
                "allowed": bool(model_config.is_chat_override_allowed(agent)),
                "override": override,
                "presets": model_config.get_presets(),
                "main_model": _model_payload(model_config.get_chat_model_config(agent)),
                "utility_model": _model_payload(model_config.get_utility_model_config(agent)),
            }

        if action == "get":
            return build_state()

        if not context_id:
            return Response(status=400, response="Missing context_id")

        if context is None:
            return Response(status=404, response="Context not found")

        if not model_config.is_chat_override_allowed(agent):
            return Response(status=403, response="Per-chat override is disabled")

        if action == "set_preset":
            preset_name = str(input.get("preset_name", "")).strip()
            if not preset_name:
                return Response(status=400, response="Missing preset_name")
            preset = model_config.get_preset_by_name(preset_name)
            if not preset:
                return Response(status=404, response=f"Preset '{preset_name}' not found")
            context.set_data("chat_model_override", {"preset_name": preset_name})
            save_tmp_chat(context)
            return build_state()

        if action == "clear":
            context.set_data("chat_model_override", None)
            save_tmp_chat(context)
            return build_state()

        return Response(status=400, response=f"Unknown action: {action}")
