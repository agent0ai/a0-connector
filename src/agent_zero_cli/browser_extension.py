"""A0 CLI orchestration for the standalone browser bridge companion.

The Python CLI deliberately does not implement native-host installation or
browser registration itself.  Those security-sensitive operations belong to
the signed ``a0-browser-bridge`` companion and its release catalog.
"""

from __future__ import annotations

import asyncio
from contextlib import ExitStack
from dataclasses import dataclass
import ipaddress
import json
import os
import re
import subprocess
import sys
import threading
import time
from typing import Any, BinaryIO, Mapping, Sequence, TextIO
from urllib.parse import urlsplit, urlunsplit


COMPANION_EXECUTABLE = "a0-browser-bridge"
RESULT_CONTRACT = "a0.browser-extension.cli.v1"
RESULT_SCHEMA_VERSION = 1
FOUNDATION_FEATURE = "browser_extension_bridge_foundation"

BROWSER_CHOICES = (
    "auto",
    "chrome",
    "edge",
    "brave",
    "vivaldi",
    "opera",
    "chromium",
)

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_NOT_INSTALLED = 3
EXIT_ACTION_REQUIRED = 4
EXIT_INTEGRITY = 5
EXIT_PARTIAL = 6
EXIT_UNAVAILABLE = 7
_STABLE_EXIT_CODES = {
    EXIT_OK,
    EXIT_USAGE,
    EXIT_NOT_INSTALLED,
    EXIT_ACTION_REQUIRED,
    EXIT_INTEGRITY,
    EXIT_PARTIAL,
    EXIT_UNAVAILABLE,
}
_BROWSER_COMMANDS = {"install", "repair"}
_HOST_STATUS_COMMANDS = {"install", "status", "doctor"}
_KNOWN_COMMANDS = {
    "install",
    "status",
    "doctor",
    "pair",
    "repair",
    "update",
    "uninstall",
}
_COMPANION_JSON_CONTRACTS = {
    "install": "a0.browser-bridge.install-plan.v1",
    "status": "a0.browser-bridge.status.v1",
    "doctor": "a0.browser-bridge.status.v1",
    "update": "a0.browser-bridge.install-plan.v1",
    "repair": "a0.browser-bridge.lifecycle.v1",
    "uninstall": "a0.browser-bridge.lifecycle.v1",
}
_COMPANION_ERROR_CONTRACT = "a0.browser-bridge.cli-error.v1"
_MAX_COMPANION_JSON_BYTES = 1024 * 1024
_MAX_COMPANION_STDOUT_BYTES = 1024 * 1024
_MAX_COMPANION_STDERR_BYTES = 64 * 1024
_PAIRING_STDIN_MAX_BYTES = 64 * 1024
_PIPE_READ_BYTES = 16 * 1024
_COMPANION_ENV_ALLOWLIST = (
    "PATH",
    "HOME",
    "LOCALAPPDATA",
    "XDG_DATA_HOME",
    "XDG_CONFIG_HOME",
    "XDG_RUNTIME_DIR",
    "DBUS_SESSION_BUS_ADDRESS",
    "LANG",
    "LANGUAGE",
    "LC_ALL",
    "LC_CTYPE",
    "LC_MESSAGES",
    "TMPDIR",
    "TMP",
    "TEMP",
    "SystemRoot",
    "WINDIR",
)
_FORBIDDEN_JSON_KEYS = {
    "body",
    "content",
    "cookie",
    "cookies",
    "credential",
    "credentials",
    "form",
    "form_data",
    "header",
    "headers",
    "page",
    "page_content",
    "pairing_code",
    "password",
    "path",
    "private_key",
    "raw",
    "raw_manifest",
    "script",
    "secret",
    "selector",
    "signature",
    "token",
    "url",
    "username",
}
_SAFE_DIAGNOSTIC_KEYS = {
    "platform_signature",
}
_PAIRING_CODE_RE = re.compile(r"\bA0B1-[A-Z0-9-]+\b", re.IGNORECASE)
_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_WINDOWS_PATH_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:\\[^\r\n\t\"']+")
_UNIX_PATH_RE = re.compile(r"(?<![A-Za-z0-9])(?:~|/)[^\r\n\t\"']+")


@dataclass(frozen=True)
class BrowserExtensionOptions:
    """Normalized options for one browser-extension subcommand."""

    command: str
    host: str = ""
    browsers: tuple[str, ...] = ()
    json_output: bool = False
    yes: bool = False
    force_local: bool = False
    keep_logs: bool = False


@dataclass(frozen=True)
class _ServerProbe:
    state: str
    reason_code: str
    authentication: str = "not_checked"
    foundation: Mapping[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "state": self.state,
            "reason_code": self.reason_code,
            "authentication": self.authentication,
        }
        if self.foundation:
            result["foundation"] = dict(self.foundation)
        return result


@dataclass(frozen=True)
class _CompanionProcessResult:
    returncode: int
    stdout: str
    stderr: str
    overflow_stream: str = ""
    timed_out: bool = False


