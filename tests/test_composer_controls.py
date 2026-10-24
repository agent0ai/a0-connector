from __future__ import annotations

import asyncio
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.widgets import Button, Input, Static

from agent_zero_cli.client import A0Client
from agent_zero_cli.composer_controls import choose_control, refresh_controls
from agent_zero_cli.screens.composer_choice import ComposerChoice, ComposerChoiceScreen
from agent_zero_cli.widgets.composer_controls import ComposerControls
from agent_zero_cli.widgets.model_switcher_bar import ModelSwitcherBar

pytestmark = pytest.mark.anyio
CSS_PATH = Path(__file__).resolve().parents[1] / "src/agent_zero_cli/styles/app.tcss"
CONTROL = {
    "id": "sample.mode", "type": "choice", "title": "Processing mode", "label": "Mode: Auto",
    "value": "", "revision": "opaque-17", "custom": True,
    "options": [{"value": value, "label": label} for value, label in (
        ("", "Use default"), ("careful", "Careful"), ("fast", "Fast"),
    )],
}
STATE = {"ok": True, "controls": [CONTROL]}


def make_app():
    bar = SimpleNamespace(controls=[])
    bar.set_composer_controls = lambda controls: setattr(bar, "controls", controls)
    return SimpleNamespace(
        connected=True, current_context="ctx-1", connector_features={"composer_controls"},
        composer_controls={}, _composer_controls_scope=None,
        _composer_controls_generation=0, _composer_controls_saving=False,
        client=SimpleNamespace(base_url="http://agent.test", composer_controls_action=AsyncMock(return_value=deepcopy(STATE))),
        query_one=lambda *args: bar, _show_notice=lambda *args, **kwargs: None,
        _focus_message_input=lambda: None, push_screen_wait=AsyncMock(return_value=None),
    )


async def test_client_preserves_csrf_base_path_and_opaque_control_revision():
    requests = []

    def receive(request):
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"ok": True, "token": "fresh-token"})
        if len([item for item in requests if item.method == "POST"]) == 1:
            return httpx.Response(403, text="Expired CSRF")
        return httpx.Response(200, json=STATE)

    client = A0Client("http://agent.test/prefix")
    await client.http.aclose()
    async with httpx.AsyncClient(transport=httpx.MockTransport(receive)) as transport:
        client.http = transport
        client._csrf_token = "expired-token"
        result = await client.composer_controls_action(
            "set", "ctx-1", control_id=CONTROL["id"], value="fast", custom=False, revision="opaque-17",
        )
        assert result == STATE
        posts = [request for request in requests if request.method == "POST"]
        assert str(posts[-1].url) == "http://agent.test/prefix/api/plugins/_a0_connector/v1/composer_controls"
        assert posts[0].headers["x-csrf-token"] == "expired-token"
        assert posts[-1].headers["x-csrf-token"] == "fresh-token"
        assert b'"revision":"opaque-17"' in posts[-1].content


async def test_older_core_hides_controls_without_calling_optional_endpoint():
    app = make_app()
    app.connector_features.clear()
    await refresh_controls(app)
    assert app.composer_controls == {}
    assert not app.query_one().controls
    app.client.composer_controls_action.assert_not_awaited()


@pytest.mark.parametrize("response", [{"ok": False, "status_code": 404}, {"ok": True, "controls": []}, {"ok": True, "controls": [None, {"type": "future-control"}]}])
async def test_missing_disabled_and_unsupported_controls_are_hidden(response):
    app = make_app()
    app.client.composer_controls_action.return_value = response
    await refresh_controls(app)
    assert app.composer_controls == {}
    assert not app.query_one().controls


async def test_refresh_discards_old_chat_and_out_of_order_responses():
    app = make_app()
    started, release = asyncio.Event(), asyncio.Event()

    async def delayed(*args):
        started.set()
        await release.wait()
        return deepcopy(STATE)

    app.client.composer_controls_action.side_effect = delayed
    stale = asyncio.create_task(refresh_controls(app))
    await started.wait()
    app.current_context = "ctx-2"
    app.client.composer_controls_action.side_effect = None
    app.client.composer_controls_action.return_value = {"ok": True, "controls": [{**CONTROL, "value": "fast"}]}
    await refresh_controls(app)
    release.set()
    await stale
    assert app.composer_controls[CONTROL["id"]]["value"] == "fast"
    assert app._composer_controls_scope == ("http://agent.test", "ctx-2")


@pytest.mark.parametrize("choice", [ComposerChoice("fast"), ComposerChoice(""), ComposerChoice(" custom-mode ", True)])
async def test_selection_custom_and_reset_are_forwarded_without_domain_rules(choice):
    app = make_app()
    app.push_screen_wait.return_value = choice
    await choose_control(app, CONTROL["id"], "composer-control-0")
    calls = app.client.composer_controls_action.await_args_list
    assert calls[1].args == ("set", "ctx-1")
    assert calls[1].kwargs == {
        "control_id": CONTROL["id"], "value": choice.value,
        "custom": choice.custom, "revision": "opaque-17",
    }
    assert calls[2].args == ("list", "ctx-1")


@pytest.mark.parametrize("target", ["host", "chat"])
async def test_menu_cannot_apply_after_host_or_chat_switch(target):
    app = make_app()

    async def switch_context(screen):
        if target == "host":
            app.client.base_url = "http://other.test"
        else:
            app.current_context = "ctx-2"
        return ComposerChoice("fast")

    app.push_screen_wait.side_effect = switch_context
    await choose_control(app, CONTROL["id"], "composer-control-0")
    app.client.composer_controls_action.assert_awaited_once_with("list", "ctx-1")


