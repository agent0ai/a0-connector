from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from agent_zero_cli import __main__
from agent_zero_cli import __version__


ROOT = Path(__file__).resolve().parents[1]


def test_package_version_matches_cli_version() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    version_line = next(line for line in pyproject.splitlines() if line.startswith("version = "))

    assert version_line.removeprefix("version = ").strip().strip('"') == __version__


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
    assert "--chat CONTEXT_ID" in captured.out
    assert "--chat-last" in captured.out
    assert "--no-auto-connect" in captured.out
    assert "--no-docker-discovery" in captured.out
    assert "--connect" in captured.out
    assert "AGENT_ZERO_HOST" in captured.out
    assert "update" in captured.out
    assert "browser-extension" in captured.out
    assert "headless" in captured.out
    assert "gateway" in captured.out
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
            "--chat",
            "ctx-123",
            "--no-auto-connect",
            "--no-docker-discovery",
            "--connect",
        ]
    )

    assert exit_code == 0
    assert launched == [
        {
            "host": "https://example.trycloudflare.com",
            "chat": "ctx-123",
            "chat_last": False,
            "auto_connect_single": False,
            "discover_instances": False,
            "connect_configured_host": True,
        }
    ]


def test_main_chat_last_flag_routes_to_app_launcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launched: list[dict[str, object]] = []

    def fake_run_app(**kwargs: object) -> None:
        launched.append(dict(kwargs))

    monkeypatch.setattr(__main__, "_run_app", fake_run_app)

    exit_code = __main__.main(["--chat-last"])

    assert exit_code == 0
    assert launched == [
        {
            "host": "",
            "chat": "",
            "chat_last": True,
            "auto_connect_single": True,
            "discover_instances": True,
            "connect_configured_host": False,
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


def test_main_browser_extension_install_routes_without_launching_other_modes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launched: list[str] = []
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(__main__, "_run_app", lambda **kwargs: launched.append("app"))
    monkeypatch.setattr(__main__, "_run_headless", lambda **kwargs: launched.append("headless"))
    monkeypatch.setattr(__main__, "_run_gateway", lambda **kwargs: launched.append("gateway"))
    monkeypatch.setattr(__main__, "_run_acp", lambda **kwargs: launched.append("acp"))

    def fake_browser_extension(**kwargs: object) -> int:
        calls.append(dict(kwargs))
        return 6

    monkeypatch.setattr(__main__, "_run_browser_extension", fake_browser_extension)

    exit_code = __main__.main(
        [
            "--host",
            "http://localhost:50080",
            "browser-extension",
            "install",
            "--browser",
            "chrome",
            "--browser",
            "edge",
            "--json",
        ]
    )

    assert exit_code == 6
    assert calls == [
        {
            "command": "install",
            "host": "http://localhost:50080",
            "browsers": ["chrome", "edge"],
            "json_output": True,
            "yes": False,
            "force_local": False,
            "keep_logs": False,
        }
    ]
    assert launched == []


def test_main_browser_extension_uninstall_routes_all_lifecycle_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        __main__,
        "_run_browser_extension",
        lambda **kwargs: calls.append(dict(kwargs)) or 0,
    )

    exit_code = __main__.main(
        [
            "browser-extension",
            "uninstall",
            "--yes",
            "--force-local",
            "--keep-logs",
            "--json",
        ]
    )

    assert exit_code == 0
    assert calls == [
        {
            "command": "uninstall",
            "host": "",
            "browsers": (),
            "json_output": True,
            "yes": True,
            "force_local": True,
            "keep_logs": True,
        }
    ]


def test_main_headless_routes_to_headless_launcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launched: list[dict[str, object]] = []
    monkeypatch.setattr(__main__, "_run_headless", lambda **kwargs: launched.append(dict(kwargs)) or 0)

    exit_code = __main__.main(
        [
            "headless",
            "--host",
            "http://agent.test:32080",
            "--chat",
            "ctx-123",
            "--output",
            "jsonl",
            "--print",
            "what is 2+2",
            "--workspace",
            "/tmp/work",
            "--no-docker-discovery",
        ]
    )

    assert exit_code == 0
    assert launched == [
        {
            "host": "http://agent.test:32080",
            "chat": "ctx-123",
            "chat_last": False,
            "new_chat": False,
            "output": "jsonl",
            "print_prompt": "what is 2+2",
            "workspace": "/tmp/work",
            "discover_instances": False,
        }
    ]


def test_main_gateway_routes_without_loading_textual(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launched: list[dict[str, object]] = []
    monkeypatch.setattr(__main__, "_run_gateway", lambda **kwargs: launched.append(dict(kwargs)) or 0)

    exit_code = __main__.main(
        [
            "gateway",
            "--host",
            "http://agent.test:32080",
            "--workspace",
            "/tmp/work",
            "--gateway-id",
            "launcher-1",
            "--host-label",
            "Workstation",
            "--no-master",
            "--scopes",
            "files,browser",
            "--browser-selection",
            "chrome:default",
        ]
    )

    assert exit_code == 0
    assert launched == [
        {
            "host": "http://agent.test:32080",
            "workspace": "/tmp/work",
            "gateway_id": "launcher-1",
            "host_label": "Workstation",
            "master_enabled": False,
            "scopes": "files,browser",
            "browser_selection": "chrome:default",
        }
    ]


def test_headless_and_gateway_launchers_do_not_import_terminal_image_renderer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_zero_cli import config as config_mod
    from agent_zero_cli import gateway as gateway_mod
    from agent_zero_cli.headless import runner as headless_runner

    for module_name in (
        "textual_image",
        "agent_zero_cli.app",
        "agent_zero_cli.image_store",
        "agent_zero_cli.widgets.image_entry",
    ):
        monkeypatch.delitem(sys.modules, module_name, raising=False)
    monkeypatch.setattr(config_mod, "load_config", lambda: SimpleNamespace(instance_url=""))
    monkeypatch.setattr(headless_runner, "run_headless", lambda _options: 0)
    monkeypatch.setattr(gateway_mod, "run_gateway", lambda _options, _config: 0)

    assert __main__._run_headless() == 0
    assert __main__._run_gateway(
        host="http://agent.test",
        workspace=".",
        gateway_id="test",
        host_label="",
        master_enabled=True,
        scopes="file_read",
        browser_selection="",
    ) == 0
    assert "textual_image" not in sys.modules
    assert "agent_zero_cli.app" not in sys.modules
    assert "agent_zero_cli.image_store" not in sys.modules
    assert "agent_zero_cli.widgets.image_entry" not in sys.modules


def test_browser_extension_launcher_does_not_import_textual_runtime(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from agent_zero_cli import browser_extension

    for module_name in (
        "textual_image",
        "agent_zero_cli.app",
        "agent_zero_cli.image_store",
        "agent_zero_cli.widgets.image_entry",
    ):
        monkeypatch.delitem(sys.modules, module_name, raising=False)
    monkeypatch.setattr(browser_extension, "_resolve_companion_executable", lambda: None)

    assert __main__._run_browser_extension(command="status", json_output=True) == 3
    capsys.readouterr()
    assert "textual_image" not in sys.modules
    assert "agent_zero_cli.app" not in sys.modules
    assert "agent_zero_cli.image_store" not in sys.modules
    assert "agent_zero_cli.widgets.image_entry" not in sys.modules


def test_run_app_installs_textual_input_decoder_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_zero_cli import app as app_mod
    from agent_zero_cli import config as config_mod
    from agent_zero_cli import image_render
    from agent_zero_cli import textual_compat

    calls: list[str] = []

    class FakeAgentZeroCLI:
        def __init__(self, *_args: object, **kwargs: object) -> None:
            passed_renderer = kwargs["image_renderer"]
            assert passed_renderer.mode == "halfcell"
            assert passed_renderer._widget_factory is None
            calls.append("app-init")

        def run(self) -> None:
            calls.append("app-run")

    monkeypatch.setattr(
        textual_compat,
        "install_textual_linux_input_decoder_guard",
        lambda: calls.append("guard"),
    )
    real_initialize = image_render.initialize_image_renderer

    def initialize_for_test() -> image_render.ImageRenderer:
        calls.append("renderer-init")
        return real_initialize()

    monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/test_entrypoint.py::automated")
    monkeypatch.setattr(image_render, "initialize_image_renderer", initialize_for_test)
    monkeypatch.setattr(
        config_mod,
        "load_config",
        lambda: SimpleNamespace(instance_url="", default_context_id=""),
    )
    monkeypatch.setattr(app_mod, "AgentZeroCLI", FakeAgentZeroCLI)

    __main__._run_app()

    assert calls == ["guard", "renderer-init", "app-init", "app-run"]
