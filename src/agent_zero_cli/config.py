import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_zero_cli.text_utils import coerce_bool

_ENV_DIR = Path.home() / ".agent-zero"
_ENV_FILE = _ENV_DIR / ".env"
_SESSION_FILE = _ENV_DIR / "session_cookies.json"
_LAST_CONTEXT_ID_KEY = "AGENT_ZERO_LAST_CONTEXT_ID"
_LAST_CONTEXT_HOST_KEY = "AGENT_ZERO_LAST_CONTEXT_HOST"
_DEFAULT_CONTEXT_ID_KEY = "AGENT_ZERO_DEFAULT_CONTEXT_ID"
_DEFAULT_CHAT_KEY = "A0_DEFAULT_CHAT"
_REMOTE_EXEC_ENABLED_KEY = "AGENT_ZERO_REMOTE_EXEC_ENABLED"
_A0_REMOTE_EXEC_KEY = "A0_REMOTE_EXEC"
_REMEMBER_HOST_KEY = "AGENT_ZERO_REMEMBER_HOST"
_COMPUTER_USE_ENABLED_KEY = "AGENT_ZERO_COMPUTER_USE_ENABLED"
_COMPUTER_USE_TRUST_MODE_KEY = "AGENT_ZERO_COMPUTER_USE_TRUST_MODE"
_COMPUTER_USE_RESTORE_TOKEN_KEY = "AGENT_ZERO_COMPUTER_USE_RESTORE_TOKEN"
_HOST_BROWSER_ENABLED_KEY = "AGENT_ZERO_HOST_BROWSER_ENABLED"
_HOST_BROWSER_FAMILY_KEY = "AGENT_ZERO_HOST_BROWSER_FAMILY"
_HOST_BROWSER_PROFILE_PATH_KEY = "AGENT_ZERO_HOST_BROWSER_PROFILE_PATH"
_HOST_BROWSER_PROFILE_LABEL_KEY = "AGENT_ZERO_HOST_BROWSER_PROFILE_LABEL"
_HOST_BROWSER_RELAUNCH_PREFERENCE_KEY = "AGENT_ZERO_HOST_BROWSER_RELAUNCH_PREFERENCE"
_DEFAULT_COMPUTER_USE_TRUST_MODE = "allow"
_VALID_COMPUTER_USE_TRUST_MODES = {"persistent", "allow"}
_VALID_HOST_BROWSER_RELAUNCH_PREFERENCES = {"ask", "manual"}


@dataclass
class CLIConfig:
    instance_url: str = ""
    last_context_id: str = ""
    last_context_host: str = ""
    default_context_id: str = ""
    remote_exec_enabled: bool = False
    remember_host: bool = False
    computer_use_enabled: bool = False
    computer_use_trust_mode: str = _DEFAULT_COMPUTER_USE_TRUST_MODE
    computer_use_restore_token: str = ""
    host_browser_enabled: bool = False
    host_browser_family: str = ""
    host_browser_profile_path: str = ""
    host_browser_profile_label: str = ""
    host_browser_relaunch_preference: str = "ask"


def _read_dotenv() -> dict[str, str]:
    """Read KEY=VALUE pairs from ~/.agent-zero/.env."""
    if not _ENV_FILE.exists():
        return {}
    values: dict[str, str] = {}
    for line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("\"'")
        if key:
            values[key] = value
    return values


def save_env(key: str, value: str) -> None:
    """Write or update a single KEY=VALUE pair in ~/.agent-zero/.env."""
    _ENV_DIR.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []
    replaced = False
    if _ENV_FILE.exists():
        for line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                existing_key = stripped.partition("=")[0].strip()
                if existing_key == key:
                    lines.append(f"{key}={value}")
                    replaced = True
                    continue
            lines.append(line)

    if not replaced:
        lines.append(f"{key}={value}")

    _ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def delete_env(key: str) -> None:
    """Remove a KEY from ~/.agent-zero/.env when present."""
    if not _ENV_FILE.exists():
        return

    lines: list[str] = []
    for line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            existing_key = stripped.partition("=")[0].strip()
            if existing_key == key:
                continue
        lines.append(line)

    _ENV_FILE.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def _normalize_saved_host(host: str) -> str:
    return str(host or "").strip().rstrip("/")


def _session_file_payload() -> dict[str, Any]:
    if not _SESSION_FILE.exists():
        return {}
    try:
        payload = json.loads(_SESSION_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_session_file(hosts: dict[str, list[dict[str, Any]]]) -> None:
    if not hosts:
        try:
            _SESSION_FILE.unlink()
        except FileNotFoundError:
            pass
        return

    _ENV_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "hosts": hosts}
    temp_file = _SESSION_FILE.with_suffix(f"{_SESSION_FILE.suffix}.tmp")
    temp_file.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.chmod(temp_file, 0o600)
    temp_file.replace(_SESSION_FILE)
    os.chmod(_SESSION_FILE, 0o600)


def save_remember_host(enabled: bool) -> None:
    if enabled:
        save_env(_REMEMBER_HOST_KEY, "1")
        return
    delete_env(_REMEMBER_HOST_KEY)


