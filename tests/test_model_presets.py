from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Checkbox, Input, Select, Static

from agent_zero_cli.model_config import apply_model_switcher_state
from agent_zero_cli.screens.model_presets import (
    ModelPresetsResult,
    ModelPresetsScreen,
    _coerce_model_preset,
    _render_preset_details,
)
from agent_zero_cli.screens.model_runtime import ModelRuntimeResult, ModelRuntimeScreen
from agent_zero_cli.widgets.model_switcher_bar import ModelSwitcherBar, _preset_options


pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("separate", [False, True], ids=["main-without-vision", "separate-vision"])
async def test_vision_fields_follow_main_first_routing_and_preserve_hidden_values(separate: bool) -> None:
    app = App()
    results = []
    screen = ModelRuntimeScreen(
        main_model={"provider": "openai", "name": "main"},
        utility_model={"provider": "openai", "name": "utility"},
    )
    async with app.run_test(size=(100, 65)) as pilot:
        await app.push_screen(screen, results.append)
        await pilot.pause()
        supports = screen.query_one("#model-runtime-supports-vision", Checkbox)
        override = screen.query_one("#model-runtime-separate-vision", Checkbox)
        fields = screen.query_one("#model-runtime-vision-section")
        assert supports.value and override.display and not override.value
        assert not fields.display

        if separate:
            override.value = True
        else:
            supports.value = False
        await pilot.pause()
        assert fields.display
        assert override.display == separate
        screen.query_one("#model-runtime-vision-provider", Select).value = "openai"
        screen.query_one("#model-runtime-vision-name", Input).value = "sidecar"

        supports.value = True
        override.value = False
        await pilot.pause()
        assert not fields.display
        assert screen.query_one("#model-runtime-vision-name", Input).value == "sidecar"
        supports.value = separate
        override.value = separate
        await pilot.pause()
        screen.action_apply()
        await pilot.pause()

    assert results[0].main_vision == (None if separate else False)
    assert results[0].vision_model == {
        "provider": "openai", "name": "sidecar", "vision": True, "override_main": separate,
    }
    assert not results[0].main_changed and not results[0].utility_changed


async def test_vision_editor_reopens_saved_settings_validates_and_clears_sidecar() -> None:
    app = App()
    results = []
    screen = ModelRuntimeScreen(
        main_model={"provider": "openai", "name": "main"},
        supports_vision=False,
        vision_model={"provider": "custom", "name": "sidecar", "override_main": False},
    )
    async with app.run_test(size=(100, 65)) as pilot:
        await app.push_screen(screen, results.append)
        await pilot.pause()
        assert screen.query_one("#model-runtime-vision-section").display
        assert not screen.query_one("#model-runtime-separate-vision").display
        screen.query_one("#model-runtime-vision-name", Input).value = ""
        screen.action_apply()
        await pilot.pause()
        assert not results
        screen.query_one("#model-runtime-vision-provider", Select).value = Select.NULL
        screen.action_apply()
        await pilot.pause()
    assert results[0].vision_model == {}


@pytest.mark.parametrize(("native", "override", "expected"), [
    (True, False, "Main model (native vision)"),
    (False, False, "openai/sidecar"),
    (True, True, "openai/sidecar"),
])
def test_preset_details_show_vision_routing(native: bool, override: bool, expected: str) -> None:
    preset = _coerce_model_preset({
        "name": "Power", "chat": {"provider": "openai", "name": "main", "vision": native},
        "vision": {"provider": "openai", "name": "sidecar", "override_main": override},
    })
    assert f"Vision: {expected}" in _render_preset_details(preset).plain


