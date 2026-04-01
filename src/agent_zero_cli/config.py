import json
from dataclasses import dataclass
from pathlib import Path


@dataclass
class CLIConfig:
    instance_url: str = "http://localhost:5080"
    api_key: str = ""
    theme: str = "dark"


def load_config() -> CLIConfig:
    """Load config from .cli-config.json, searching CWD then ~/.agentzero/."""
    search_paths = [
        Path.cwd() / ".cli-config.json",
        Path.home() / ".agentzero" / ".cli-config.json",
    ]
    for path in search_paths:
        if path.exists():
            with path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
            allowed = CLIConfig.__dataclass_fields__.keys()
            filtered = {key: value for key, value in data.items() if key in allowed}
            return CLIConfig(**filtered)
    return CLIConfig()