def load_persisted_session(host: str) -> list[dict[str, Any]]:
    normalized_host = _normalize_saved_host(host)
    if not normalized_host:
        return []

    payload = _session_file_payload()
    host_map = payload.get("hosts")
    if not isinstance(host_map, dict):
        return []

    raw_entries = host_map.get(normalized_host)
    if not isinstance(raw_entries, list):
        return []

    now = int(time.time())
    entries: list[dict[str, Any]] = []
    changed = False
    for raw_entry in raw_entries:
        if not isinstance(raw_entry, dict):
            changed = True
            continue

        name = str(raw_entry.get("name") or "").strip()
        domain = str(raw_entry.get("domain") or "").strip()
        if not name or not domain:
            changed = True
            continue

        expires_raw = raw_entry.get("expires")
        expires: int | None
        if expires_raw is None or expires_raw == "":
            expires = None
        else:
            try:
                expires = int(expires_raw)
            except (TypeError, ValueError):
                changed = True
                continue

        if expires is not None and expires <= now:
            changed = True
            continue

        entries.append(
            {
                "name": name,
                "value": str(raw_entry.get("value") or ""),
                "domain": domain,
                "path": str(raw_entry.get("path") or "/") or "/",
                "secure": bool(raw_entry.get("secure")),
                "expires": expires,
            }
        )

    if changed:
        if entries:
            save_persisted_session(normalized_host, entries)
        else:
            delete_persisted_session(normalized_host)

    return entries


def save_persisted_session(host: str, cookies: list[dict[str, Any]]) -> None:
    normalized_host = _normalize_saved_host(host)
    if not normalized_host:
        return

    sanitized: list[dict[str, Any]] = []
    now = int(time.time())
    for cookie in cookies:
        if not isinstance(cookie, dict):
            continue

        name = str(cookie.get("name") or "").strip()
        domain = str(cookie.get("domain") or "").strip()
        if not name or not domain:
            continue

        expires_raw = cookie.get("expires")
        expires: int | None
        if expires_raw is None or expires_raw == "":
            expires = None
        else:
            try:
                expires = int(expires_raw)
            except (TypeError, ValueError):
                continue

        if expires is not None and expires <= now:
            continue

        sanitized.append(
            {
                "name": name,
                "value": str(cookie.get("value") or ""),
                "domain": domain,
                "path": str(cookie.get("path") or "/") or "/",
                "secure": bool(cookie.get("secure")),
                "expires": expires,
            }
        )

    if not sanitized:
        delete_persisted_session(normalized_host)
        return

    payload = _session_file_payload()
    host_map = payload.get("hosts")
    hosts = dict(host_map) if isinstance(host_map, dict) else {}
    hosts[normalized_host] = sanitized
    _write_session_file(hosts)


def delete_persisted_session(host: str) -> None:
    normalized_host = _normalize_saved_host(host)
    if not normalized_host:
        return

    payload = _session_file_payload()
    host_map = payload.get("hosts")
    if not isinstance(host_map, dict):
        return

    hosts = dict(host_map)
    if hosts.pop(normalized_host, None) is None:
        return
    _write_session_file(hosts)


def normalize_computer_use_trust_mode(value: object) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in _VALID_COMPUTER_USE_TRUST_MODES:
        return normalized
    return _DEFAULT_COMPUTER_USE_TRUST_MODE


def save_computer_use_enabled(enabled: bool) -> None:
    save_env(_COMPUTER_USE_ENABLED_KEY, "1" if enabled else "0")


def save_computer_use_trust_mode(mode: str) -> None:
    save_env(_COMPUTER_USE_TRUST_MODE_KEY, normalize_computer_use_trust_mode(mode))


def save_computer_use_restore_token(token: str) -> None:
    token_value = str(token or "").strip()
    if token_value:
        save_env(_COMPUTER_USE_RESTORE_TOKEN_KEY, token_value)
        return
    delete_env(_COMPUTER_USE_RESTORE_TOKEN_KEY)


def normalize_host_browser_relaunch_preference(value: object) -> str:
    normalized = str(value or "").strip().lower().replace("-", "_")
    if normalized in _VALID_HOST_BROWSER_RELAUNCH_PREFERENCES:
        return normalized
    return "ask"


def save_host_browser_enabled(enabled: bool) -> None:
    save_env(_HOST_BROWSER_ENABLED_KEY, "1" if enabled else "0")


def save_host_browser_profile(
    *,
    family: str = "",
    profile_path: str = "",
    profile_label: str = "",
) -> None:
    family_value = str(family or "").strip().lower()
    profile_path_value = str(profile_path or "").strip()
    profile_label_value = str(profile_label or "").strip()

    if family_value:
        save_env(_HOST_BROWSER_FAMILY_KEY, family_value)
    else:
        delete_env(_HOST_BROWSER_FAMILY_KEY)

    if profile_path_value:
        save_env(_HOST_BROWSER_PROFILE_PATH_KEY, profile_path_value)
    else:
        delete_env(_HOST_BROWSER_PROFILE_PATH_KEY)

    if profile_label_value:
        save_env(_HOST_BROWSER_PROFILE_LABEL_KEY, profile_label_value)
    else:
        delete_env(_HOST_BROWSER_PROFILE_LABEL_KEY)


