from __future__ import annotations

import importlib.util
from pathlib import Path

from agent_zero_cli.config import CLIConfig


ROOT = Path(__file__).resolve().parents[1]


def _load_module(name: str, relative_path: str):
    path = ROOT / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_snapshot_uses_current_cli_config_shape() -> None:
    snapshot = _load_module("snapshot_module", "devtools/snapshot.py")

    config = snapshot._snapshot_config()

    assert isinstance(config, CLIConfig)
    assert config.instance_url == "http://127.0.0.1:19999"
