"""Tests for mapper/cues.py — pad merge + loop routing policy.

WHY: These rules are explicit user decisions (hot-cue pad muscle memory, U3 loop
frees pad, overflow itemized not silent). A silent renumber or silent drop ruins
muscle memory mid-set or hides data loss from the conversion report.
"""

from __future__ import annotations

from rb2engine.mapper.cues import (
    EMPTY_LOOP,
    EMPTY_QUICK_CUE,
    CueKind,
    CueMergeResult,
    DroppedEntry,
    LoopSlot,
    QuickCueSlot,
    SourceCue,
    merge_cues,
)


def _hot(
    slot: int,
    start: float,
    *,
    name: str | None = None,
    color: tuple[int, int, int] | None = None,
    end: float | None = None,
) -> SourceCue:
    return SourceCue(
        kind=CueKind.HOT,
        hot_slot=slot,
        start_sample=start,
        end_sample=end,
        color=color,
        name=name,
    )


def _mem(
    start: float,
    *,
    name: str | None = None,
    color: tuple[int, int, int] | None = None,
    end: float | None = None,
) -> SourceCue:
    return SourceCue(
        kind=CueKind.MEMORY,
        hot_slot=None,
        start_sample=start,
        end_sample=end,
        color=color,
        name=name,
    )


def _occupied_pads(result: CueMergeResult) -> list[int]:
    """1-based pad numbers that are not the empty sentinel."""
    return [
        i + 1
        for i, slot in enumerate(result.quick_cues)
        if slot.sample_offset != EMPTY_QUICK_CUE.sample_offset
    ]


def _occupied_loops(result: CueMergeResult) -> list[int]:
    return [
        i
        for i, slot in enumerate(result.loops)
        if slot.start != EMPTY_LOOP.start
    ]


class TestHotCuePadIdentity:
    """8 hot cues on distinct pads keep those exact pad numbers.

    WHY: Pad A–H is muscle memory; renumbering a cue to fill a gap would land
    the wrong drop mid-set even if every position value is correct.
    """

    def test_eight_hot_cues_preserve_pad_numbers(self) -> None:
        cues = [
            _hot(1, 1000.0, name="A", color=(255, 0, 0)),
            _hot(2, 2000.0, name="B", color=(0, 255, 0)),
            _hot(3, 3000.0, name="C", color=(0, 0, 255)),
            _hot(4, 4000.0, name="D", color=(255, 255, 0)),
            _hot(5, 5000.0, name="E", color=(255, 0, 255)),
            _hot(6, 6000.0, name="F", color=(0, 255, 255)),
            _hot(7, 7000.0, name="G", color=(128, 128, 128)),
            _hot(8, 8000.0, name="H", color=(255, 128, 0)),
        ]
        result = merge_cues(cues)

        assert len(result.quick_cues) == 8
        for pad in range(1, 9):
            slot = result.quick_cues[pad - 1]
            assert slot.sample_offset == float(pad * 1000)
            assert slot.label == cues[pad - 1].name
        assert result.dropped == []
        # No loops in this fixture — every loop slot is the empty sentinel.
        assert all(s == EMPTY_LOOP for s in result.loops)


class TestMemoryFillsGapsChronologically:
    """Memory cues only fill pads left free by hot cues, by start position.

    WHY: Hot cues own their pad; memory cues are secondary markers. Chronological
    fill is the deterministic rule the user accepted so deck order matches
    timeline order, not source-list order.
    """

    def test_memory_fills_only_remaining_pads_in_time_order(self) -> None:
        # Hot on pads 1, 3, 5 — gaps at 2, 4, 6, 7, 8.
        # Three memory cues out of chronological source order.
        cues = [
            _hot(1, 100.0, name="hot1"),
            _hot(3, 300.0, name="hot3"),
            _hot(5, 500.0, name="hot5"),
            _mem(400.0, name="mem-late"),  # 2nd chronologically
            _mem(50.0, name="mem-early"),  # 1st chronologically
            _mem(250.0, name="mem-mid"),  # 3rd? 50, 250, 400 → early, mid, late
        ]
        result = merge_cues(cues)

        assert result.quick_cues[0].label == "hot1"
        assert result.quick_cues[2].label == "hot3"
        assert result.quick_cues[4].label == "hot5"
        # Remaining pads 2,4,6 filled in chronological order of memory cues.
        assert result.quick_cues[1].label == "mem-early"
        assert result.quick_cues[1].sample_offset == 50.0
        assert result.quick_cues[3].label == "mem-mid"
        assert result.quick_cues[3].sample_offset == 250.0
        assert result.quick_cues[5].label == "mem-late"
        assert result.quick_cues[5].sample_offset == 400.0
        # Pads 7–8 unused → sentinel.
        assert result.quick_cues[6] == EMPTY_QUICK_CUE
        assert result.quick_cues[7] == EMPTY_QUICK_CUE
        assert result.dropped == []