async def test_rejected_custom_value_stays_available_for_correction():
    app = make_app()
    screens = []

    async def choose(screen):
        screens.append(screen)
        return ComposerChoice("bad value", True) if len(screens) == 1 else None

    app.push_screen_wait.side_effect = choose
    app.client.composer_controls_action.side_effect = [deepcopy(STATE), {"ok": False, "status_code": 400, "message": "Invalid mode"}]
    await choose_control(app, CONTROL["id"], "composer-control-0")
    assert len(screens) == 2
    assert screens[-1]._custom_value == "bad value"
    assert screens[-1]._error == "Invalid mode"


async def test_stale_revision_is_reported_and_refreshed_not_retried():
    app = make_app()
    app._show_notice = lambda text, **kwargs: setattr(app, "notice", text)
    app.push_screen_wait.return_value = ComposerChoice("fast")
    app.client.composer_controls_action.side_effect = [deepcopy(STATE), {"ok": False, "status_code": 409, "message": "Reopen control"}, deepcopy(STATE)]
    await choose_control(app, CONTROL["id"], "composer-control-0")
    assert app.notice == "Reopen control"
    assert app.push_screen_wait.await_count == 1
    assert app.client.composer_controls_action.await_count == 3


class PopoverApp(App):
    CSS_PATH = CSS_PATH

    def __init__(self, *, controls=None):
        super().__init__()
        self.controls = deepcopy([CONTROL] if controls is None else controls)
        self.result = None

    def compose(self) -> ComposeResult:
        yield Vertical(Static("Agent Zero terminal preview"), id="background-root")
        yield ModelSwitcherBar(id="model-switcher-bar")

    def on_mount(self):
        bar = self.query_one(ModelSwitcherBar)
        bar.set_state(main_model={"provider": "example", "name": "example-model"}, presets=[], allowed=True)
        bar.set_composer_controls(self.controls)

    def on_composer_controls_requested(self, event: ComposerControls.Requested):
        control = next(item for item in self.controls if item["id"] == event.control_id)
        self.push_screen(ComposerChoiceScreen(control, anchor_id=event.anchor_id), lambda result: setattr(self, "result", result))


@pytest.mark.parametrize("width", [120, 80, 40])
async def test_popover_is_anchored_and_fits_narrow_terminals(width):
    app = PopoverApp()
    async with app.run_test(size=(width, 28)) as pilot:
        await pilot.pause()
        await pilot.click("#composer-control-0")
        await pilot.pause()
        box = app.screen.query_one("#composer-choice-popover")
        anchor = app.query_one("#composer-control-0")
        assert box.region.x >= 0
        assert box.region.right <= width
        assert box.region.bottom <= anchor.region.y
        assert app.screen.query_one("#composer-choice-custom").visible
        await pilot.click("#composer-choice-option-2")
        assert app.result == ComposerChoice("fast")


async def test_custom_enter_escape_and_read_only_choice_lists():
    app = PopoverApp()
    async with app.run_test(size=(80, 28)) as pilot:
        await pilot.pause()
        await pilot.click("#composer-control-0")
        await pilot.click("#composer-choice-custom")
        await pilot.press(*" custom-value ", "enter")
        assert app.result == ComposerChoice(" custom-value ", True)
        await pilot.click("#composer-control-0")
        await pilot.press("escape")
        assert app.result is None
        app.controls[0].update(custom=False, value="fast")
        await pilot.click("#composer-control-0")
        assert not list(app.screen.query(Input))
        await pilot.click("#composer-choice-option-0")
        assert app.result == ComposerChoice("")


async def test_busy_model_bar_still_allows_controls_and_outside_click_cancels():
    app = PopoverApp()
    async with app.run_test(size=(80, 28)) as pilot:
        await pilot.pause()
        app.query_one(ModelSwitcherBar).set_busy(True)
        assert str(app.query_one("#composer-control-0", Button).label) == "Mode: Auto ▴"
        await pilot.click("#composer-control-0")
        assert isinstance(app.screen, ComposerChoiceScreen)
        app.result = ComposerChoice("fast")
        await pilot.click(offset=(1, 1))
        assert not isinstance(app.screen, ComposerChoiceScreen)
        assert app.result is None


@pytest.mark.parametrize("width", [120, 40])
async def test_two_unrelated_controls_render_refresh_and_remove_independently(width):
    second = {**CONTROL, "id": "another.output", "label": "Output: Text", "title": "Output format", "value": "text", "custom": False,
              "options": [{"value": "text", "label": "Text"}, {"value": "json", "label": "JSON"}]}
    app = PopoverApp(controls=[CONTROL, second])
    async with app.run_test(size=(width, 28)) as pilot:
        await pilot.pause()
        assert len(app.query(".composer-control")) == 2
        app.query_one("#composer-control-0", Button).focus()
        await pilot.press("tab", "enter")
        await pilot.click("#composer-choice-option-1")
        assert app.result == ComposerChoice("json")
        app.controls = [deepcopy(CONTROL)]
        app.query_one(ModelSwitcherBar).set_composer_controls(app.controls)
        await pilot.pause()
        assert len(app.query(".composer-control")) == 1
        app.query_one(ModelSwitcherBar).clear()
        await pilot.pause()
        assert not app.query_one(ComposerControls).display
