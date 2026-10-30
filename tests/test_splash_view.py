from __future__ import annotations

from types import SimpleNamespace

import pytest
from textual.app import App, ComposeResult

from agent_zero_cli.instance_discovery import DiscoveredInstance
from agent_zero_cli.widgets import SplashState, SplashView
from agent_zero_cli.widgets.splash_view import (
    SplashHostPanel,
    SplashHostRow,
    _normalize_connection_target,
    _validate_connection_target,
)


class SplashViewHarness(App[None]):
    def compose(self) -> ComposeResult:
        yield SplashView()


def test_splash_stage_updates_styles_only_when_stage_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    view = SplashView()
    view.add_class("custom-splash")
    updates: list[bool] = []
    monkeypatch.setattr(view, "update_node_styles", lambda **_: updates.append(True))

    for stage in ("host", "login", "connecting", "ready", "error", "host"):
        updates.clear()
        view._apply_stage(stage)
        assert view.classes == {"custom-splash", f"stage-{stage}"}
        assert updates == [True]
        assert view._host_panel.display is (stage == "host")
        assert view._login_panel.display is (stage == "login")
        assert view._status_panel.display is (stage in {"connecting", "error"})
        assert view._actions.display is (stage == "ready")

        updates.clear()
        view._apply_stage(stage)
        assert updates == []


@pytest.mark.parametrize(
    "host",
    [
        "https://webmasters-ink-tribe-zope.trycloudflare.com",
        "https://webmasters-ink-tribe-zope.trycloudflare.com:443",
        "http://localhost",
    ],
)
def test_validate_connection_target_accepts_standard_urls_without_explicit_port(host: str) -> None:
    valid, message = _validate_connection_target(host)

    assert valid is True
    assert message.startswith("URL format looks valid.")


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("localhost:32080", "http://localhost:32080"),
        ("127.0.0.1:5080", "http://127.0.0.1:5080"),
        ("[::1]:5080", "http://[::1]:5080"),
        ("agent-zero.example.com", "http://agent-zero.example.com"),
        ("//localhost:32080", "http://localhost:32080"),
        ("https://agent-zero.example.com/", "https://agent-zero.example.com"),
    ],
)
def test_normalize_connection_target_adds_http_for_manual_hostnames(
    host: str,
    expected: str,
) -> None:
    assert _normalize_connection_target(host) == expected


def test_validate_connection_target_accepts_manual_host_without_scheme() -> None:
    valid, message = _validate_connection_target("localhost:32080")

    assert valid is True
    assert message == "URL format looks valid. Using http://localhost:32080."


def test_validate_connection_target_rejects_unsupported_scheme() -> None:
    valid, message = _validate_connection_target("ws://localhost:32080")

    assert valid is False
    assert message == "Invalid URL format. Include http:// or https://."


def test_manual_host_panel_connect_host_normalizes_url() -> None:
    panel = SplashHostPanel()
    panel._state = SplashState(stage="host", manual_entry_expanded=True)
    panel._host = SimpleNamespace(value="localhost:32080")  # type: ignore[assignment]

    assert panel.connect_host == "http://localhost:32080"


def test_empty_host_panel_mentions_agent_zero_install_url() -> None:
    panel = SplashHostPanel()
    panel._state = SplashState(
        stage="host",
        discovery_status="empty",
        discovery_detail="No running Docker containers were found.",
    )

    message = panel._status_message()

    assert "Install Agent Zero: http://agent-zero.ai" in message.plain


@pytest.mark.anyio
async def test_host_rows_select_with_arrow_keys() -> None:
    app = SplashViewHarness()
    first = DiscoveredInstance("first", "First", "http://localhost:5080", "5080")
    second = DiscoveredInstance("second", "Second", "http://localhost:5081", "5081")

    async with app.run_test(size=(100, 30)) as pilot:
        view = app.query_one(SplashView)
        view.set_state(
            SplashState(
                stage="host",
                discovered_instances=(first, second),
                discovery_status="ready",
                selected_host_url=first.url,
            )
        )
        await pilot.pause()

        rows = list(view.query(SplashHostRow))
        assert app.focused is rows[0]

        await pilot.press("down")
        assert app.focused is rows[1]
        assert view._state.selected_host_url == second.url

        await pilot.press("up")
        assert app.focused is rows[0]
        assert view._state.selected_host_url == first.url


def test_error_back_button_requests_navigation_to_host() -> None:
    view = SplashView()
    messages: list[object] = []
    view.post_message = lambda message: messages.append(message)  # type: ignore[method-assign]

    view.on_button_pressed(
        SimpleNamespace(button=SimpleNamespace(id="splash-status-back"))  # type: ignore[arg-type]
    )

    assert len(messages) == 1
    assert isinstance(messages[0], SplashView.ActionRequested)
    assert messages[0].action == "back"
