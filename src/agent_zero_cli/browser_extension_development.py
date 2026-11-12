"""Explicit local-source execution, isolated from signed companion discovery.

The caller trusts the supplied program by choosing it. Checking its response is
protocol validation, not provenance or production release verification. Native
code alone owns installation, registration, collision checks, and removal.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat
import sys
from typing import Any, Sequence, TextIO

from .browser_extension import (
    BROWSER_CHOICES, BrowserExtensionOptions, _BrowserExtensionFailure,
    _emit_result, _normalize_browsers, _result_payload, _run_process,
)

DEVELOPMENT_CONTRACT = "a0.browser-bridge.development.v1"
DEVELOPMENT_HOST = "io.agentzero.browser_bridge.dev"
DEVELOPMENT_EXTENSION = "paoagmddepkmonpeboobaijlenlcokpc"
_FIELDS = frozenset({
    "contract", "schema_version", "channel", "action", "state", "reason_code",
    "companion_version", "native_host_name", "extension_id", "registered_browsers",
    "registration_count", "already_current", "mutation_allowed", "exit_code",
})
_STATES = {"installed", "not_installed", "unhealthy", "unsupported", "blocked"}


def _failure(code: str, message: str, exit_code: int = 5) -> _BrowserExtensionFailure:
    return _BrowserExtensionFailure(exit_code, code, message)


def _explicit_source(value: str) -> str:
    path = Path(value)
    if not path.is_absolute() or path.is_symlink():
        raise _failure("DEVELOPMENT_SOURCE_INVALID", "Choose an absolute, non-symlink local executable.", 2)
    try:
        metadata = path.stat()
        valid = stat.S_ISREG(metadata.st_mode) and os.access(path, os.X_OK)
        if hasattr(os, "getuid"):
            valid = valid and metadata.st_uid == os.getuid() and not metadata.st_mode & 0o022
    except OSError:
        valid = False
    if not valid:
        raise _failure("DEVELOPMENT_SOURCE_INVALID", "The local executable must be owned by you and not writable by other users.", 2)
    return str(path)


def _decode_result(stdout: str, action: str, exit_code: int) -> dict[str, Any]:
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    try:
        value = json.loads(stdout, object_pairs_hook=unique_object)
    except (ValueError, RecursionError):
        value = None
    if not isinstance(value, dict) or value.keys() != _FIELDS:
        raise _failure("DEVELOPMENT_RESULT_INVALID", "The executable did not return the development contract.")
    targets = value["registered_browsers"]
    valid = (
        value["contract"] == DEVELOPMENT_CONTRACT
        and type(value["schema_version"]) is int and value["schema_version"] == 1
        and value["channel"] == "local-development"
        and value["action"] == action
        and isinstance(value["state"], str) and value["state"] in _STATES
        and value["native_host_name"] == DEVELOPMENT_HOST
        and value["extension_id"] == DEVELOPMENT_EXTENSION
        and type(value["exit_code"]) is int and value["exit_code"] == exit_code
        and exit_code in {0, 2, 3, 4, 5, 6, 7}
        and type(value["already_current"]) is bool
        and type(value["mutation_allowed"]) is bool
        and isinstance(value["reason_code"], str)
        and re.fullmatch(r"[A-Z][A-Z0-9_]{0,95}", value["reason_code"]) is not None
        and isinstance(value["companion_version"], str)
        and re.fullmatch(r"[0-9]{1,6}\.[0-9]{1,6}\.[0-9]{1,6}", value["companion_version"]) is not None
        and isinstance(targets, list) and len(targets) <= len(BROWSER_CHOICES) - 1
        and all(isinstance(item, str) and item in BROWSER_CHOICES[1:] for item in targets)
        and len(targets) == len(set(targets))
        and type(value["registration_count"]) is int
        and 0 <= value["registration_count"] <= len(targets)
    )
    if not valid or (exit_code == 0 and value["state"] != ("not_installed" if action == "uninstall" else "installed")):
        raise _failure("DEVELOPMENT_RESULT_INVALID", "The development result has invalid identity or status fields.")
    return value


def run_development(
    *, action: str, source_binary: str, browsers: Sequence[str] = (),
    yes: bool = False, json_output: bool = False,
    stdout: TextIO | None = None, stderr: TextIO | None = None,
) -> int:
    output, errors = stdout or sys.stdout, stderr or sys.stderr
    options = BrowserExtensionOptions(command=f"development:{action}", json_output=json_output)
    try:
        if action not in {"install", "update", "status", "uninstall"}:
            raise _failure("UNKNOWN_COMMAND", "Unknown development command.", 2)
        if action != "status" and not yes:
            raise _failure("DEVELOPMENT_CONFIRMATION_REQUIRED", "Use --yes to trust this local build and confirm development-only changes.", 4)
        if action != "install" and browsers:
            raise _failure("UNEXPECTED_ARGUMENT", "Browser targets are accepted only for development installation.", 2)
        targets = _normalize_browsers(browsers) if action == "install" else ()
        executable = _explicit_source(source_binary)
        argv = [executable, "development", action, "--json"]
        for browser in targets:
            argv.extend(("--browser", browser))
        if action != "status":
            argv.append("--yes")
        completed = _run_process(argv, stdin_payload=None, timeout_seconds=60)
        if completed.overflow_stream or completed.timed_out:
            raise _failure("DEVELOPMENT_OUTCOME_UNKNOWN", "The development command was stopped; inspect owned state before retrying.")
        result = _decode_result(completed.stdout, action, completed.returncode)
        if result["reason_code"] == "DEVELOPMENT_UNINSTALLED_CREDENTIAL_CLEANUP_PENDING":
            message = (
                "Development registration removed; local keys may remain. Revoke development "
                "credentials in Browser settings. Chrome tabs were not closed."
            )
        elif result["reason_code"] == "DEVELOPMENT_UPDATE_RECOVERY_REQUIRED":
            message = (
                "A development update needs recovery. Retry development update with the "
                "same trusted source build and --yes; do not uninstall or delete its journal. "
                "Foreign or changed files require inspection before recovery."
            )
        elif completed.returncode != 0:
            message = (
                f"Development command needs attention ({result['reason_code']}). "
                "Inspect development status before retrying."
            )
        elif action == "update":
            message = (
                ("Development companion is already current; " if result["already_current"]
                 else "Development companion updated; ")
                + "existing pairing and browser registrations "
                "were preserved. Reconnect the extension to use the selected build. "
                "This is not a signed production release."
            )
        else:
            message = f"Development companion: {result['state']}. This is not a signed production release."
        payload = _result_payload(
            options=options, exit_code=completed.returncode, code=result["reason_code"],
            message=message,
            result=result,
        )
    except OSError:
        payload = _result_payload(options=options, exit_code=3, code="DEVELOPMENT_SOURCE_UNAVAILABLE", message="The selected local executable could not be started.")
    except _BrowserExtensionFailure as exc:
        payload = _result_payload(options=options, exit_code=exc.exit_code, code=exc.code, message=exc.message)
    _emit_result(payload, json_output=json_output, stdout=output, stderr=errors)
    return int(payload["exit_code"])
