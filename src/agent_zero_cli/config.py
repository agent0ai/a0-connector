import os
from dataclasses import dataclass
from pathlib import Path

_ENV_DIR = Path.home() / ".agent-zero"
_ENV_FILE = _ENV_DIR / ".env"
_LAST_CONTEXT_ID_KEY = "AGENT_ZERO_LAST_CONTEXT_ID"
_LAST_CONTEXT_HOST_KEY = "AGENT_ZERO_LAST_CONTEXT_HOST"
_COMPUTER_USE_ENABLED_KEY = "AGENT_ZERO_COMPUTER_USE_ENABLED"
_COMPUTER_USE_TRUST_MODE_KEY = "AGENT_ZERO_COMPUTER_USE_TRUST_MODE"
_COMPUTER_USE_RESTORE_TOKEN_KEY = "AGENT_ZERO_COMPUTER_USE_RESTORE_TOKEN"
_DEFAULT_COMPUTER_USE_TRUST_MODE = "persistent"
_VALID_COMPUTER_USE_TRUST_MODES = {"persistent", "free_run"}
_COMPUTER_USE_TRUST_MODE_ALIASES = {
    "confirm": "persistent",
    "confirm with user": "persistent",
    "confirm_with_user": "persistent",
    "confirm-with-user": "persistent",
    "interactive": "persistent",
    "free run": "free_run",
    "free-run": "free_run",
}


@dataclass
class CLIConfig:
    instance_url: str = ""
    last_context_id: str = ""
    last_context_host: str = ""
    computer_use_enabled: bool = False
    computer_use_trust_mode: str = _DEFAULT_COMPUTER_USE_TRUST_MODE
    computer_use_restore_token: str = ""


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


def _parse_bool(value: object, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value or "").strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def normalize_computer_use_trust_mode(value: object) -> str:
    normalized = str(value or "").strip().lower()
    normalized = _COMPUTER_USE_TRUST_MODE_ALIASES.get(normalized, normalized)
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
    computer_use_enabled = _parse_bool(
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

    return CLIConfig(
        instance_url=instance_url,
        last_context_id=last_context_id,
        last_context_host=last_context_host,
        computer_use_enabled=computer_use_enabled,
        computer_use_trust_mode=computer_use_trust_mode,
        computer_use_restore_token=computer_use_restore_token,
    )
