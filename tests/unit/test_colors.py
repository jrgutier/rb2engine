"""Unit tests for rekordbox color_id → RGB palette (reader/colors.py).

Why: PCOB / memory-cue color_id is a palette index, not RGB. When PCO2
explicit RGB is unavailable the DJ still saw a pad colour in rekordbox; a
wrong mapping actively misleads under club lights. Unknown ids must never
be guessed — None + warning only.
"""

from __future__ import annotations

import logging

import pytest

from rb2engine.ir import RGB
from rb2engine.reader.colors import color_id_to_rgb

# Documented standard hot-cue / memory-cue palette (ids 1–8).
# RGB values match Deep Symmetry Beat Link ColorItem.colorForId, which uses
# Java AWT Color constants for 1–7 and (128, 0, 128) for purple (8).
DOCUMENTED_PALETTE: dict[int, RGB] = {
    1: RGB(255, 175, 175),  # Pink  — java.awt.Color.PINK
    2: RGB(255, 0, 0),  # Red    — java.awt.Color.RED
    3: RGB(255, 200, 0),  # Orange — java.awt.Color.ORANGE
    4: RGB(255, 255, 0),  # Yellow — java.awt.Color.YELLOW
    5: RGB(0, 255, 0),  # Green  — java.awt.Color.GREEN
    6: RGB(0, 255, 255),  # Aqua   — java.awt.Color.CYAN
    7: RGB(0, 0, 255),  # Blue   — java.awt.Color.BLUE
    8: RGB(128, 0, 128),  # Purple — Color(128, 0, 128)
}


def test_every_documented_id_maps_to_distinct_rgb() -> None:
    """Ids 1–8 must each map to a unique RGB the DJ actually saw.

    Distinctness is load-bearing: if two pads collapse to the same colour,
    pad muscle-memory under lights breaks.
    """
    seen: set[tuple[int, int, int]] = set()
    for color_id, expected in DOCUMENTED_PALETTE.items():
        got = color_id_to_rgb(color_id)
        assert got == expected, f"id {color_id}: expected {expected}, got {got}"
        assert got is not None
        key = (got.r, got.g, got.b)
        assert key not in seen, f"id {color_id} collides with another palette entry"
        seen.add(key)
    assert len(seen) == 8


def test_color_id_zero_means_no_colour() -> None:
    """0 is the explicit 'no colour' sentinel — None, not black, not green.

    Older CDJs defaulted missing hot-cue colour to green; we must not invent
    that default when the export says no colour.
    """
    assert color_id_to_rgb(0) is None


def test_unknown_color_id_returns_none_and_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Unknown id → None + warning; never a guessed colour.

    Guessing the wrong pad colour is worse than leaving the pad uncoloured.
    """
    with caplog.at_level(logging.WARNING):
        result = color_id_to_rgb(99)

    assert result is None
    assert any(
        "unknown" in r.getMessage().lower() or "color_id" in r.getMessage().lower()
        for r in caplog.records
    )


def test_unknown_negative_id_returns_none(caplog: pytest.LogCaptureFixture) -> None:
    """Negative ids are corrupt data — not a palette entry."""
    with caplog.at_level(logging.WARNING):
        assert color_id_to_rgb(-1) is None


def test_zero_does_not_warn(caplog: pytest.LogCaptureFixture) -> None:
    """0 is a normal 'no colour' value; must not spam unknown-id warnings."""
    with caplog.at_level(logging.WARNING):
        color_id_to_rgb(0)
    assert not any("unknown" in r.getMessage().lower() for r in caplog.records)


@pytest.mark.parametrize("color_id,expected", list(DOCUMENTED_PALETTE.items()))
def test_each_palette_entry_individually(color_id: int, expected: RGB) -> None:
    """Parametrized check so a single wrong channel fails with a clear id."""
    assert color_id_to_rgb(color_id) == expected
