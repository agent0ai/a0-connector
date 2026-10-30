from pathlib import Path

import pytest
from rich.panel import Panel
from textual.app import App, ComposeResult

from agent_zero_cli.widgets.chat_log import (
    AgentZeroBanner,
    ChatLog,
    _AGENT_ZERO_BANNER,
    _AGENT_ZERO_BANNER_COMPACT,
    _banner_width,
    _select_agent_zero_banner,
)
from agent_zero_cli.widgets.splash_view import SplashView


def test_banner_switches_at_the_width_needed_to_preserve_its_shape() -> None:
    full_width = _banner_width(_AGENT_ZERO_BANNER)
    symbol_width = _banner_width(_AGENT_ZERO_BANNER_COMPACT)

    assert _select_agent_zero_banner(full_width) == _AGENT_ZERO_BANNER
    assert _select_agent_zero_banner(full_width - 1) == _AGENT_ZERO_BANNER_COMPACT
    assert _select_agent_zero_banner(symbol_width) == _AGENT_ZERO_BANNER_COMPACT
    assert _select_agent_zero_banner(symbol_width - 1) == ""


@pytest.mark.anyio
@pytest.mark.parametrize("surface", ["splash", "chat"])
async def test_splash_and_chat_restore_the_full_logo_after_resizing(surface: str) -> None:
    class BannerHarness(App[None]):
        CSS_PATH = Path(__file__).resolve().parents[1] / "src/agent_zero_cli/styles/app.tcss"

        def compose(self) -> ComposeResult:
            yield SplashView() if surface == "splash" else ChatLog(id="chat-log")

    app = BannerHarness()
    async with app.run_test(size=(80, 30)) as pilot:
        if surface == "chat":
            log = app.query_one(ChatLog)
            log.ensure_intro_banner()
            log.set_workspace(local_workspace="/workspace")
            log.append_or_update(1, Panel("Hello"))
        await pilot.pause()
        banner = app.query_one(AgentZeroBanner)

        for width, height, expected in (
            (80, 30, _AGENT_ZERO_BANNER),
            (40, 30, _AGENT_ZERO_BANNER_COMPACT),
            (80, 24, _AGENT_ZERO_BANNER),
        ):
            await pilot.resize_terminal(width, height)
            await pilot.pause()
            assert banner._current_banner == expected
            assert banner.region.height == len(expected.splitlines())
            assert _banner_width(expected) <= banner.content_size.width
            if surface == "chat":
                assert log._workspace_widget.region.y > banner.region.bottom
                assert log._seq_to_widget[1].region.y > log._workspace_widget.region.y
