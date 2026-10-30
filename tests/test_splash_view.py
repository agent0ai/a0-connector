from __future__ import annotations

from dataclasses import replace
from pathlib import Path
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
    CSS_PATH = Path(__file__).resolve().parents[1] / "src/agent_zero_cli/styles/app.tcss"

    def __init__(self) -> None:
        super().__init__()
        self.submissions: list[SplashView.SubmitRequested] = []

    def compose(self) -> ComposeResult:
        yield SplashView()

    def on_splash_view_host_state_changed(self, event: SplashView.HostStateChanged) -> None:
        view = self.query_one(SplashView)
        view.set_state(view._state)

    def on_splash_view_submit_requested(self, event: SplashView.SubmitRequested) -> None:
        self.submissions.append(event)


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
    assert message == ""


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
    assert message == "Will connect to http://localhost:32080"


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

        await pilot.press("down", "tab", "space")
        assert app.focused is view._host_panel._remember_host
        assert list(view.query(SplashHostRow)) == rows
        assert [row.has_class("-selected") for row in rows] == [False, True]
        await pilot.press("tab", "enter")
        assert len(app.submissions) == 1
        assert app.submissions[0].host == second.url
        assert app.submissions[0].remember_host is True


@pytest.mark.anyio
@pytest.mark.parametrize("size", [(40, 30), (80, 24), (100, 30), (160, 24), (160, 48)])
@pytest.mark.parametrize("manual", [False, True])
async def test_splash_connection_controls_fit_without_scrolling(
    size: tuple[int, int], manual: bool,
) -> None:
    app = SplashViewHarness()
    first = DiscoveredInstance("first", "Agent Zero", "http://localhost:5080", "5080")
    second = DiscoveredInstance("second", "Development", "http://localhost:5081", "5081")

    async with app.run_test(size=size) as pilot:
        view = app.query_one(SplashView)
        view.set_state(SplashState(
            stage="host", host=first.url, local_workspace="/home/developer/workspace",
            discovery_status="ready", discovered_instances=(first, second),
            selected_host_url=first.url, manual_entry_expanded=manual,
        ))
        await pilot.pause()

        panel = view._host_panel
        assert view.max_scroll_y == 0
        assert panel._instances_mount.max_scroll_y == 0
        assert panel._refresh.region.y == panel._title.region.y
        assert panel._refresh.region.right == panel.content_region.right
        target = panel._manual_section if manual else panel._instances_mount
        assert target.region.y == panel._title.region.bottom + 1
        assert panel._manual_toggle.region.y > panel._hint.region.bottom
        assert not view.query("#splash-workspace")
        for control in (panel._button, panel._remember_host, panel._refresh, panel._manual_toggle):
            assert control.region.width > 0
            assert control.region in view.content_region
        for button in (panel._button, panel._refresh, panel._manual_toggle):
            rendered = "".join(line.text for line in button.render_lines(button.region.reset_offset))
            assert button.label.plain in rendered
        if manual:
            assert panel._host.region in view.content_region
            assert panel._host.region.bottom <= panel._button.region.y


@pytest.mark.anyio
async def test_manual_connection_validation_and_resize_preserve_the_target() -> None:
    app = SplashViewHarness()
    async with app.run_test(size=(100, 30)) as pilot:
        view = app.query_one(SplashView)
        view.set_state(SplashState(stage="host", discovery_status="ready", manual_entry_expanded=True))
        await pilot.pause()
        panel = view._host_panel
        assert view._hero.region.height == 6

        panel._host.value = "ws://localhost:5080"
        await pilot.pause()
        await pilot.press("enter")
        assert panel._button.disabled
        assert app.submissions == []

        panel._host.value = "localhost:5080"
        await pilot.pause()
        assert not panel._button.disabled
        await pilot.resize_terminal(80, 24)
        await pilot.pause()
        assert view._hero.region.height == 6
        assert panel.connect_host == "http://localhost:5080"
        assert panel._button.region in view.content_region
        await pilot.press("enter")
        assert len(app.submissions) == 1
        assert app.submissions[0].host == "http://localhost:5080"

        view.set_state(replace(view._state, manual_entry_expanded=False))
        view.set_state(replace(view._state, manual_entry_expanded=True))
        await pilot.pause()
        assert panel._host.value == "localhost:5080"


@pytest.mark.anyio
async def test_many_hosts_scroll_to_the_keyboard_selection() -> None:
    app = SplashViewHarness()
    hosts = tuple(DiscoveredInstance(str(i), f"Host {i}", f"http://localhost:{5080+i}", str(5080+i)) for i in range(6))
    async with app.run_test(size=(100, 30)) as pilot:
        view = app.query_one(SplashView)
        view.set_state(SplashState(
            stage="host", discovery_status="ready", discovered_instances=hosts, selected_host_url=hosts[0].url,
        ))
        await pilot.pause()
        await pilot.press(*(["down"] * 5))
        assert app.focused.host == hosts[-1].url
        assert view._host_panel._instances_mount.scroll_y > 0
        await pilot.press("enter")
        assert app.submissions[0].host == hosts[-1].url


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
