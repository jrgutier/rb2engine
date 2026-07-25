"""Leaf unit conversions (e.g. ms_to_samples). Pure arithmetic; no internal imports.

Positions cross the reader boundary exactly once: rekordbox milliseconds →
integer samples. Downstream IR, mapper, and writer all work in samples.

Rounding
--------
`ms_to_samples` uses Python 3 `round()` (round-half-to-even / banker's
rounding) on `ms * sample_rate / 1000.0`, then converts to `int`. This is
pinned by `tests/unit/test_units.py` half-cases so a switch to half-up
would fail CI deliberately rather than drift silently.

At 44.1 kHz, ±1 ms ≈ 44 samples; half-sample ambiguity is well inside the
project acceptance bar. Exact integer millisecond boundaries (1000 ms,
600_000 ms, …) remain bit-exact at both 44100 and 48000.
"""

from __future__ import annotations


def ms_to_samples(ms: float, sample_rate: int) -> int:
    """Convert a rekordbox millisecond position to an integer sample offset.

    Parameters
    ----------
    ms:
        Position in milliseconds (may be fractional).
    sample_rate:
        Audio sample rate in Hz (typically 44100 or 48000 from pdb).

    Returns
    -------
    Non-negative sample index for non-negative `ms`. Negative `ms` rounds
    toward the same half-to-even rule as Python `round`.
    """
    return round(ms * sample_rate / 1000.0)


def samples_to_ms(samples: int, sample_rate: int) -> float:
    """Inverse of `ms_to_samples` for diagnostics and verify diffs.

    Returns a float millisecond position. Round-tripping through
    `ms_to_samples` is exact for values that land on sample boundaries;
    otherwise the forward conversion's half-to-even step dominates.
    """
    return samples * 1000.0 / sample_rate