class TestPadOverflowItemized:
    """4 hot + 8 memory = 12 → exactly 8 pads, exactly 4 dropped with reason.

    WHY: Silent truncation hides data loss. The drop list feeds the conversion
    report so the user can see *which* memory cues never made the stick.
    """

    def test_twelve_point_cues_drop_four_with_pad_slots_exhausted(self) -> None:
        hot = [_hot(i, float(i * 1000), name=f"H{i}") for i in (1, 2, 3, 4)]
        # 8 memory cues; only 4 free pads remain → 4 must drop.
        # Chronological: m0..m7 at 10, 20, … 80 — earliest four fill, rest drop.
        mem = [_mem(float((i + 1) * 10), name=f"M{i}") for i in range(8)]
        result = merge_cues(hot + mem)

        occupied = _occupied_pads(result)
        assert len(occupied) == 8
        assert occupied == [1, 2, 3, 4, 5, 6, 7, 8]

        # Hot still on original pads.
        for i in (1, 2, 3, 4):
            assert result.quick_cues[i - 1].label == f"H{i}"

        # Free pads 5–8 take the four earliest memory cues (M0..M3).
        assert [result.quick_cues[i].label for i in range(4, 8)] == [
            "M0",
            "M1",
            "M2",
            "M3",
        ]

        dropped = result.dropped
        assert len(dropped) == 4
        assert all(d.reason_code == "pad_slots_exhausted" for d in dropped)
        assert {d.name for d in dropped} == {"M4", "M5", "M6", "M7"}
        # Dropped entries must carry actionable detail for the report.
        for d in dropped:
            assert isinstance(d, DroppedEntry)
            assert d.kind == CueKind.MEMORY
            assert d.start_sample is not None
            assert d.end_sample is None


class TestLoopFreesHotPad:
    """A hot cue that is a loop occupies a loop slot and frees its pad (U3).

    WHY: User decision U3 — route strictly by type. Keeping a loop on a pad would
    waste one of 8 pads and deny Engine's dedicated loop store; freeing the pad
    lets another point cue land there.
    """

    def test_hot_loop_does_not_consume_pad(self) -> None:
        cues = [
            _hot(1, 1000.0, end=2000.0, name="loop-on-A", color=(1, 2, 3)),
            _mem(500.0, name="fills-pad-1"),
        ]
        result = merge_cues(cues)

        # Pad 1 is free → memory cue may occupy it.
        assert result.quick_cues[0].label == "fills-pad-1"
        assert result.quick_cues[0].sample_offset == 500.0
        # Loop landed in loop store, not on a pad.
        assert result.loops[0].label == "loop-on-A"
        assert result.loops[0].start == 1000.0
        assert result.loops[0].end == 2000.0
        assert result.loops[0].is_start_set == 1
        assert result.loops[0].is_end_set == 1
        assert all(
            s.sample_offset == EMPTY_QUICK_CUE.sample_offset
            or s.label == "fills-pad-1"
            for s in result.quick_cues
        )
        assert result.dropped == []


