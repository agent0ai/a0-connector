from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Button

from agent_zero_cli.widgets.model_switcher_bar import (
    ModelSwitcherBar,
    _show_preset_for_width,
)


pytestmark = pytest.mark.anyio


class ModelSwitcherBarHarness(App[None]):
    CSS_PATH = str(Path(__file__).resolve().parents[1] / "src/agent_zero_cli/styles/app.tcss")

    def compose(self) -> ComposeResult:
        yield ModelSwitcherBar(id="model-switcher-bar")


def _select_event(value: str, *, widget_id: str = "model-switcher-preset") -> SimpleNamespace:
    return SimpleNamespace(
        select=SimpleNamespace(id=widget_id),
        value=value,
    )


def test_select_changed_emits_message_for_new_value() -> None:
    bar = ModelSwitcherBar(id="model-switcher-bar")
    posted: list[object] = []
    bar.post_message = lambda message: posted.append(message)  # type: ignore[method-assign]

    bar.on_select_changed(_select_event("Max Power"))

    assert len(posted) == 1
    assert posted[0].value == "Max Power"


def test_select_changed_ignores_events_while_busy() -> None:
    bar = ModelSwitcherBar(id="model-switcher-bar")
    posted: list[object] = []
    bar.post_message = lambda message: posted.append(message)  # type: ignore[method-assign]
    bar._selected_value = "Max Power"
    bar.set_busy(True)

    bar.on_select_changed(_select_event(""))

    assert posted == []
    assert bar._selected_value == "Max Power"


def test_select_changed_ignores_same_value() -> None:
    bar = ModelSwitcherBar(id="model-switcher-bar")
    posted: list[object] = []
    bar.post_message = lambda message: posted.append(message)  # type: ignore[method-assign]
    bar._selected_value = "Max Power"

    bar.on_select_changed(_select_event("Max Power"))

    assert posted == []


def test_show_preset_for_width_hides_selector_on_narrow_layout() -> None:
    assert _show_preset_for_width(70) is False


def test_show_preset_for_width_keeps_selector_on_wide_layout() -> None:
    assert _show_preset_for_width(100) is True


def test_main_button_emits_model_config_request() -> None:
    bar = ModelSwitcherBar(id="model-switcher-bar")
    posted: list[object] = []
    bar.post_message = lambda message: posted.append(message)  # type: ignore[method-assign]

    bar.on_button_pressed(SimpleNamespace(button=SimpleNamespace(id="model-switcher-main")))

    assert len(posted) == 1
    assert posted[0].target == "main"


async def test_label_growth_remeasures_model_buttons_without_resize() -> None:
    app = ModelSwitcherBarHarness()

    async with app.run_test(size=(110, 20)) as pilot:
        bar = app.query_one("#model-switcher-bar", ModelSwitcherBar)
        bar.set_state(
            main_model={"provider": "openai", "name": "gpt-4.1"},
            utility_model={"provider": "openai", "name": "gpt-4.1-mini"},
            presets=[{"name": "Balanced"}],
            allowed=True,
            selected_preset="Balanced",
        )
        await pilot.pause(0.1)

        main_button = bar.query_one("#model-switcher-main", Button)
        utility_button = bar.query_one("#model-switcher-utility", Button)
        short_main_width = main_button.size.width
        short_utility_width = utility_button.size.width

        bar.set_state(
            main_model={"provider": "anthropic", "name": "claude-sonnet-4"},
            utility_model={"provider": "anthropic", "name": "claude-haiku-4-5"},
            presets=[{"name": "Balanced"}],
            allowed=True,
            selected_preset="Balanced",
        )
        await pilot.pause(0.1)

        assert main_button.label == "Main anthropic/claude-sonnet-4"
        assert utility_button.label == "Utility anthropic/claude-haiku-4-5"
        assert main_button.size.width > short_main_width
        assert utility_button.size.width > short_utility_width


async def test_label_shrink_remeasures_model_buttons_without_resize() -> None:
    app = ModelSwitcherBarHarness()

    async with app.run_test(size=(110, 20)) as pilot:
        bar = app.query_one("#model-switcher-bar", ModelSwitcherBar)
        bar.set_state(
            main_model={"provider": "anthropic", "name": "claude-haiku-4-5"},
            utility_model={"provider": "anthropic", "name": "claude-haiku-4-5"},
            presets=[{"name": "Balanced"}],
            allowed=True,
            selected_preset="Balanced",
        )
        await pilot.pause(0.1)

        main_button = bar.query_one("#model-switcher-main", Button)
        utility_button = bar.query_one("#model-switcher-utility", Button)
        long_main_width = main_button.size.width
        long_utility_width = utility_button.size.width

        bar.set_state(
            main_model={"provider": "openai", "name": "gpt-4.1"},
            utility_model={"provider": "openai", "name": "gpt-4.1-mini"},
            presets=[{"name": "Balanced"}],
            allowed=True,
            selected_preset="Balanced",
        )
        await pilot.pause(0.1)

        assert main_button.label == "Main openai/gpt-4.1"
        assert utility_button.label == "Utility openai/gpt-4.1-mini"
        assert main_button.size.width < long_main_width
        assert utility_button.size.width < long_utility_width