class _BrowserExtensionFailure(RuntimeError):
    def __init__(self, exit_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.code = code
        self.message = message


def _result_payload(
    *,
    options: BrowserExtensionOptions,
    exit_code: int,
    code: str,
    message: str,
    result: Mapping[str, Any] | None = None,
    server: _ServerProbe | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": RESULT_SCHEMA_VERSION,
        "contract": RESULT_CONTRACT,
        "command": options.command,
        "ok": exit_code == EXIT_OK,
        "code": code,
        "message": _sanitize_text(message),
        "exit_code": exit_code,
    }
    if result:
        sanitized = _sanitize_json_value(dict(result))
        if isinstance(sanitized, dict) and sanitized:
            payload["result"] = sanitized
    if server is not None:
        payload["server"] = server.as_dict()
    return payload


def _emit_result(
    payload: Mapping[str, Any],
    *,
    json_output: bool,
    stdout: TextIO,
    stderr: TextIO,
) -> None:
    if json_output:
        stdout.write(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
        stdout.flush()
        return

    message = str(payload.get("message") or "Browser extension command failed.")
    stream = stdout if bool(payload.get("ok")) else stderr
    stream.write(message + "\n")
    stream.flush()


def _normalized_key(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value).strip().lower()).strip("_")


def _forbidden_json_key(key: object) -> bool:
    normalized = _normalized_key(key)
    if normalized in _SAFE_DIAGNOSTIC_KEYS:
        return False
    if normalized in _FORBIDDEN_JSON_KEYS:
        return True
    return any(normalized.endswith(f"_{suffix}") for suffix in _FORBIDDEN_JSON_KEYS)


def _safe_origin(match: re.Match[str]) -> str:
    value = match.group(0).rstrip(".,;:!?)]}")
    suffix = match.group(0)[len(value) :]
    try:
        parsed = urlsplit(value)
    except ValueError:
        return "[redacted-url]" + suffix
    if not parsed.scheme or not parsed.hostname or parsed.username or parsed.password:
        return "[redacted-url]" + suffix
    try:
        netloc = parsed.hostname
        if ":" in netloc and not netloc.startswith("["):
            netloc = f"[{netloc}]"
        if parsed.port is not None:
            netloc = f"{netloc}:{parsed.port}"
    except ValueError:
        return "[redacted-url]" + suffix
    return urlunsplit((parsed.scheme.lower(), netloc, "", "", "")) + suffix


def _sanitize_text(value: object) -> str:
    text = str(value or "")
    text = _PAIRING_CODE_RE.sub("[redacted-pairing-code]", text)
    origins: list[str] = []

    def hold_origin(match: re.Match[str]) -> str:
        origins.append(_safe_origin(match))
        return f"[a0-safe-origin-{len(origins) - 1}]"

    text = _URL_RE.sub(hold_origin, text)
    text = _WINDOWS_PATH_RE.sub("[redacted-path]", text)
    text = _UNIX_PATH_RE.sub("[redacted-path]", text)
    for index, origin in enumerate(origins):
        text = text.replace(f"[a0-safe-origin-{index}]", origin)
    return text[:8192]


def _sanitize_json_value(value: Any, *, depth: int = 0) -> Any:
    if depth >= 8:
        return "[redacted-depth]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _sanitize_text(value)
    if isinstance(value, Mapping):
        sanitized: dict[str, Any] = {}
        for index, (key, nested) in enumerate(value.items()):
            if index >= 100 or _forbidden_json_key(key):
                continue
            sanitized[str(key)] = _sanitize_json_value(nested, depth=depth + 1)
        return sanitized
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_sanitize_json_value(item, depth=depth + 1) for item in list(value)[:100]]
    return _sanitize_text(value)


