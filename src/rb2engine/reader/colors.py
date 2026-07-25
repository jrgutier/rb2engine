"""rekordbox color_id → RGB palette (PCOB / memory-cue fallback).

PCOB cue entries and PCO2 memory-cue ``color_id`` fields store a **palette
index**, not RGB. Only the extended hot-cue fields on ``PCO2`` carry explicit
``color_red`` / ``color_green`` / ``color_blue``. When those are unavailable,
:func:`color_id_to_rgb` maps the index to the colour the DJ saw in rekordbox.

**RGB source (documented standard 8-colour palette, ids 1–8):**

Deep Symmetry Beat Link
``org.deepsymmetry.beatlink.data.ColorItem.colorForId``
(https://github.com/Deep-Symmetry/beat-link), which is the community-verified
table for the export.pdb / memory-cue colour rows:

| id | Name   | RGB (r, g, b)   | Provenance              |
|----|--------|-----------------|-------------------------|
| 0  | none   | — (returns None)| sentinel                |
| 1  | Pink   | (255, 175, 175) | java.awt.Color.PINK     |
| 2  | Red    | (255, 0, 0)     | java.awt.Color.RED      |
| 3  | Orange | (255, 200, 0)   | java.awt.Color.ORANGE   |
| 4  | Yellow | (255, 255, 0)   | java.awt.Color.YELLOW   |
| 5  | Green  | (0, 255, 0)     | java.awt.Color.GREEN    |
| 6  | Aqua   | (0, 255, 255)   | java.awt.Color.CYAN     |
| 7  | Blue   | (0, 0, 255)     | java.awt.Color.BLUE     |
| 8  | Purple | (128, 0, 128)   | ``new Color(128,0,128)``|

Unknown ids return ``None`` and log a warning — **never a guessed colour**.
A wrong pad colour actively misleads a DJ reaching for a cue under lights.

Note: PCO2 hot-cue ``color_code`` (0x01–0x3e) is a different, denser palette
(``CueList.findRekordboxColor``). This module covers the **standard 8-colour**
``color_id`` table only.
"""

from __future__ import annotations

import logging

from rb2engine.ir import RGB

logger = logging.getLogger(__name__)

# Standard rekordbox 8-colour palette (see module docstring for provenance).
_PALETTE: dict[int, RGB] = {
    1: RGB(255, 175, 175),  # Pink
    2: RGB(255, 0, 0),  # Red
    3: RGB(255, 200, 0),  # Orange
    4: RGB(255, 255, 0),  # Yellow
    5: RGB(0, 255, 0),  # Green
    6: RGB(0, 255, 255),  # Aqua
    7: RGB(0, 0, 255),  # Blue
    8: RGB(128, 0, 128),  # Purple
}


def color_id_to_rgb(color_id: int) -> RGB | None:
    """Map a rekordbox palette ``color_id`` to RGB.

    Parameters
    ----------
    color_id:
        0 means no colour. 1–8 are the standard palette. Any other value is
        unknown.

    Returns
    -------
    RGB | None
        Palette colour, or ``None`` for 0 / unknown. Unknown ids also emit a
        warning; 0 does not.
    """
    if color_id == 0:
        return None
    rgb = _PALETTE.get(color_id)
    if rgb is not None:
        return rgb
    logger.warning(
        "Unknown rekordbox color_id=%s; leaving cue uncoloured (not guessing)",
        color_id,
    )
    return None
