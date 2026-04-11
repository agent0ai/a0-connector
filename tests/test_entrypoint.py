from __future__ import annotations

import pytest

from agent_zero_cli import __main__
from agent_zero_cli import __version__


def test_main_prints_version_without_launching_app(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    launched: list[bool] = []
    monkeypatch.setattr(__main__, "_run_app", lambda: launched.append(True))

    exit_code = __main__.main(["--version"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out.strip() == __version__
    assert launched == []


def test_main_help_exits_without_launching_app(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    launched: list[bool] = []
    monkeypatch.setattr(__main__, "_run_app", lambda: launched.append(True))

    with pytest.raises(SystemExit) as exc_info:
        __main__.main(["--help"])

    captured = capsys.readouterr()
    assert exc_info.value.code == 0
    assert "usage: a0" in captured.out
    assert launched == []
