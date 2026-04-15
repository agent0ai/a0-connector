import os
from dataclasses import dataclass
from pathlib import Path

_ENV_DIR = Path.home() / ".agent-zero"
_ENV_FILE = _ENV_DIR / ".env"
_LAST_CONTEXT_ID_KEY = "AGENT_ZERO_LAST_CONTEXT_ID"
_LAST_CONTEXT_HOST_KEY = "AGENT_ZERO_LAST_CONTEXT_HOST"


@dataclass
class CLIConfig:
    instance_url: str = ""
    last_context_id: str = ""
    last_context_host: str = ""
    username: str = ""
    password: str = ""
    codeexec: bool = False


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


def save_last_context(host: str, context_id: str) -> None:
    """Persist the last active chat context for the current host."""
    normalized_host = host.strip().rstrip("/")
    normalized_context_id = context_id.strip()
    if not normalized_host or not normalized_context_id:
        return

    save_env(_LAST_CONTEXT_HOST_KEY, normalized_host)
    save_env(_LAST_CONTEXT_ID_KEY, normalized_context_id)


def load_config(
    *,
    cli_server: str = "",
    cli_username: str = "",
    cli_password: str = "",
    cli_codeexec: bool = False,
) -> CLIConfig:
    """Load config from CLI args, environment variables, and ~/.agent-zero/.env.

    Priority: CLI args > environment variables > dotenv file > defaults.
    """
    dotenv = _read_dotenv()

    # Server URL: --server > A0_CLI_SERVER > AGENT_ZERO_HOST env/dotenv
    instance_url = (
        cli_server
        or os.environ.get("A0_CLI_SERVER", "")
        or os.environ.get("AGENT_ZERO_HOST", "")
        or dotenv.get("AGENT_ZERO_HOST", "")
    )

    last_context_id = os.environ.get(_LAST_CONTEXT_ID_KEY) or dotenv.get(_LAST_CONTEXT_ID_KEY, "")
    last_context_host = os.environ.get(_LAST_CONTEXT_HOST_KEY) or dotenv.get(_LAST_CONTEXT_HOST_KEY, "")

    # Username: --username > A0_CLI_USERNAME
    username = cli_username or os.environ.get("A0_CLI_USERNAME", "")

    # Password: --password > A0_CLI_PASSWORD
    password = cli_password or os.environ.get("A0_CLI_PASSWORD", "")

    # Code execution: --codeexec flag (no env fallback)
    codeexec = cli_codeexec

    return CLIConfig(
        instance_url=instance_url,
        last_context_id=last_context_id,
        last_context_host=last_context_host,
        username=username,
        password=password,
        codeexec=codeexec,
    )
