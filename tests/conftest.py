from pathlib import Path

import pytest

from agent_zero_cli import config


@pytest.fixture(autouse=True)
def isolate_cli_preferences(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    directory = tmp_path / ".agent-zero"
    monkeypatch.setattr(config, "_ENV_DIR", directory)
    monkeypatch.setattr(config, "_ENV_FILE", directory / ".env")
    monkeypatch.setattr(config, "_SESSION_FILE", directory / "session_cookies.json")
