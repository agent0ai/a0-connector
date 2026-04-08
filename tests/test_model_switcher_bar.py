from __future__ import annotations

from types import SimpleNamespace

from agent_zero_cli.widgets.model_switcher_bar import (
    ModelSwitcherBar,
    _should_stack_summary_rows,
    _show_preset_for_width,
)


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


def test_summary_rows_stay_inline_when_width_is_sufficient() -> None:
    assert _should_stack_summary_rows(
        68,
        main_model_text="anthropic/claude-haiku-4-5",
        utility_model_text="anthropic/claude-haiku-4-5",
    ) is False


def test_summary_rows_stack_when_inline_layout_does_not_fit() -> None:
    assert _should_stack_summary_rows(
        67,
        main_model_text="anthropic/claude-haiku-4-5",
        utility_model_text="anthropic/claude-haiku-4-5",
    ) is True
