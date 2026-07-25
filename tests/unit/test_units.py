"""ms ↔ samples conversion — the single timebase boundary for the whole pipeline.

rekordbox ANLZ stores cue/beat positions in milliseconds; Engine stores them
in integer samples. Conversion happens exactly once (`units.ms_to_samples`)
at the reader boundary so every IR position is already an int sample count.

Acceptance criterion is ±1 ms. At 44.1 kHz that is 44 samples, so correct
integer arithmetic has a large margin — but only if we do not accumulate
float drift across a full track, and only if rounding is deterministic.

Rounding choice (pinned here, not left implicit): Python 3 `round()` —
round-half-to-even (banker's rounding) — then convert to `int`. Documented
in `units.py`. Tests below fix half-cases so a silent switch to half-up
would fail.
"""

from __future__ import annotations

import pytest

from rb2engine.units import ms_to_samples, samples_to_ms

# ---------------------------------------------------------------------------
# Exact integer results at common sample rates
# ---------------------------------------------------------------------------


def test_zero_ms_is_zero_samples() -> None:
    """Cue at track start must stay sample 0 at every rate."""
    assert ms_to_samples(0.0, 44100) == 0
    assert ms_to_samples(0.0, 48000) == 0


def test_one_second_exact_at_44100() -> None:
    """1000 ms × 44100 / 1000 = 44100 samples exactly (no rounding)."""
    assert ms_to_samples(1000.0, 44100) == 44100


def test_one_second_exact_at_48000() -> None:
    assert ms_to_samples(1000.0, 48000) == 48000


def test_known_cue_position_44100() -> None:
    """1:23.456 → 83456 ms → 83456 * 44100 / 1000 = 3_680_409.6 → 3_680_410.

    Hand-computed: 83456 * 44.1 = 3_680_409.6; half-to-even on .6 rounds away
    from zero to even? 3_680_409.6 is not a half case — standard round →
    3_680_410.
    """
    assert 83456 * 44100 / 1000 == 3_680_409.6
    assert ms_to_samples(83456.0, 44100) == 3_680_410


def test_known_cue_position_48000() -> None:
    """Same wall time at 48 kHz: 83456 * 48 = 4_005_888 exactly."""
    assert ms_to_samples(83456.0, 48000) == 4_005_888


def test_fractional_ms_44100() -> None:
    """1.5 ms at 44.1 kHz = 66.15 samples → 66."""
    assert 1.5 * 44100 / 1000 == 66.15
    assert ms_to_samples(1.5, 44100) == 66


def test_fractional_ms_48000() -> None:
    """1.5 ms at 48 kHz = 72.0 exactly."""
    assert ms_to_samples(1.5, 48000) == 72


# ---------------------------------------------------------------------------
# Rounding pin: half-to-even (Python 3 round)
# ---------------------------------------------------------------------------


def test_half_to_even_rounds_2_5_to_2() -> None:
    """Construct ms, rate so product/1000 == 2.5 → banker's round → 2.

    2.5 ms * 1000 Hz / 1000 = 2.5. Half-up would give 3; half-to-even gives 2.
    """
    assert ms_to_samples(2.5, 1000) == 2


def test_half_to_even_rounds_3_5_to_4() -> None:
    """3.5 → nearest even is 4 (still half-to-even, not always-down)."""
    assert ms_to_samples(3.5, 1000) == 4


def test_half_to_even_at_44100() -> None:
    """Find ms where ms * 44100 / 1000 is exactly k + 0.5.

    ms * 44.1 = n + 0.5 ⇒ ms = (n + 0.5) / 44.1.
    For n = 2: ms = 2.5 / 44.1 = 2500/44100. Using ms = 2500/44100:
    2500/44100 * 44100 / 1000 = 2.5 → rounds to 2.
    """
    ms = 2500 / 44100
    assert ms * 44100 / 1000 == pytest.approx(2.5)
    assert ms_to_samples(ms, 44100) == 2


# ---------------------------------------------------------------------------
# Large values near a full track — no float drift
# ---------------------------------------------------------------------------


def test_ten_minute_track_44100_exact() -> None:
    """10 min = 600_000 ms × 44100 / 1000 = 26_460_000 exactly.

    A naive float path that loses integer exactness would land off-by-one
    and fail the 0-sample verify gate even though we are well inside ±1 ms.
    """
    ms = 600_000.0
    expected = 26_460_000
    assert ms * 44100 / 1000 == expected
    assert ms_to_samples(ms, 44100) == expected


def test_ten_minute_track_48000_exact() -> None:
    assert ms_to_samples(600_000.0, 48000) == 28_800_000


def test_long_track_almost_hour_44100() -> None:
    """~1 hour: 3_600_000 ms → 158_760_000 samples at 44.1 kHz (exact)."""
    assert ms_to_samples(3_600_000.0, 44100) == 158_760_000


def test_sub_millisecond_does_not_invent_samples_from_noise() -> None:
    """Very small positive ms still converts; 0.0 stays 0 (already tested)."""
    # 0.01 ms at 44100 = 0.441 samples → 0
    assert ms_to_samples(0.01, 44100) == 0
    # 0.02 ms at 44100 = 0.882 → 1? 0.882 rounds to 1? No, 0.882 → 1 under
    # round half... actually round(0.882) = 1 in Python? round uses banker's
    # only on .5; 0.882 → 1.
    assert ms_to_samples(0.02, 44100) == 1


# ---------------------------------------------------------------------------
# Inverse
# ---------------------------------------------------------------------------


def test_samples_to_ms_inverse_of_exact_second() -> None:
    assert samples_to_ms(44100, 44100) == pytest.approx(1000.0)
    assert samples_to_ms(48000, 48000) == pytest.approx(1000.0)


def test_samples_to_ms_zero() -> None:
    assert samples_to_ms(0, 44100) == 0.0


def test_roundtrip_within_half_sample() -> None:
    """ms → samples → ms stays within half a sample of the original.

    Not a free pass for any rounding policy: half a sample at 44.1 kHz is
    ~0.011 ms, far under the ±1 ms acceptance bar. Locks that the inverse
    matches the forward scale factor.
    """
    for ms, rate in (
        (0.0, 44100),
        (1.5, 44100),
        (83456.0, 44100),
        (83456.0, 48000),
        (600_000.0, 44100),
    ):
        samples = ms_to_samples(ms, rate)
        back = samples_to_ms(samples, rate)
        # half-sample in ms
        half_sample_ms = 500.0 / rate
        assert abs(back - ms) <= half_sample_ms + 1e-9
