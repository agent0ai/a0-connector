"""Regression tests: programmatic ModelSwitcherBar updates must not emit PresetChanged."""
from __future__ import annotations

import asyncio

import pytest
from textual.app import App, ComposeResult

from agent_zero_cli.widgets.model_switcher_bar import ModelSwitcherBar


class HarnessApp(App):
    def __init__(self):
        super().__init__()
        self.preset_changed_events: list[str] = []

    def compose(self) -> ComposeResult:
        yield ModelSwitcherBar(id="model-switcher-bar")

    def on_model_switcher_bar_preset_changed(self, event: ModelSwitcherBar.PresetChanged) -> None:
        self.preset_changed_events.append(event.value)


@pytest.mark.asyncio
async def test_programmatic_set_state_emits_no_preset_changed():
    """set_state() must not produce phantom PresetChanged events (auto-cycling bug)."""
    app = HarnessApp()
    async with app.run_test() as pilot:
        bar = app.query_one(ModelSwitcherBar)
        bar.set_state(
            main_model={"provider": "openai", "name": "gpt-4.1", "label": "openai/gpt-4.1"},
            presets=["fast", "fast_reasoning", "thorough"],
            allowed=True,
            selected_preset="fast_reasoning",  # not the first option
            configured_preset="thorough",
        )
        await pilot.pause()
        await asyncio.sleep(0.2)
        await pilot.pause()
    assert app.preset_changed_events == [], (
        f"BUG: programmatic set_state() triggered preset switches: {app.preset_changed_events}"
    )


@pytest.mark.asyncio
async def test_clear_emits_no_preset_changed():
    """clear() must not produce phantom PresetChanged events either."""
    app = HarnessApp()
    async with app.run_test() as pilot:
        bar = app.query_one(ModelSwitcherBar)
        bar.set_state(
            main_model={"provider": "openai", "name": "gpt-4.1", "label": "openai/gpt-4.1"},
            presets=["fast", "fast_reasoning", "thorough"],
            allowed=True,
            selected_preset="fast_reasoning",
            configured_preset="thorough",
        )
        await pilot.pause()
        app.preset_changed_events.clear()
        bar.clear()
        await pilot.pause()
        await asyncio.sleep(0.2)
        await pilot.pause()
    assert app.preset_changed_events == [], (
        f"BUG: clear() triggered preset switches: {app.preset_changed_events}"
    )


@pytest.mark.asyncio
async def test_genuine_user_selection_still_emits():
    """A real user selection must still emit exactly one PresetChanged (no over-suppression)."""
    app = HarnessApp()
    async with app.run_test() as pilot:
        bar = app.query_one(ModelSwitcherBar)
        bar.set_state(
            main_model={"provider": "openai", "name": "gpt-4.1", "label": "openai/gpt-4.1"},
            presets=["fast", "fast_reasoning", "thorough"],
            allowed=True,
            selected_preset="fast_reasoning",
            configured_preset="thorough",
        )
        await pilot.pause()
        app.preset_changed_events.clear()
        # Simulate a genuine user pick
        bar._preset.value = "thorough"
        await pilot.pause()
        await asyncio.sleep(0.2)
        await pilot.pause()
    assert app.preset_changed_events == ["thorough"], (
        f"Expected exactly one PresetChanged for user selection, got: {app.preset_changed_events}"
    )
