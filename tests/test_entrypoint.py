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
    assert "--host URL" in captured.out
    assert "--no-auto-connect" in captured.out
    assert "--no-docker-discovery" in captured.out
    assert "AGENT_ZERO_HOST" in captured.out
    assert "update" in captured.out
    assert launched == []


def test_main_connection_flags_route_to_app_launcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launched: list[dict[str, object]] = []

    def fake_run_app(**kwargs: object) -> None:
        launched.append(dict(kwargs))

    monkeypatch.setattr(__main__, "_run_app", fake_run_app)

    exit_code = __main__.main(
        [
            "--host",
            "https://example.trycloudflare.com",
            "--no-auto-connect",
            "--no-docker-discovery",
        ]
    )

    assert exit_code == 0
    assert launched == [
        {
            "host": "https://example.trycloudflare.com",
            "auto_connect_single": False,
            "discover_instances": False,
        }
    ]


def test_main_update_routes_without_launching_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launched: list[bool] = []
    updated: list[bool] = []
    monkeypatch.setattr(__main__, "_run_app", lambda: launched.append(True))
    monkeypatch.setattr(__main__, "_run_self_update", lambda: updated.append(True) or 0)

    exit_code = __main__.main(["update"])

    assert exit_code == 0
    assert updated == [True]
    assert launched == []
