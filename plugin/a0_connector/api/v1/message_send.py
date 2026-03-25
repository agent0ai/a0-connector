"""POST /api/plugins/a0_connector/v1/message_send

Sends a message to an Agent Zero context.
"""
from __future__ import annotations

import base64
import os

from helpers.api import ApiHandler, Request, Response
from helpers.print_style import PrintStyle
from helpers.security import safe_filename


class MessageSend(ApiHandler):
    @classmethod
    def requires_auth(cls) -> bool:
        return True

    @classmethod
    def requires_csrf(cls) -> bool:
        return False

    @classmethod
    def requires_api_key(cls) -> bool:
        return False

    async def process(self, input: dict, request: Request) -> dict | Response:
        from agent import AgentContext, AgentContextType, UserMessage
        from initialize import initialize_agent
        from helpers import projects, files

        message: str = input.get("message", "")
        if not message:
            return Response('{"error": "message is required"}', status=400, mimetype="application/json")

        context_id: str | None = input.get("context_id")
        project_name: str | None = input.get("project_name")
        agent_profile: str | None = input.get("agent_profile")
        attachments_data: list = input.get("attachments", [])

        # Handle attachments (base64 encoded)
        attachment_paths: list[str] = []
        if attachments_data:
            upload_folder_ext = files.get_abs_path("usr/uploads")
            upload_folder_int = "/a0/usr/uploads"
            os.makedirs(upload_folder_ext, exist_ok=True)

            for attachment in attachments_data:
                if not isinstance(attachment, dict):
                    continue
                filename = attachment.get("filename", "")
                b64_content = attachment.get("base64", "")
                if not filename or not b64_content:
                    continue
                try:
                    safe_name = safe_filename(filename)
                    if not safe_name:
                        continue
                    file_content = base64.b64decode(b64_content)
                    save_path = os.path.join(upload_folder_ext, safe_name)
                    with open(save_path, "wb") as f:
                        f.write(file_content)
                    attachment_paths.append(os.path.join(upload_folder_int, safe_name))
                except Exception as e:
                    PrintStyle.error(f"[a0-connector] attachment error: {e}")

        # Get or create context
        if context_id:
            context = AgentContext.get(context_id)
            if context is None:
                return Response('{"error": "Context not found"}', status=404, mimetype="application/json")
            if agent_profile and getattr(context.agent0.config, "profile", None) != agent_profile:
                return Response('{"error": "Cannot change agent_profile on existing context"}', status=400, mimetype="application/json")
        else:
            override_settings: dict = {}
            if agent_profile:
                override_settings["agent_profile"] = agent_profile
            config = initialize_agent(override_settings=override_settings)
            context = AgentContext(config=config, type=AgentContextType.USER)
            AgentContext.use(context.id)
            context_id = context.id
            if project_name:
                try:
                    projects.activate_project(context_id, project_name)
                except Exception as e:
                    return Response(f'{{"error": "Failed to activate project: {str(e)}"}}', status=400, mimetype="application/json")

        # Log user message
        context.log.log(
            type="user",
            heading="",
            content=message,
            kvps={"attachments": [os.path.basename(p) for p in attachment_paths]},
        )

        # Send message
        try:
            task = context.communicate(
                UserMessage(message=message, attachments=attachment_paths)
            )
            result = await task.result()
            return {
                "context_id": context_id,
                "status": "completed",
                "response": result,
            }
        except Exception as e:
            PrintStyle.error(f"[a0-connector] message_send error: {e}")
            return Response(f'{{"error": "{str(e)}"}}', status=500, mimetype="application/json")
