"""Shimmer text effect for status lines — terminal equivalent of WebUI shiny-text.

The WebUI uses a CSS gradient sweep (`background-clip: text` + `animation: shine`).
In the terminal we approximate this with per-character color interpolation: a bright
highlight window sweeps across the text on each tick, giving it a "shiny" feel.
"""

from __future__ import annotations

from rich.style import Style
from rich.text import Text

# Shimmer color palette (matches TUI accent tones).
_BASE = (30, 100, 170)  # muted blue — resting state
_PEAK = (215, 230, 245)  # near-white — shimmer highlight


def _lerp(a: int, b: int, t: float) -> int:
    return int(a + (b - a) * t)


def _lerp_color(t: float) -> tuple[int, int, int]:
    return (_lerp(_BASE[0], _PEAK[0], t), _lerp(_BASE[1], _PEAK[1], t), _lerp(_BASE[2], _PEAK[2], t))


def build_shimmer_text(label: str, detail: str, phase: float, spinner_frame: int) -> Text:
    """Build status text with a sweeping shimmer highlight.

    Args:
        label: Activity label (e.g. "Thinking").
        detail: Optional detail (e.g. "A0: Reasoning").
        phase: Shimmer phase 0.0–1.0 (highlight center position).
        spinner_frame: Index into the Braille spinner sequence.

    Returns:
        A ``rich.text.Text`` with per-character shimmer styling.
    """
    detail_part = f" [{detail}]" if detail else ""
    content = f"{label}{detail_part}"

    text = Text()
    width = len(content)
    if width == 0:
        return text

    for i, char in enumerate(content):
        pos = i / max(width - 1, 1)
        # Wrapping distance from highlight center.
        dist = min(
            abs(pos - phase),
            abs(pos - phase + 1.0),
            abs(pos - phase - 1.0),
        )
        # Sharp triangle falloff — highlight window ~30% of text width.
        intensity = max(0.0, 1.0 - dist / 0.15) ** 1.5
        r, g, b = _lerp_color(intensity)
        text.append(char, style=Style(color=f"#{r:02x}{g:02x}{b:02x}"))

    return text


def build_dim_status(label: str, detail: str) -> Text:
    """Build a dim (frozen) status line for history or completed steps.

    Args:
        label: Activity label.
        detail: Optional detail string.

    Returns:
        A ``rich.text.Text`` styled as dim.
    """
    detail_part = f" [{detail}]" if detail else ""
    return Text(f"{label}{detail_part}", style="dim")