class TestLoopOverflowItemized:
    """10 loops → 8 slots filled, 2 itemized with loop_slots_exhausted.

    WHY: Same as pad overflow — Engine hard-caps at 8 loop slots; excess must
    surface in the report under a stable reason code, not vanish.
    """

    def test_ten_loops_drop_two(self) -> None:
        # 4 hot-cue loops (slots 1–4) + 6 memory loops, chronological.
        hot_loops = [
            _hot(i, float(i * 100), end=float(i * 100 + 50), name=f"HL{i}")
            for i in (1, 2, 3, 4)
        ]
        mem_loops = [
            _mem(float(1000 + i * 10), end=float(1000 + i * 10 + 5), name=f"ML{i}")
            for i in range(6)
        ]
        result = merge_cues(hot_loops + mem_loops)

        assert len(_occupied_loops(result)) == 8
        # Hot-cue loops first in pad order, then memory loops chronologically.
        labels = [result.loops[i].label for i in range(8)]
        assert labels == [
            "HL1",
            "HL2",
            "HL3",
            "HL4",
            "ML0",
            "ML1",
            "ML2",
            "ML3",
        ]
        dropped = result.dropped
        assert len(dropped) == 2
        assert all(d.reason_code == "loop_slots_exhausted" for d in dropped)
        assert {d.name for d in dropped} == {"ML4", "ML5"}
        for d in dropped:
            assert d.end_sample is not None  # actionable: report can show in/out


class TestZeroCuesSentinelPadding:
    """No cues → both stores are fully sentinel-padded to length 8.

    WHY: Engine always writes 8 slots; partial arrays would desync blob encoding
    and make empty tracks look corrupt vs Engine-authored empties.
    """

    def test_empty_input_is_fully_sentinel_padded(self) -> None:
        result = merge_cues([])
        assert len(result.quick_cues) == 8
        assert len(result.loops) == 8
        assert result.quick_cues == [EMPTY_QUICK_CUE] * 8
        assert result.loops == [EMPTY_LOOP] * 8
        assert result.dropped == []


class TestColorAndNamePreservation:
    """Colors and names pass through the mapping unchanged (ARGB alpha=255).

    WHY: Pad color and label are part of acceptance criteria 5; any palette remap
    or name rewrite would fail the GUI check even when positions are perfect.
    """

    def test_color_and_name_preserved_exactly(self) -> None:
        rgb = (0x4D, 0x00, 0xFF)
        cues = [
            _hot(2, 12345.0, name="Drop 日本語", color=rgb),
            _mem(50.0, name="mem-cue", color=(10, 20, 30)),
            _hot(3, 500.0, end=900.0, name="loop-name", color=(7, 8, 9)),
        ]
        result = merge_cues(cues)

        pad2 = result.quick_cues[1]
        assert pad2.label == "Drop 日本語"
        assert pad2.sample_offset == 12345.0
        assert pad2.argb == (255, 0x4D, 0x00, 0xFF)  # ARGB, alpha first

        # Memory fills first free pad (pad 1).
        pad1 = result.quick_cues[0]
        assert pad1.label == "mem-cue"
        assert pad1.argb == (255, 10, 20, 30)

        loop0 = result.loops[0]
        assert loop0.label == "loop-name"
        assert loop0.argb == (255, 7, 8, 9)
        assert loop0.start == 500.0
        assert loop0.end == 900.0


class TestLoopOrderingHotThenMemory:
    """Hot-cue loops outrank memory loops even when memory starts earlier.

    WHY: Hot-cue loops carry explicit pad intent (A–H); chronological memory
    loops must not push them out when the 8-slot cap is tight.
    """

    def test_hot_loops_before_earlier_memory_loops(self) -> None:
        cues = [
            _mem(10.0, end=20.0, name="early-mem-loop"),
            _hot(8, 9999.0, end=10000.0, name="late-hot-loop"),
            _hot(1, 5000.0, end=6000.0, name="mid-hot-loop"),
        ]
        result = merge_cues(cues)
        assert [s.label for s in result.loops[:3]] == [
            "mid-hot-loop",  # pad 1 before pad 8
            "late-hot-loop",
            "early-mem-loop",
        ]


class TestResultShape:
    """merge_cues always returns fixed-width slot arrays.

    WHY: writer/blobs.py encodes exactly 8 quick cues and 8 loops; variable
    length would be a latent framing bug.
    """

    def test_always_eight_slots(self) -> None:
        result = merge_cues([_hot(1, 1.0, name="only")])
        assert len(result.quick_cues) == 8
        assert len(result.loops) == 8
        assert isinstance(result.quick_cues[0], QuickCueSlot)
        assert isinstance(result.loops[0], LoopSlot)
