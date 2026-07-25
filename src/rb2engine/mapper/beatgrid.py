"""PQTZ/PQT2 → compressed Engine beat markers; -4 normalization; default vs adjusted grids.

rekordbox lists EVERY beat (~550 for a 6-min track). Engine wants SPARSE markers
with tempo implied by (Δsample_offset)/(Δbeat_number) between adjacent markers.

Algorithm (aligned with libdjinterop normalize_beatgrid + convert write path):
1. Collapse constant-tempo runs to endpoints; emit a marker at each BPM change.
2. Shift the first marker so its beat index is -4 (Engine convention).
3. Extrapolate the last marker to the first beat past total_samples.
4. number_of_beats on each non-final marker = next.beat_number - this.beat_number.

Downbeat: when the dense list starts on beat_in_bar==1, indices after -4
normalization keep downbeats on beat_number ≡ 0 (mod 4).
"""

from __future__ import annotations

import math

from rb2engine.ir import SourceBeat, SourceBeatgrid
from rb2engine.ir_engine import EngineBeatGrid, EngineBeatMarker

# BPM stored as centi-bpm floats from ANLZ; equality within this is "same tempo".
_BPM_EPS = 1e-4


def compress_beatgrid(
    grid: SourceBeatgrid,
    *,
    sample_rate: int,
    total_samples: int | None,
) -> EngineBeatGrid:
    """Compress a dense rekordbox beat list into Engine sparse markers.

    Parameters
    ----------
    grid:
        Source beatgrid (PQTZ beats; ``is_adjusted`` from PQT2 presence).
    sample_rate:
        Track sample rate in Hz (for tempo math / fallbacks).
    total_samples:
        Exact sample count for end-of-track extrapolation; ``None`` falls back
        to the last beat offset so mapping still succeeds without mutagen.
    """
    del sample_rate  # reserved for future absolute-BPM reconstruction paths
    if not grid.beats:
        return EngineBeatGrid(
            default_markers=[],
            adjusted_markers=[],
            is_beatgrid_set=False,
        )

    sparse = _compress_dense(grid.beats)
    sample_count = _resolve_sample_count(sparse, total_samples)
    normalized = _normalize_beatgrid(sparse, sample_count)
    markers = _with_number_of_beats(normalized)

    # Writer policy: adjusted grid must never be empty when a grid exists.
    # Reader only supplies one dense list (PQTZ); is_adjusted is a flag only.
    return EngineBeatGrid(
        default_markers=markers,
        adjusted_markers=list(markers),
        is_beatgrid_set=True,
    )


def _same_tempo(a: SourceBeat, b: SourceBeat) -> bool:
    return abs(a.bpm - b.bpm) <= _BPM_EPS


def _compress_dense(beats: list[SourceBeat]) -> list[EngineBeatMarker]:
    """Keep first, last, and any beat where BPM changes from the previous."""
    n = len(beats)
    if n == 1:
        b = beats[0]
        return [
            EngineBeatMarker(
                sample_offset=float(b.sample_offset),
                beat_number=0,
                number_of_beats=0,
                unknown=0,
            )
        ]

    keep: list[int] = [0]
    for i in range(1, n):
        # Boundary: first beat of a new tempo.
        if not _same_tempo(beats[i - 1], beats[i]) and keep[-1] != i:
            keep.append(i)
    if keep[-1] != n - 1:
        keep.append(n - 1)

    return [
        EngineBeatMarker(
            sample_offset=float(beats[i].sample_offset),
            beat_number=i,  # dense index — absolute beat count from start
            number_of_beats=0,
            unknown=0,
        )
        for i in keep
    ]


def _resolve_sample_count(
    markers: list[EngineBeatMarker], total_samples: int | None
) -> int:
    if total_samples is not None and total_samples > 0:
        return int(total_samples)
    # Fallback: treat last marker as the track end so we still extrapolate +1 beat.
    last = markers[-1].sample_offset
    return max(math.ceil(last), 1)


def _normalize_beatgrid(
    markers: list[EngineBeatMarker], sample_count: int
) -> list[EngineBeatMarker]:
    """libdjinterop ``normalize_beatgrid`` semantics (Engine -4 / past-end)."""
    if not markers:
        return []

    # Work on a mutable copy of (offset, index).
    work: list[tuple[float, int]] = [
        (m.sample_offset, int(m.beat_number)) for m in markers
    ]

    # Drop markers strictly after the first one past sample_count.
    last_past = None
    for i, (off, _) in enumerate(work):
        if off > sample_count:
            last_past = i
            break
    if last_past is not None:
        work = work[: last_past + 1]

    # Drop leading markers before the last one with sample_offset <= 0.
    after_first = None
    for i, (off, _) in enumerate(work):
        if off > 0:
            after_first = i
            break
    if after_first is not None and after_first > 0:
        work = work[after_first - 1 :]

    if len(work) < 2:
        # Single-marker (or empty after trim): synthesize a 1-beat end so Engine
        # still gets a 2-point grid rather than a misplaced single point.
        if not work:
            return []
        off0, idx0 = work[0]
        # Assume 120 BPM placeholder only if we truly lack a second point —
        # prefer extending by one index unit with zero sample delta avoided by
        # a minimal positive step derived from a default 120 BPM at 44.1k.
        # Callers with real grids always have ≥2 dense beats.
        work = [(off0, idx0), (off0 + 1.0, idx0 + 1)]

    # First marker → beat index -4, sample shifted earlier by the same number of beats.
    spb = (work[1][0] - work[0][0]) / (work[1][1] - work[0][1])
    if spb == 0:
        spb = 1.0  # degenerate guard
    new_first_off = work[0][0] - (4 + work[0][1]) * spb
    work[0] = (new_first_off, -4)

    # Last marker → first beat past usable end of track.
    last = len(work) - 1
    spb_end = (work[last][0] - work[last - 1][0]) / (
        work[last][1] - work[last - 1][1]
    )
    if spb_end == 0:
        spb_end = spb if spb != 0 else 1.0
    index_adjustment = math.ceil((sample_count - work[last][0]) / spb_end)
    # Ensure we always clear the track end when still inside (ceil(0)==0).
    if index_adjustment == 0 and work[last][0] <= sample_count:
        index_adjustment = 1
    work[last] = (
        work[last][0] + index_adjustment * spb_end,
        work[last][1] + index_adjustment,
    )

    return [
        EngineBeatMarker(
            sample_offset=off,
            beat_number=idx,
            number_of_beats=0,
            unknown=0,
        )
        for off, idx in work
    ]


def _with_number_of_beats(
    markers: list[EngineBeatMarker],
) -> list[EngineBeatMarker]:
    """Fill number_of_beats as (next.beat_number - this.beat_number); last = 0."""
    if not markers:
        return []
    out: list[EngineBeatMarker] = []
    for i, m in enumerate(markers):
        if i + 1 < len(markers):
            nbeats = int(markers[i + 1].beat_number - m.beat_number)
        else:
            nbeats = 0
        out.append(
            EngineBeatMarker(
                sample_offset=m.sample_offset,
                beat_number=m.beat_number,
                number_of_beats=nbeats,
                unknown=0,
            )
        )
    return out
