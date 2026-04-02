"""POST /api/plugins/a0_connector/v1/connector_login."""
from __future__ import annotations

import asyncio

from helpers.api import Request, Response
import usr.plugins.a0_connector.api.v1.base as connector_base


class ConnectorLogin(connector_base.PublicConnectorApiHandler):
    """Exchange username + password for the connector API key."""

    async def process(self, input: dict, request: Request) -> dict | Response:
        from helpers import dotenv
        from helpers.settings import get_settings

        username = input.get("username", "")
        password = input.get("password", "")

        expected_user = dotenv.get_dotenv_value(dotenv.KEY_AUTH_LOGIN)
        expected_pass = dotenv.get_dotenv_value(dotenv.KEY_AUTH_PASSWORD)

        if not expected_user:
            settings = get_settings()
            return {"api_key": settings["mcp_server_token"]}

        if username == expected_user and password == expected_pass:
            settings = get_settings()
            return {"api_key": settings["mcp_server_token"]}

        await asyncio.sleep(1)
        return Response("Invalid credentials", status=401)
