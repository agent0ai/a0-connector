"""API-key authentication helpers for the a0-connector plugin."""
from __future__ import annotations

from typing import Any


DEFAULT_HEADER_NAME = "X-API-KEY"
DEFAULT_BEARER_HEADER = "Authorization"


def get_valid_api_key() -> str | None:
    """Return the current valid API key from settings (mcp_server_token)."""
    try:
        from helpers.settings import get_settings
        return get_settings().get("mcp_server_token") or None
    except Exception:
        return None


def validate_api_key(token: str | None) -> bool:
    """Return True if *token* matches the configured API key.

    An empty / missing configured key means auth is disabled and all tokens
    (including None) are accepted.
    """
    valid = get_valid_api_key()
    if not valid:
        # No key configured – open access (mirrors HTTP ApiHandler behaviour).
        return True
    if not token:
        return False
    return token == valid


def extract_api_key_from_request() -> str | None:
    """Extract the API key from the current Flask HTTP request.

    Checks, in order:
    1. X-API-KEY header
    2. Authorization: Bearer <token> header
    3. JSON body ``api_key`` field
    4. Query-string ``api_key`` parameter
    """
    try:
        from flask import request
        # 1. X-API-KEY header
        key = request.headers.get(DEFAULT_HEADER_NAME)
        if key:
            return key
        # 2. Authorization: Bearer ...
        auth_header = request.headers.get(DEFAULT_BEARER_HEADER, "")
        if auth_header.startswith("Bearer "):
            return auth_header[7:].strip()
        # 3. JSON body
        try:
            body = request.get_json(silent=True) or {}
            if body.get("api_key"):
                return body["api_key"]
        except Exception:
            pass
        # 4. Query string
        return request.args.get("api_key")
    except Exception:
        return None


def extract_api_key_from_ws_auth(auth: Any, environ: dict[str, Any]) -> str | None:
    """Extract the API key from a Socket.IO connect auth payload or environ.

    Checks, in order:
    1. auth dict ``api_key`` field
    2. auth dict ``token`` field
    3. HTTP_X_API_KEY environ header
    4. HTTP_AUTHORIZATION environ header (Bearer)
    """
    if isinstance(auth, dict):
        key = auth.get("api_key") or auth.get("token")
        if key:
            return str(key)

    # Environ headers (set by the reverse proxy or client)
    env_key = environ.get("HTTP_X_API_KEY")
    if env_key:
        return str(env_key)

    auth_header = environ.get("HTTP_AUTHORIZATION", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:].strip()

    return None