def save_host_browser_relaunch_preference(preference: str) -> None:
    save_env(
        _HOST_BROWSER_RELAUNCH_PREFERENCE_KEY,
        normalize_host_browser_relaunch_preference(preference),
    )


def save_last_context(host: str, context_id: str) -> None:
    """Persist the last active chat context for the current host."""
    normalized_host = host.strip().rstrip("/")
    normalized_context_id = context_id.strip()
    if not normalized_host or not normalized_context_id:
        return

    save_env(_LAST_CONTEXT_HOST_KEY, normalized_host)
    save_env(_LAST_CONTEXT_ID_KEY, normalized_context_id)


def load_config() -> CLIConfig:
    """Load config from environment variables, falling back to ~/.agent-zero/.env."""
    dotenv = _read_dotenv()

    instance_url = os.environ.get("AGENT_ZERO_HOST") or dotenv.get("AGENT_ZERO_HOST", "")
    last_context_id = os.environ.get(_LAST_CONTEXT_ID_KEY) or dotenv.get(_LAST_CONTEXT_ID_KEY, "")
    last_context_host = os.environ.get(_LAST_CONTEXT_HOST_KEY) or dotenv.get(_LAST_CONTEXT_HOST_KEY, "")
    default_context_id = (
        os.environ.get(_DEFAULT_CONTEXT_ID_KEY)
        or os.environ.get(_DEFAULT_CHAT_KEY)
        or dotenv.get(_DEFAULT_CONTEXT_ID_KEY, "")
        or dotenv.get(_DEFAULT_CHAT_KEY, "")
        or dotenv.get("default_chat", "")
    ).strip()
    remote_exec_enabled = coerce_bool(
        os.environ.get(
            _REMOTE_EXEC_ENABLED_KEY,
            os.environ.get(
                _A0_REMOTE_EXEC_KEY,
                dotenv.get(
                    _REMOTE_EXEC_ENABLED_KEY,
                    dotenv.get(_A0_REMOTE_EXEC_KEY, "0"),
                ),
            ),
        ),
        default=False,
    )
    remember_host = coerce_bool(
        os.environ.get(_REMEMBER_HOST_KEY, dotenv.get(_REMEMBER_HOST_KEY, "0")),
        default=False,
    )
    computer_use_enabled = coerce_bool(
        os.environ.get(_COMPUTER_USE_ENABLED_KEY, dotenv.get(_COMPUTER_USE_ENABLED_KEY, "0")),
        default=False,
    )
    computer_use_trust_mode = normalize_computer_use_trust_mode(
        os.environ.get(_COMPUTER_USE_TRUST_MODE_KEY, dotenv.get(_COMPUTER_USE_TRUST_MODE_KEY, ""))
    )
    computer_use_restore_token = (
        os.environ.get(_COMPUTER_USE_RESTORE_TOKEN_KEY)
        or dotenv.get(_COMPUTER_USE_RESTORE_TOKEN_KEY, "")
    ).strip()
    host_browser_enabled = coerce_bool(
        os.environ.get(_HOST_BROWSER_ENABLED_KEY, dotenv.get(_HOST_BROWSER_ENABLED_KEY, "0")),
        default=False,
    )
    host_browser_family = (
        os.environ.get(_HOST_BROWSER_FAMILY_KEY)
        or dotenv.get(_HOST_BROWSER_FAMILY_KEY, "")
    ).strip().lower()
    host_browser_profile_path = (
        os.environ.get(_HOST_BROWSER_PROFILE_PATH_KEY)
        or dotenv.get(_HOST_BROWSER_PROFILE_PATH_KEY, "")
    ).strip()
    host_browser_profile_label = (
        os.environ.get(_HOST_BROWSER_PROFILE_LABEL_KEY)
        or dotenv.get(_HOST_BROWSER_PROFILE_LABEL_KEY, "")
    ).strip()
    host_browser_relaunch_preference = normalize_host_browser_relaunch_preference(
        os.environ.get(
            _HOST_BROWSER_RELAUNCH_PREFERENCE_KEY,
            dotenv.get(_HOST_BROWSER_RELAUNCH_PREFERENCE_KEY, "ask"),
        )
    )

    return CLIConfig(
        instance_url=instance_url,
        last_context_id=last_context_id,
        last_context_host=last_context_host,
        default_context_id=default_context_id,
        remote_exec_enabled=remote_exec_enabled,
        remember_host=remember_host,
        computer_use_enabled=computer_use_enabled,
        computer_use_trust_mode=computer_use_trust_mode,
        computer_use_restore_token=computer_use_restore_token,
        host_browser_enabled=host_browser_enabled,
        host_browser_family=host_browser_family,
        host_browser_profile_path=host_browser_profile_path,
        host_browser_profile_label=host_browser_profile_label,
        host_browser_relaunch_preference=host_browser_relaunch_preference,
    )
