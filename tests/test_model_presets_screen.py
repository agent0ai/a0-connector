from __future__ import annotations

from agent_zero_cli.screens.model_presets import (
    _coerce_model_preset,
    _coerce_preset_list,
    _render_default_details,
    _render_preset_details,
)


def test_coerce_model_preset_extracts_main_and_utility_models() -> None:
    preset = _coerce_model_preset(
        {
            "name": "Balanced",
            "label": "Balanced profile",
            "description": "Default everyday profile.",
            "chat": {"provider": "anthropic", "name": "claude-sonnet-4"},
            "utility": {"provider": "anthropic", "name": "claude-haiku-4-5"},
        }
    )

    assert preset.name == "Balanced"
    assert preset.label == "Balanced profile"
    assert preset.description == "Default everyday profile."
    assert preset.main_model == "anthropic/claude-sonnet-4"
    assert preset.utility_model == "anthropic/claude-haiku-4-5"


def test_coerce_preset_list_deduplicates_by_name() -> None:
    presets = _coerce_preset_list(
        [
            {"name": "Balanced", "chat": {"provider": "x", "name": "y"}},
            {"name": "Balanced", "chat": {"provider": "a", "name": "b"}},
            {"name": "Fast", "chat": {"provider": "m", "name": "n"}},
        ]
    )

    assert [preset.name for preset in presets] == ["Balanced", "Fast"]


def test_render_default_details_mentions_default_models() -> None:
    details = _render_default_details()
    rendered = details.plain

    assert "Default LLM" in rendered
    assert "Main model: Connector default" in rendered
    assert "Utility model: Connector default" in rendered


def test_render_preset_details_mentions_main_and_utility() -> None:
    preset = _coerce_model_preset(
        {
            "name": "Fast",
            "chat": {"provider": "anthropic", "name": "claude-haiku-4-5"},
            "utility": {"provider": "anthropic", "name": "claude-haiku-4-5"},
        }
    )
    details = _render_preset_details(preset)
    rendered = details.plain

    assert "Fast" in rendered
    assert "Main model: anthropic/claude-haiku-4-5" in rendered
    assert "Utility model: anthropic/claude-haiku-4-5" in rendered
