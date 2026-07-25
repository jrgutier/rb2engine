"""Pad merge policy, loop routing (U3: loop frees pad), and loop-slot overflow.

Pure functions over plain data — no I/O. Local dataclasses stand in for the
shared IR types (another worker owns ir.py / ir_engine.py).

Policy (priority order):
1. Any entry with an end position is a LOOP → loop slots only (no pad).
2. Hot point-cues map 1:1 onto their original pad (muscle memory).
3. Memory point-cues fill remaining pads chronologically by start_sample.
4. Excess point-cues → dropped, reason_code=pad_slots_exhausted.
5. Loop slots: hot-cue loops by pad order, then memory loops chronologically;
   excess → reason_code=loop_slots_exhausted.
6. Both stores always length 8, unused slots = empty sentinel.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum

NUM_SLOTS = 8
EMPTY_SAMPLE = -1.0
EMPTY_ARGB: tuple[int, int, int, int] = (0, 0, 0, 0)
POPULATED_ALPHA = 255

REASON_PAD_SLOTS_EXHAUSTED = "pad_slots_exhausted"
REASON_LOOP_SLOTS_EXHAUSTED = "loop_slots_exhausted"


class CueKind(Enum):
    HOT = "HOT"
    MEMORY = "MEMORY"


@dataclass(frozen=True)
class SourceCue:
    """One rekordbox cue/loop entry (mapper-local stand-in for IR SourceCue)."""

    kind: CueKind
    start_sample: float
    end_sample: float | None = None  # not None ⇒ loop (U3)
    hot_slot: int | None = None  # 1..8 == pads A..H
    color: tuple[int, int, int] | None = None  # RGB; None → opaque black
    name: str | None = None

    @property
    def is_loop(self) -> bool:
        return self.end_sample is not None


@dataclass(frozen=True)
class QuickCueSlot:
    """One Engine quick-cue pad (mapper-local stand-in for ir_engine)."""

    label: str
    sample_offset: float
    argb: tuple[int, int, int, int]  # alpha first


@dataclass(frozen=True)
class LoopSlot:
    """One Engine loop slot (mapper-local stand-in for ir_engine)."""

    label: str
    start: float
    end: float
    is_start_set: int
    is_end_set: int
    argb: tuple[int, int, int, int]


@dataclass(frozen=True)
class DroppedEntry:
    """Itemized drop for the conversion report — actionable, machine-stable."""

    reason_code: str
    kind: CueKind
    start_sample: float
    end_sample: float | None
    hot_slot: int | None
    name: str | None
    color: tuple[int, int, int] | None


@dataclass(frozen=True)
class CueMergeResult:
    quick_cues: list[QuickCueSlot]  # always len == 8
    loops: list[LoopSlot]  # always len == 8
    dropped: list[DroppedEntry]


EMPTY_QUICK_CUE = QuickCueSlot(
    label="",
    sample_offset=EMPTY_SAMPLE,
    argb=EMPTY_ARGB,
)

EMPTY_LOOP = LoopSlot(
    label="",
    start=EMPTY_SAMPLE,
    end=EMPTY_SAMPLE,
    is_start_set=0,
    is_end_set=0,
    argb=EMPTY_ARGB,
)


def _rgb_to_argb(color: tuple[int, int, int] | None) -> tuple[int, int, int, int]:
    if color is None:
        return (POPULATED_ALPHA, 0, 0, 0)
    r, g, b = color
    return (POPULATED_ALPHA, r, g, b)


def _label(cue: SourceCue) -> str:
    return cue.name if cue.name is not None else ""


def _to_quick_cue(cue: SourceCue) -> QuickCueSlot:
    return QuickCueSlot(
        label=_label(cue),
        sample_offset=float(cue.start_sample),
        argb=_rgb_to_argb(cue.color),
    )


def _to_loop_slot(cue: SourceCue) -> LoopSlot:
    assert cue.end_sample is not None
    return LoopSlot(
        label=_label(cue),
        start=float(cue.start_sample),
        end=float(cue.end_sample),
        is_start_set=1,
        is_end_set=1,
        argb=_rgb_to_argb(cue.color),
    )


def _to_dropped(cue: SourceCue, reason_code: str) -> DroppedEntry:
    return DroppedEntry(
        reason_code=reason_code,
        kind=cue.kind,
        start_sample=float(cue.start_sample),
        end_sample=None if cue.end_sample is None else float(cue.end_sample),
        hot_slot=cue.hot_slot,
        name=cue.name,
        color=cue.color,
    )


def _valid_hot_slot(slot: int | None) -> bool:
    return slot is not None and 1 <= slot <= NUM_SLOTS


def merge_cues(cues: Sequence[SourceCue]) -> CueMergeResult:
    """Apply the pad/loop merge policy. Always returns 8+8 slots and a drop list."""
    loops_src: list[SourceCue] = []
    hot_points: list[SourceCue] = []
    memory_points: list[SourceCue] = []

    for cue in cues:
        if cue.is_loop:
            loops_src.append(cue)
        elif cue.kind is CueKind.HOT and _valid_hot_slot(cue.hot_slot):
            hot_points.append(cue)
        else:
            # MEMORY point-cues, or HOT without a usable pad number → fill queue.
            memory_points.append(cue)

    # --- quick-cue pads -------------------------------------------------
    pads: list[QuickCueSlot | None] = [None] * NUM_SLOTS

    for cue in hot_points:
        assert cue.hot_slot is not None
        idx = cue.hot_slot - 1
        # First claim wins — real exports do not double-book a pad.
        if pads[idx] is None:
            pads[idx] = _to_quick_cue(cue)

    memory_points_sorted = sorted(memory_points, key=lambda c: c.start_sample)
    dropped: list[DroppedEntry] = []
    mem_iter = iter(memory_points_sorted)
    for idx in range(NUM_SLOTS):
        if pads[idx] is not None:
            continue
        try:
            cue = next(mem_iter)
        except StopIteration:
            break
        pads[idx] = _to_quick_cue(cue)
    for leftover in mem_iter:
        dropped.append(_to_dropped(leftover, REASON_PAD_SLOTS_EXHAUSTED))

    quick_cues = [p if p is not None else EMPTY_QUICK_CUE for p in pads]

    # --- loop slots -----------------------------------------------------
    hot_loops = sorted(
        (c for c in loops_src if c.kind is CueKind.HOT and _valid_hot_slot(c.hot_slot)),
        key=lambda c: c.hot_slot if c.hot_slot is not None else 0,
    )
    # HOT loops without a valid slot join the memory-loop chronological queue.
    other_loops = sorted(
        (
            c
            for c in loops_src
            if not (c.kind is CueKind.HOT and _valid_hot_slot(c.hot_slot))
        ),
        key=lambda c: c.start_sample,
    )
    ordered_loops = hot_loops + other_loops

    loop_slots: list[LoopSlot] = []
    for i, cue in enumerate(ordered_loops):
        if i < NUM_SLOTS:
            loop_slots.append(_to_loop_slot(cue))
        else:
            dropped.append(_to_dropped(cue, REASON_LOOP_SLOTS_EXHAUSTED))
    while len(loop_slots) < NUM_SLOTS:
        loop_slots.append(EMPTY_LOOP)

    return CueMergeResult(
        quick_cues=quick_cues,
        loops=loop_slots,
        dropped=dropped,
    )
