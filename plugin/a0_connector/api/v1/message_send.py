"""POST /api/plugins/a0_connector/v1/message_send."""
from __future__ import annotations

import base64
import os
import uuid

from helpers.api import Request, Response
from helpers.print_style import PrintStyle
from helpers.security import safe_filename
import usr.plugins.a0_connector.api.v1.base as connector_base


class MessageSend(connector_base.ProtectedConnectorApiHandler):
    async def process(self, input: dict, request: Request) -> dict | Response:
        from agent import AgentContext, AgentContextType, UserMessage
        from helpers import files, projects
        from initialize import initialize_agent

        message = str(input.get("message", "")).strip()
        if not message:
            return Response(
                response='{"error": "message is required"}',
                status=400,
                mimetype="application/json",
            )

        context_id = str(input.get("context_id", "")).strip() or None
        project_name = str(input.get("project_name", "")).strip() or None
        agent_profile = str(input.get("agent_profile", "")).strip() or None
        attachments_data = input.get("attachments", [])

        attachment_paths: list[str] = []
        if isinstance(attachments_data, list) and attachments_data:
            upload_folder_ext = files.get_abs_path("usr/uploads")
            upload_folder_int = "/a0/usr/uploads"
            os.makedirs(upload_folder_ext, exist_ok=True)

            for attachment in attachments_data:
                if not isinstance(attachment, dict):
                    continue
                filename = str(attachment.get("filename", "")).strip()
                b64_content = str(attachment.get("base64", "")).strip()
                if not filename or not b64_content:
                    continue

                try:
                    safe_name = safe_filename(filename)
                    if not safe_name:
                        continue
                    save_path = os.path.join(upload_folder_ext, safe_name)
                    with open(save_path, "wb") as handle:
                        handle.write(base64.b64decode(b64_content))
                    attachment_paths.append(os.path.join(upload_folder_int, safe_name))
                except Exception as exc:
                    PrintStyle.error(f"[a0-connector] attachment error: {exc}")

        if context_id:
            context = AgentContext.get(context_id)
            if context is None:
                return Response(
                    response='{"error": "Context not found"}',
                    status=404,
                    mimetype="application/json",
                )
            if (
                agent_profile
                and getattr(context.agent0.config, "profile", None) != agent_profile
            ):
                return Response(
                    response='{"error": "Cannot change agent_profile on existing context"}',
                    status=400,
                    mimetype="application/json",
                )
            existing_project = context.get_data(projects.CONTEXT_DATA_KEY_PROJECT)
            if project_name and existing_project and existing_project != project_name:
                return Response(
                    response='{"error": "Project can only be set on first message"}',
                    status=400,
                    mimetype="application/json",
                )
        else:
            override_settings: dict[str, str] = {}
            if agent_profile:
                override_settings["agent_profile"] = agent_profile
            context = AgentContext(
                config=initialize_agent(override_settings=override_settings),
                type=AgentContextType.USER,
            )
            AgentContext.use(context.id)
            context_id = context.id
            if project_name:
                try:
                    projects.activate_project(context_id, project_name)
                except Exception as exc:
                    return Response(
                        response=f'{{"error": "Failed to activate project: {str(exc)}"}}',
                        status=400,
                        mimetype="application/json",
                    )

        attachment_names = [os.path.basename(path) for path in attachment_paths]
        message_id = str(uuid.uuid4())
        context.log.log(
            type="user",
            heading="",
            content=message,
            kvps={"attachments": attachment_names},
            id=message_id,
        )

        try:
            task = context.communicate(
                UserMessage(message=message, attachments=attachment_paths, id=message_id)
            )
            result = await task.result()
            return {
                "context_id": context_id,
                "status": "completed",
                "response": result,
            }
        except Exception as exc:
            PrintStyle.error(f"[a0-connector] message_send error: {exc}")
            return Response(
                response=f'{{"error": "{str(exc)}"}}',
                status=500,
                mimetype="application/json",
            )