def _normalize_browsers(values: Sequence[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    for value in values or ("auto",):
        browser = str(value or "").strip().lower()
        if browser not in BROWSER_CHOICES:
            raise _BrowserExtensionFailure(
                EXIT_USAGE,
                "UNSUPPORTED_BROWSER",
                f"Unsupported browser {browser or '<empty>'!r}.",
            )
        if browser not in normalized:
            normalized.append(browser)
    if "auto" in normalized and len(normalized) > 1:
        raise _BrowserExtensionFailure(
            EXIT_USAGE,
            "AMBIGUOUS_BROWSER_SELECTION",
            "Use --browser auto by itself, or repeat --browser with explicit browser names.",
        )
    return tuple(normalized or ("auto",))


def _companion_environment() -> dict[str, str]:
    """Pass only variables required for user paths, locale, and credential APIs."""

    return {
        key: os.environ[key]
        for key in _COMPANION_ENV_ALLOWLIST
        if key in os.environ
    }


def _resolve_companion_executable() -> str | None:
    """Resolve exact compiled release pins against owned native install state."""
    from .browser_extension_release import CompanionDiscoveryError, resolve_installed_companion

    try:
        return resolve_installed_companion()
    except CompanionDiscoveryError as error:
        raise _BrowserExtensionFailure(
            EXIT_INTEGRITY, error.code,
            "The browser companion installation could not be verified. "
            "No program was started; repair the installation using its trusted installer.",
        ) from None


def _normalize_server_host(host: str, *, pairing: bool) -> str:
    candidate = str(host or "").strip().rstrip("/")
    if not candidate:
        raise _BrowserExtensionFailure(
            EXIT_ACTION_REQUIRED,
            "HOST_REQUIRED",
            "An Agent Zero host is required for pairing.",
        )
    try:
        parsed = urlsplit(candidate)
    except ValueError as exc:
        raise _BrowserExtensionFailure(EXIT_USAGE, "INVALID_HOST", "The Agent Zero host URL is invalid.") from exc
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise _BrowserExtensionFailure(
            EXIT_USAGE,
            "INVALID_HOST",
            "Use an HTTP(S) Agent Zero base URL without credentials, a query, or a fragment.",
        )
    if pairing and parsed.scheme.lower() != "https" and not _is_loopback_host(parsed.hostname):
        raise _BrowserExtensionFailure(
            EXIT_INTEGRITY,
            "INSECURE_PAIRING_HOST",
            "Pairing requires HTTPS unless Agent Zero is running on this computer.",
        )
    return candidate


def _is_loopback_host(hostname: str) -> bool:
    normalized = hostname.rstrip(".").lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


async def _close_client(client: Any) -> None:
    try:
        await client.disconnect(close_http=True, notify=False)
    except Exception:
        try:
            await client.http.aclose()
        except Exception:
            pass


def _remote_reason_code(value: object) -> str:
    candidate = str(value or "").strip().lower()
    if re.fullmatch(r"[a-z0-9_.-]{1,96}", candidate):
        return candidate
    return "remote_status_invalid"


def _remote_foundation_projection(status: Mapping[str, Any]) -> dict[str, Any] | None:
    if (
        status.get("scope") != "extension_bridge_foundation"
        or status.get("foundation_contract") != "a0.browser-bridge.foundation.v1"
        or status.get("status_contract") != "a0.browser-bridge.status.v1"
    ):
        return None

    gate = status.get("gate")
    selection = status.get("selection")
    layers = status.get("layers")
    server_layer = layers.get("server") if isinstance(layers, Mapping) else None
    if not isinstance(gate, Mapping) or not isinstance(selection, Mapping) or not isinstance(server_layer, Mapping):
        return None
    if gate.get("gate_contract") != "a0.browser-bridge.rollout.v1":
        return None

    rollout_state = str(gate.get("state") or "")
    selection_state = str(selection.get("state") or "")
    server_state = str(server_layer.get("state") or "")
    if rollout_state not in {"disabled", "preview", "available"}:
        return None
    if selection_state not in {"not_checked", "blocked", "selected", "configured", "not_selected"}:
        return None
    if server_state not in {"not_checked", "blocked", "configured", "unknown", "healthy"}:
        return None

    return {
        "foundation_contract": "a0.browser-bridge.foundation.v1",
        "status_contract": "a0.browser-bridge.status.v1",
        "rollout_state": rollout_state,
        "rollout_reason_code": _remote_reason_code(gate.get("reason_code")),
        "selection_state": selection_state,
        "selection_reason_code": _remote_reason_code(selection.get("reason_code")),
        "server_layer_state": server_state,
        "server_layer_reason_code": _remote_reason_code(server_layer.get("reason_code")),
    }


async def _collect_server_status(host: str, *, remember_session: bool) -> _ServerProbe:
    normalized_host = _normalize_server_host(host, pairing=False)
    from agent_zero_cli.client import A0Client

    client = A0Client(normalized_host)
    try:
        try:
            capabilities = await client.fetch_capabilities()
        except Exception:
            return _ServerProbe("not_checked", "server_unavailable", "unavailable")

        features_value = capabilities.get("features") if isinstance(capabilities, Mapping) else None
        features = {str(value) for value in features_value or () if isinstance(value, str)}
        if FOUNDATION_FEATURE not in features:
            return _ServerProbe("not_checked", "server_upgrade_required", "not_checked")

        try:
            client.restore_session(normalized_host)
        except Exception:
            pass
        try:
            authenticated = await client.verify_session()
        except Exception:
            return _ServerProbe("not_checked", "session_verification_unavailable", "unavailable")

        if not authenticated:
            username = os.environ.get("A0_USERNAME", "").strip()
            password = os.environ.get("A0_PASSWORD", "")
            if username and password:
                try:
                    authenticated = await client.login(username, password)
                except Exception:
                    authenticated = False
        if not authenticated:
            return _ServerProbe("not_checked", "authentication_required", "required")
        if remember_session:
            try:
                client.persist_session(normalized_host)
            except Exception:
                pass

        try:
            remote_status = await client.fetch_browser_extension_status()
        except Exception:
            return _ServerProbe("not_checked", "remote_status_unavailable", "authenticated")
        foundation = _remote_foundation_projection(remote_status)
        if foundation is None:
            return _ServerProbe("not_checked", "remote_status_invalid", "authenticated")
        return _ServerProbe(
            "checked",
            "remote_status_checked",
            "authenticated",
            foundation,
        )
    finally:
        await _close_client(client)


async def _create_pairing_bundle(host: str, *, remember_session: bool) -> dict[str, Any]:
    from .client import A0Client
    from . import browser_extension_release as releases

    normalized = _normalize_server_host(host, pairing=True)
    allowed_ids = {origin.removeprefix("chrome-extension://").removesuffix("/")
                   for pin in releases.APPROVED_COMPANION_RELEASES
                   if (pin.platform, pin.artifact_arch) == releases._host_target() and releases._pin_valid(pin)
                   for origin in pin.extension_origins}
    if not allowed_ids:
        raise _BrowserExtensionFailure(EXIT_UNAVAILABLE, "PAIRING_RELEASE_UNAVAILABLE", "The approved browser extension identity is unavailable.")
    client = A0Client(normalized)
    try:
        try:
            client.restore_session(normalized)
        except Exception:
            pass
        authenticated = await client.verify_session()
        if not authenticated:
            username, password = os.environ.get("A0_USERNAME", "").strip(), os.environ.get("A0_PASSWORD", "")
            if username and password:
                authenticated = await client.login(username, password)
        if not authenticated:
            raise _BrowserExtensionFailure(EXIT_ACTION_REQUIRED, "PAIRING_AUTHENTICATION_REQUIRED", "Sign in to this Agent Zero instance, then run pair again.")
        capabilities = await client.fetch_capabilities()
        features = capabilities.get("features", [])
        if not isinstance(features, list) or "browser_bridge_pairing_v1" not in features:
            raise _BrowserExtensionFailure(EXIT_UNAVAILABLE, "PAIRING_CONTRACT_UNAVAILABLE", "Update Agent Zero to enable browser pairing.")
        if remember_session:
            try:
                client.persist_session(normalized)
            except Exception:
                pass
        # One authenticated CSRF-protected creation only. Never retry a secret-
        # producing POST after an ambiguous response, and never follow redirects.
        body = bytearray()
        async with client.http.stream(
            "POST", f"{normalized}/api/plugins/_a0_connector/browser_bridge_pairing",
            json={"action": "create", "display_name": "My browser"},
            headers=await client._csrf_headers(), follow_redirects=False,
        ) as response:
            if response.status_code != 201:
                raise _BrowserExtensionFailure(EXIT_ACTION_REQUIRED, "PAIRING_NOT_AVAILABLE", "Enable browser pairing in Agent Zero Browser settings, then try again.")
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > 8192:
                    raise ValueError()
        bundle = releases._json(bytes(body))
        required = {"contract", "trust_version", "state", "pairing_id", "pairing_code", "server", "extension_id", "display_name", "created_at_ms", "expires_at_ms", "expires_in_seconds", "native_runtime_location", "docker_install_target", "connector_session_ready", "browser_control_ready"}
        server = bundle.get("server")
        now = int(time.time() * 1000)
        if (set(bundle) != required or bundle["contract"] != "a0.browser-bridge.trust.v1"
                or type(bundle["trust_version"]) is not int or bundle["trust_version"] != 1
                or bundle["state"] != "pairing_pending" or bundle["extension_id"] not in allowed_ids
                or bundle["display_name"] != "My browser"
                or not isinstance(bundle["pairing_id"], str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,512}", bundle["pairing_id"])
                or not isinstance(bundle["pairing_code"], str) or not re.fullmatch(r"A0B1-[0-9A-F]{8}-[0-9A-HJKMNP-TV-Z]{32}", bundle["pairing_code"])
                or not isinstance(server, dict) or set(server) != {"base_url", "instance_fingerprint"}
                or server["base_url"] != normalized
                or not isinstance(server["instance_fingerprint"], str) or not re.fullmatch(r"sha256:[a-f0-9]{20}", server["instance_fingerprint"])
                or type(bundle["created_at_ms"]) is not int or type(bundle["expires_at_ms"]) is not int
                or not now - 30000 <= bundle["created_at_ms"] <= now + 30000
                or bundle["expires_at_ms"] - bundle["created_at_ms"] != 300000
                or bundle["expires_at_ms"] <= now
                or type(bundle["expires_in_seconds"]) is not int or bundle["expires_in_seconds"] != 300
                or bundle["native_runtime_location"] != "user_browser_host"
                or any(bundle[key] is not False for key in ("docker_install_target", "connector_session_ready", "browser_control_ready"))):
            raise ValueError()
        return bundle
    except _BrowserExtensionFailure:
        raise
    except Exception:
        raise _BrowserExtensionFailure(EXIT_UNAVAILABLE, "PAIRING_REQUEST_FAILED", "Pairing could not be prepared. Check Agent Zero Browser settings before trying again.") from None
    finally:
        await _close_client(client)


def _companion_argv(options: BrowserExtensionOptions, executable: str, browsers: Sequence[str]) -> list[str]:
    argv = [executable, options.command]
    if options.command in _BROWSER_COMMANDS:
        for browser in browsers:
            argv.extend(("--browser", browser))
    if options.command == "pair":
        raise _BrowserExtensionFailure(EXIT_ACTION_REQUIRED, "PAIRING_REQUIRES_BROWSER_OPTIONS", "Complete pairing in Chrome Options; the CLI cannot impersonate its profile.")
    if options.command == "uninstall":
        if options.yes:
            argv.append("--yes")
        if options.force_local:
            argv.append("--force-local")
        if options.keep_logs:
            argv.append("--keep-logs")
    if options.json_output:
        argv.append("--json")
    return argv


def _parse_companion_json(command: str, returncode: int, stdout: str) -> dict[str, Any]:
    if len(stdout) > _MAX_COMPANION_JSON_BYTES:
        raise _BrowserExtensionFailure(
            EXIT_INTEGRITY,
            "COMPANION_OUTPUT_TOO_LARGE",
            "The browser companion returned an oversized machine-readable result.",
        )
    try:
        output_size = len(stdout.encode("utf-8"))
    except UnicodeError as exc:
        raise _BrowserExtensionFailure(
            EXIT_INTEGRITY,
            "COMPANION_OUTPUT_INVALID",
            "The browser companion returned invalid machine-readable output.",
        ) from exc
    if output_size > _MAX_COMPANION_JSON_BYTES:
        raise _BrowserExtensionFailure(
            EXIT_INTEGRITY,
            "COMPANION_OUTPUT_TOO_LARGE",
            "The browser companion returned an oversized machine-readable result.",
        )
    try:
        payload = json.loads(stdout)
    except Exception as exc:
        raise _BrowserExtensionFailure(
            EXIT_INTEGRITY,
            "COMPANION_OUTPUT_INVALID",
            "The browser companion returned invalid machine-readable output.",
        ) from exc
    schema_version = payload.get("schema_version") if isinstance(payload, dict) else None
    contract = payload.get("contract") if isinstance(payload, dict) else None
    expected_contract = _COMPANION_JSON_CONTRACTS.get(command)
    contract_valid = contract == expected_contract
    if returncode == EXIT_USAGE:
        contract_valid = contract == _COMPANION_ERROR_CONTRACT
    if (
        not isinstance(payload, dict)
        or schema_version != 1
        or isinstance(schema_version, bool)
        or not contract_valid
        or not _companion_payload_matches_schema(command, returncode, payload)
    ):
        raise _BrowserExtensionFailure(
            EXIT_INTEGRITY,
            "COMPANION_OUTPUT_INVALID",
            "The browser companion returned an unsupported machine-readable result.",
        )
    return payload


def _is_nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _companion_payload_matches_schema(
    command: str,
    returncode: int,
    payload: Mapping[str, Any],
) -> bool:
    if returncode == EXIT_USAGE:
        return (
            set(payload) == {"contract", "schema_version", "state", "reason_code"}
            and payload.get("state") == "error"
            and isinstance(payload.get("reason_code"), str)
            and bool(str(payload.get("reason_code") or ""))
        )

    if command in {"repair", "uninstall"}:
        if (set(payload) != {"contract", "schema_version", "companion_version", "operation", "state", "reason_code", "registration_count", "credential_cleanup", "disposition"}
                or payload.get("operation") != command
                or not isinstance(payload.get("companion_version"), str)
                or not re.fullmatch(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", payload["companion_version"])
                or not isinstance(payload.get("reason_code"), str)
                or not re.fullmatch(r"[A-Z][A-Z0-9_]{1,127}", payload["reason_code"])
                or not _is_nonnegative_int(payload.get("registration_count"))
                or payload["registration_count"] > 6):
            return False
        state = (payload.get("state"), payload.get("credential_cleanup"), payload.get("disposition"))
        if state == ("blocked", "not_attempted", "unchanged"):
            return returncode in {EXIT_NOT_INSTALLED, EXIT_INTEGRITY, EXIT_UNAVAILABLE}
        if command == "repair":
            if (returncode == 6 and state == ("blocked", "not_attempted", "recovery_required")
                    and payload["reason_code"] == "REPAIR_RECOVERY_REQUIRED"
                    and payload["registration_count"] == 0):
                return True
            return (returncode == 0 and payload["reason_code"] == "REPAIR_VERIFIED"
                    and state == ("repaired", "not_attempted", "repaired")
                    and payload["registration_count"] > 0)
        if state == ("action_required", "not_attempted", "unchanged"):
            return returncode == EXIT_ACTION_REQUIRED and payload["reason_code"] == "LOCAL_RETIREMENT_CONFIRMATION_REQUIRED"
        if returncode != 6:
            return False
        return ((state == ("cleanup_pending", "pending", "registrations_retired_recoverable")
                 and payload["reason_code"] == "CREDENTIAL_CLEANUP_PENDING")
                or (state == ("cleanup_pending", "pending", "unchanged")
                    and payload["reason_code"] == "PROFILE_REVOCATION_REQUIRED"
                    and payload["registration_count"] > 0)
                or (state == ("cleanup_pending", "pending", "recovery_required")
                    and payload["reason_code"] == "LOCAL_RETIREMENT_RECOVERY_REQUIRED"
                    and payload["registration_count"] == 0))

    if command in {"status", "doctor"}:
        expected_keys = {
            "contract",
            "schema_version",
            "companion_version",
            "state",
            "reason_code",
            "platform",
            "architecture",
            "install_root",
            "release_trust",
            "native_host",
            "registered_browser_count",
        }
        return (
            set(payload) == expected_keys
            and payload.get("state") in {"not_installed", "unknown", "blocked", "installed"}
            and isinstance(payload.get("companion_version"), str)
            and bool(str(payload.get("companion_version") or ""))
            and isinstance(payload.get("reason_code"), str)
            and bool(str(payload.get("reason_code") or ""))
            and isinstance(payload.get("platform"), str)
            and isinstance(payload.get("architecture"), str)
            and payload.get("install_root") in {"resolved", "unavailable"}
            and payload.get("release_trust") in {"configured", "not_configured"}
            and payload.get("native_host") in {"enabled", "disabled"}
            and _is_nonnegative_int(payload.get("registered_browser_count"))
        )

    if command in {"install", "update"}:
        expected_keys = {
            "contract",
            "schema_version",
            "companion_version",
            "install_contract",
            "operation",
            "state",
            "reason_code",
            "mutation_allowed",
            "catalog",
            "artifact",
            "platform_signature",
            "platform",
            "architecture",
            "install_root",
            "target_browsers",
            "registration_count",
            "rollback",
        }
        targets = payload.get("target_browsers")
        return (
            set(payload) == expected_keys
            and isinstance(payload.get("companion_version"), str)
            and bool(str(payload.get("companion_version") or ""))
            and payload.get("install_contract") == "a0.browser-bridge.install.v1"
            and payload.get("operation") == command
            and payload.get("state") in {"blocked", "ready", "installed"}
            and isinstance(payload.get("reason_code"), str)
            and bool(str(payload.get("reason_code") or ""))
            and isinstance(payload.get("mutation_allowed"), bool)
            and payload.get("catalog") in {"not_verified", "verified"}
            and payload.get("artifact") in {"not_verified", "verified"}
            and payload.get("platform_signature") in {"not_verified", "verified"}
            and isinstance(payload.get("platform"), str)
            and isinstance(payload.get("architecture"), str)
            and payload.get("install_root") in {"resolved", "unavailable"}
            and isinstance(targets, list)
            and bool(targets)
            and all(isinstance(target, str) and target in BROWSER_CHOICES for target in targets)
            and len(targets) == len(set(targets))
            and _is_nonnegative_int(payload.get("registration_count"))
            and payload.get("rollback") in {"not_started", "available", "not_completed", "not_needed", "cleanup_pending"}
            and (
                payload.get("state") != "installed"
                or (
                    payload.get("reason_code") == "INSTALL_VERIFIED"
                    and payload.get("mutation_allowed") is True
                    and all(payload.get(field) == "verified" for field in ("catalog", "artifact", "platform_signature"))
                    and payload.get("registration_count", 0) > 0
                    and payload.get("rollback") in {"not_needed", "cleanup_pending"}
                )
            )
        )

    return False


def _run_process(
    argv: Sequence[str],
    *,
    stdin_payload: str | None,
    timeout_seconds: float | None = None,
    retained_descriptor: int | None = None,
) -> _CompanionProcessResult:
    stdin_bytes = stdin_payload.encode("utf-8") if stdin_payload is not None else None
    if stdin_bytes is not None and len(stdin_bytes) > _PAIRING_STDIN_MAX_BYTES:
        raise _BrowserExtensionFailure(
            EXIT_INTEGRITY,
            "PAIRING_BUNDLE_TOO_LARGE",
            "The pairing bundle exceeded its protected stdin limit.",
        )

    arguments = list(argv)
    process_options: dict[str, Any] = {}
    if retained_descriptor is not None:
        if not sys.platform.startswith("linux") or retained_descriptor < 0:
            raise _BrowserExtensionFailure(EXIT_INTEGRITY, "BOOTSTRAP_DESCRIPTOR_INVALID", "The bootstrap execution handle is unavailable.")
        arguments[0] = f"/proc/self/fd/{retained_descriptor}"
        process_options["pass_fds"] = (retained_descriptor,)
    process = subprocess.Popen(
        arguments,
        stdin=subprocess.PIPE if stdin_bytes is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_companion_environment(),
        **process_options,
    )
    if process.stdout is None or process.stderr is None:
        try:
            process.terminate()
        except OSError:
            pass
        raise OSError("Browser companion pipes are unavailable.")

    stdout_buffer = bytearray()
    stderr_buffer = bytearray()
    overflow = threading.Event()
    overflow_lock = threading.Lock()
    overflow_stream: list[str] = []

    def read_bounded(pipe: BinaryIO, buffer: bytearray, limit: int, stream_name: str) -> None:
        try:
            while True:
                chunk = pipe.read(_PIPE_READ_BYTES)
                if not chunk:
                    return
                remaining = max(0, limit - len(buffer))
                buffer.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    with overflow_lock:
                        if not overflow_stream:
                            overflow_stream.append(stream_name)
                    overflow.set()
                    return
        finally:
            try:
                pipe.close()
            except OSError:
                pass

    readers = (
        threading.Thread(
            target=read_bounded,
            args=(process.stdout, stdout_buffer, _MAX_COMPANION_STDOUT_BYTES, "stdout"),
            daemon=True,
        ),
        threading.Thread(
            target=read_bounded,
            args=(process.stderr, stderr_buffer, _MAX_COMPANION_STDERR_BYTES, "stderr"),
            daemon=True,
        ),
    )
    for reader in readers:
        reader.start()

    writer: threading.Thread | None = None
    if stdin_bytes is not None and process.stdin is not None:
        def write_stdin() -> None:
            try:
                process.stdin.write(stdin_bytes)
                process.stdin.flush()
            except (BrokenPipeError, OSError):
                pass
            finally:
                try:
                    process.stdin.close()
                except OSError:
                    pass

        writer = threading.Thread(target=write_stdin, daemon=True)
        writer.start()

    deadline = time.monotonic() + timeout_seconds if timeout_seconds is not None else None
    timed_out = False
    while process.poll() is None:
        timed_out = deadline is not None and time.monotonic() >= deadline
        if not overflow.wait(0.05) and not timed_out:
            continue
        try:
            process.terminate()
        except OSError:
            pass
        try:
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except OSError:
                pass
        break

    returncode = process.wait()
    for reader in readers:
        reader.join(timeout=1.0)
    if writer is not None:
        writer.join(timeout=1.0)

    # Interrupted output may end halfway through a UTF-8 scalar. Preserve the
    # stop reason before decoding so mutation uncertainty cannot be masked by
    # an incidental encoding error.
    if timed_out or overflow_stream:
        return _CompanionProcessResult(
            returncode=returncode,
            stdout="",
            stderr="",
            overflow_stream=overflow_stream[0] if overflow_stream else "",
            timed_out=timed_out,
        )

    try:
        decoded_stdout = bytes(stdout_buffer).decode("utf-8")
        decoded_stderr = bytes(stderr_buffer).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _BrowserExtensionFailure(
            EXIT_INTEGRITY,
            "COMPANION_OUTPUT_ENCODING_INVALID",
            "The browser companion returned non-UTF-8 output.",
        ) from exc

    return _CompanionProcessResult(
        returncode=returncode,
        stdout=decoded_stdout,
        stderr=decoded_stderr,
        overflow_stream=overflow_stream[0] if overflow_stream else "",
        timed_out=timed_out,
    )


def run_browser_extension(
    options: BrowserExtensionOptions,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Run one browser-extension command and return its stable exit code."""

    output = stdout or sys.stdout
    errors = stderr or sys.stderr
    server_probe: _ServerProbe | None = None
    resolved_host = ""
    remember_session = False
    resources = ExitStack()

    try:
        if options.command not in _KNOWN_COMMANDS:
            raise _BrowserExtensionFailure(EXIT_USAGE, "UNKNOWN_COMMAND", "Unknown browser-extension command.")
        browsers = _normalize_browsers(options.browsers) if options.command in _BROWSER_COMMANDS else ()
        if options.command in _HOST_STATUS_COMMANDS | {"pair"}:
            from agent_zero_cli.client import DEFAULT_HOST
            from agent_zero_cli.config import load_config

            config = load_config()
            resolved_host = (
                str(options.host or "").strip()
                or str(config.instance_url or "").strip()
                or DEFAULT_HOST
            )
            remember_session = bool(config.remember_host)
            resolved_host = _normalize_server_host(
                resolved_host,
                pairing=options.command == "pair",
            )
            server_probe = asyncio.run(
                _collect_server_status(
                    resolved_host,
                    remember_session=remember_session,
                )
            )
            if options.command == "install" and server_probe.reason_code == "server_upgrade_required":
                raise _BrowserExtensionFailure(
                    EXIT_UNAVAILABLE,
                    "SERVER_UPGRADE_REQUIRED",
                    "This Agent Zero server does not support the browser bridge foundation; "
                    "no installation changes were made.",
                )

        try:
            executable = _resolve_companion_executable()
        except _BrowserExtensionFailure:
            if options.command != "repair":
                raise
            # A damaged registration must not launch its unverified executable.
            # Only an independently pinned fresh bootstrap may inspect/repair it.
            executable = None
        bootstrap = None
        if options.command == "update" or (not executable and options.command in {"install", "repair"}):
            from .browser_extension_bootstrap import acquire_bootstrap
            bootstrap = resources.enter_context(acquire_bootstrap())
            if bootstrap is not None:
                executable = bootstrap.path
        if not executable:
            exit_code = EXIT_UNAVAILABLE if options.command in {"install", "repair"} else EXIT_NOT_INSTALLED
            code = "COMPANION_CATALOG_UNAVAILABLE" if exit_code == EXIT_UNAVAILABLE else "COMPANION_NOT_INSTALLED"
            message = (
                "The signed browser companion/catalog is unavailable; no installation changes were made."
                if exit_code == EXIT_UNAVAILABLE
                else "The Agent Zero browser companion is not installed."
            )
            raise _BrowserExtensionFailure(exit_code, code, message)

        stdin_payload: str | None = None
        if options.command == "pair":
            if options.json_output or not getattr(output, "isatty", lambda: False)():
                raise _BrowserExtensionFailure(EXIT_ACTION_REQUIRED, "PAIRING_REQUIRES_TERMINAL", "Run a0 browser-extension pair in an interactive terminal, or use Browser settings. No pairing code was created.")
            bundle = asyncio.run(
                _create_pairing_bundle(
                    resolved_host,
                    remember_session=remember_session,
                )
            )
            # Explicit human-only reveal. Chrome owns profile/install identity
            # and performs the exchange; no unsupported native CLI pair call.
            output.write(
                "One-time browser setup (expires in five minutes):\n"
                f"1. Open chrome-extension://{bundle['extension_id']}/options.html\n"
                f"2. Enter Agent Zero address: {resolved_host}\n"
                f"3. Paste this code: {bundle['pairing_code']}\n"
                "4. Choose Connect. Your browser remembers this pairing and reconnects automatically.\n"
                "Keep this code private. Pairing is not complete until Options confirms it.\n"
            )
            output.flush()
            return EXIT_ACTION_REQUIRED

        argv = _companion_argv(options, executable, browsers)
        try:
            if bootstrap is not None:
                bootstrap.verify()
                completed = _run_process(argv, stdin_payload=stdin_payload, timeout_seconds=600,
                                         retained_descriptor=bootstrap.descriptor if sys.platform.startswith("linux") else None)
                bootstrap.verify()
            else:
                completed = _run_process(argv, stdin_payload=stdin_payload)
        except OSError as exc:
            raise _BrowserExtensionFailure(
                EXIT_NOT_INSTALLED,
                "COMPANION_UNAVAILABLE",
                "The Agent Zero browser companion could not be started.",
            ) from exc

        if getattr(completed, "timed_out", False):
            raise _BrowserExtensionFailure(EXIT_PARTIAL, "COMPANION_INSTALL_TIMEOUT", "Installation timed out; its final state is unknown. Run browser-extension doctor before retrying.")
        overflow_stream = str(getattr(completed, "overflow_stream", "") or "")
        if overflow_stream:
            raise _BrowserExtensionFailure(
                EXIT_INTEGRITY,
                f"COMPANION_{overflow_stream.upper()}_TOO_LARGE",
                f"The browser companion exceeded its bounded {overflow_stream} limit and was stopped.",
            )
        if completed.returncode not in _STABLE_EXIT_CODES:
            raise _BrowserExtensionFailure(
                EXIT_INTEGRITY,
                "COMPANION_EXIT_INVALID",
                "The browser companion returned an unsupported exit status.",
            )

        companion_result: dict[str, Any] | None = None
        if options.json_output:
            companion_result = _parse_companion_json(options.command, completed.returncode, completed.stdout)
        elif options.command != "pair":
            if completed.stderr:
                safe_stderr = _sanitize_text(completed.stderr)
                errors.write(safe_stderr)
                if safe_stderr and not safe_stderr.endswith("\n"):
                    errors.write("\n")
            if completed.stdout:
                output.write(completed.stdout)
                if not completed.stdout.endswith("\n"):
                    output.write("\n")
            output.flush()
            errors.flush()

        exit_code = completed.returncode
        code = "OK" if exit_code == EXIT_OK else "COMPANION_REPORTED_FAILURE"
        message = (
            f"Browser extension {options.command} completed."
            if exit_code == EXIT_OK
            else f"Browser extension {options.command} did not complete."
        )
        payload = _result_payload(
            options=options,
            exit_code=exit_code,
            code=code,
            message=message,
            result=None if options.command == "pair" else companion_result,
            server=server_probe,
        )
        if options.json_output or options.command == "pair" or not (completed.stdout or completed.stderr):
            _emit_result(payload, json_output=options.json_output, stdout=output, stderr=errors)
        return exit_code
    except _BrowserExtensionFailure as exc:
        payload = _result_payload(
            options=options,
            exit_code=exc.exit_code,
            code=exc.code,
            message=exc.message,
            server=server_probe,
        )
        _emit_result(payload, json_output=options.json_output, stdout=output, stderr=errors)
        return exc.exit_code
    except Exception as exc:
        from .browser_extension_release import CompanionDiscoveryError
        if not isinstance(exc, CompanionDiscoveryError):
            raise
        exit_code = EXIT_UNAVAILABLE if exc.code == "COMPANION_BOOTSTRAP_UNAVAILABLE" else EXIT_INTEGRITY
        payload = _result_payload(options=options, exit_code=exit_code, code=exc.code,
                                  message="The release bootstrap could not be verified. No unchecked program was started.", server=server_probe)
        _emit_result(payload, json_output=options.json_output, stdout=output, stderr=errors)
        return exit_code
    finally:
        resources.close()
