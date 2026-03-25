"""Session-based authentication helpers for the a0-connector plugin.

The connector uses Agent Zero's built-in session auth (login with username
and password).  The core framework handles session validation automatically
when API handlers and WebSocket namespaces declare ``requires_auth = True``.

This module provides lightweight utilities for checking session state from
plugin code that runs outside the normal request-decorator pipeline.
"""
from __future__ import annotations


def is_auth_enabled() -> bool:
    """Return True if Agent Zero has authentication credentials configured."""
    try:
        from helpers import login
        return bool(login.get_credentials_hash())
    except Exception:
        return False


def get_session_user_id() -> str:
    """Return the current session's user_id, or 'single_user' as fallback."""
    try:
        from flask import session
        return session.get("user_id") or "single_user"
    except Exception:
        return "single_user"
