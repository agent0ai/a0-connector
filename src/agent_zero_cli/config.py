import os
from dataclasses import dataclass
from pathlib import Path

_ENV_DIR = Path.home() / ".agent-zero"
_ENV_FILE = _ENV_DIR / ".env"


@dataclass
class CLIConfig:
    instance_url: str = ""


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


def load_config() -> CLIConfig:
    """Load config from environment variables, falling back to ~/.agent-zero/.env."""
    dotenv = _read_dotenv()

    instance_url = os.environ.get("AGENT_ZERO_HOST") or dotenv.get("AGENT_ZERO_HOST", "")

    return CLIConfig(instance_url=instance_url)