async def test_model_editor_provider_enter_selects_before_saving() -> None:
    app = App()
    results = []
    screen = ModelRuntimeScreen(
        preset_name="Power",
        main_model={"provider": "openai", "name": "old", "api_base": "https://example.test/v1"},
        utility_model={"provider": "openai", "name": "utility", "api_key": "existing-key"},
        provider_options=[("OpenAI", "openai"), ("Anthropic", "anthropic")],
    )
    async with app.run_test(size=(100, 65)) as pilot:
        await app.push_screen(screen, results.append)
        await pilot.pause()
        await pilot.press("space", "down", "enter")
        await pilot.pause()
        assert not results
        assert screen.query_one("#model-runtime-main-provider", Select).value == "anthropic"
        screen.query_one("#model-runtime-main-name", Input).value = "new"
        screen.query_one("#model-runtime-main-base-url", Input).value = ""
        await pilot.press("ctrl+s")
        await pilot.pause()

    assert results == [ModelRuntimeResult(
        main_model={"provider": "anthropic", "name": "new"},
        utility_model={"provider": "openai", "name": "utility"},
        main_changed=True,
        utility_changed=False,
    )]


class ModelPresetsHarness(App[None]):
    def __init__(self) -> None:
        super().__init__()
        self.result: ModelPresetsResult | None | str = "pending"

    def compose(self) -> ComposeResult:
        yield Static("base")

    async def on_mount(self) -> None:
        self.push_screen(
            ModelPresetsScreen(
                [
                    {"name": "fast", "label": "Fast"},
                    {"name": "deep", "label": "Deep"},
                ],
                current_preset="fast",
            ),
            self._capture,
        )

    def _capture(self, result: ModelPresetsResult | None) -> None:
        self.result = result


class ModelSwitcherHarness(App[None]):
    def __init__(self) -> None:
        super().__init__()
        self.preset_changes: list[str] = []

    def compose(self) -> ComposeResult:
        yield ModelSwitcherBar(id="model-switcher-bar")

    def on_mount(self) -> None:
        self.query_one(ModelSwitcherBar).set_state(
            main_model={"provider": "old", "name": "old-model"},
            presets=[{"name": "Old"}, {"name": "New"}],
            allowed=True,
            selected_preset="Old",
            configured_preset="Old",
        )

    def on_model_switcher_bar_preset_changed(self, event: ModelSwitcherBar.PresetChanged) -> None:
        self.preset_changes.append(event.value)
        if len(self.preset_changes) == 1:
            event.bar.set_state(
                main_model={"provider": "new", "name": "new-model"},
                presets=[{"name": "Old"}, {"name": "New"}],
                allowed=True,
                selected_preset="New",
                configured_preset="Old",
                override_active=True,
            )


async def test_model_presets_keyboard_enter_applies_highlighted_dropdown_choice() -> None:
    app = ModelPresetsHarness()

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.2)
        await pilot.press("space")
        await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause(0.2)

    assert isinstance(app.result, ModelPresetsResult)
    assert app.result.preset_name == "deep"


async def test_model_switcher_ignores_stale_events_from_programmatic_refresh() -> None:
    app = ModelSwitcherHarness()

    async with app.run_test(size=(100, 20)) as pilot:
        await pilot.pause()
        await pilot.click("#model-switcher-preset")
        await pilot.press("down", "enter")
        await pilot.pause()

    assert app.preset_changes == ["New"]


def test_unified_preset_payload_uses_effective_name_and_clear_copy() -> None:
    _, state = apply_model_switcher_state(
        {
            "allowed": True,
            "override": {"preset_name": "Power"},
            "configured_preset": "Default",
            "effective_preset": "Power",
            "presets": [{"name": "Default"}, {"name": "Power"}],
            "main_model": {"provider": "openrouter", "name": "openai/gpt-5.6-sol"},
        }
    )

    assert state["selected_preset"] == "Power"
    assert state["configured_preset"] == "Default"
    assert _preset_options(
        state["presets"],
        configured_preset="Default",
        include_settings=True,
    )[0] == ("Use preset from settings (Default)", "")


def test_model_preset_details_include_embedding_model() -> None:
    preset = _coerce_model_preset(
        {
            "name": "Power",
            "chat": {"provider": "openrouter", "name": "openai/gpt-5.6-sol"},
            "utility": {"provider": "openrouter", "name": "openai/gpt-5.6-luna"},
            "embedding": {"provider": "openai", "name": "text-embedding-3-large"},
        }
    )

    assert "Embedding model: openai/text-embedding-3-large" in _render_preset_details(preset).plain
