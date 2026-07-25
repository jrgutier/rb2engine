"""PerformanceData blob codecs — golden byte-identity is the primary gate.

These tests exist because a self-consistent-but-wrong encoder (wrong endianness,
zlib framing, ARGB order, or loop compression) would silently corrupt every
beatgrid and cue on the stick. Engine-authored bytes in engine_ref.db are the
only non-tautological oracle: encode(decode(blob)) == blob cannot be satisfied
by inventing a private dialect.
"""

from __future__ import annotations

import sqlite3
import struct
import zlib
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from rb2engine.writer.blobs import (
    MAX_LOOPS,
    MAX_QUICK_CUES,
    BeatData,
    BeatGrid,
    BeatMarker,
    Color,
    Loop,
    Loops,
    QuickCue,
    QuickCues,
    TrackData,
    decode_beat_data,
    decode_loops,
    decode_overview_waveform,
    decode_quick_cues,
    decode_track_data,
    encode_beat_data,
    encode_loops,
    encode_quick_cues,
    encode_track_data,
)

GOLDEN_DB = (
    Path(__file__).resolve().parents[1] / "fixtures" / "golden" / "engine_ref.db"
)


def _load_performance_blobs() -> list[dict[str, bytes | None]]:
    """Read every PerformanceData blob column from the Engine-authored golden DB."""
    conn = sqlite3.connect(GOLDEN_DB)
    try:
        rows = conn.execute(
            "SELECT trackData, beatData, quickCues, loops, overviewWaveFormData "
            "FROM PerformanceData"
        ).fetchall()
    finally:
        conn.close()
    assert rows, "engine_ref.db must contain at least one PerformanceData row"
    out: list[dict[str, bytes | None]] = []
    for row in rows:
        out.append(
            {
                "trackData": row[0],
                "beatData": row[1],
                "quickCues": row[2],
                "loops": row[3],
                "overviewWaveFormData": row[4],
            }
        )
    return out


# ---------------------------------------------------------------------------
# Golden byte-identity — THE primary gate (Engine's own bytes)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "column,decode,encode",
    [
        ("trackData", decode_track_data, encode_track_data),
        ("beatData", decode_beat_data, encode_beat_data),
        ("quickCues", decode_quick_cues, encode_quick_cues),
        ("loops", decode_loops, encode_loops),
    ],
)
def test_golden_byte_identity_roundtrip(column, decode, encode) -> None:
    """encode(decode(engine_blob)) must equal engine_blob byte-for-byte.

    Why this matters: any wrong field order, endianness, sentinel, padding, or
    zlib framing produces a different blob while still looking "reasonable" in
    unit tests that only round-trip our own values. Engine authored these bytes
    in 4.3.0; matching them is the only proof we speak Engine's dialect.
    """
    for blobs in _load_performance_blobs():
        original = blobs[column]
        assert original is not None and len(original) > 0, (
            f"golden {column} must be populated"
        )
        assert encode(decode(original)) == original


def test_golden_decoded_values_are_sane() -> None:
    """Decoded golden fields must match measured Engine 4.3.0 ground truth.

    Pins sample-rate/sample-count identity across trackData and beatData, the
    -4 first-beat convention, always-8 cue/loop slots, and empty-slot sentinels
    on this fixture's un-cued track.
    """
    blobs = _load_performance_blobs()[0]

    td = decode_track_data(blobs["trackData"])  # type: ignore[arg-type]
    assert td.sample_rate == 44100.0
    assert td.samples == 12172288
    assert td.key == 1
    assert td.average_loudness_low == 0.0
    assert td.average_loudness_mid == 0.0
    assert td.average_loudness_high == 0.0

    bd = decode_beat_data(blobs["beatData"])  # type: ignore[arg-type]
    assert bd.sample_rate == td.sample_rate
    assert bd.samples == float(td.samples)
    assert bd.is_beatgrid_set == 1
    assert len(bd.default_beat_grid.markers) == 2
    assert len(bd.adjusted_beat_grid.markers) == 2
    # Engine's normalize_beatgrid convention: first beat index is -4.
    assert bd.default_beat_grid.markers[0].beat_number == -4
    assert bd.adjusted_beat_grid.markers[0].beat_number == -4

    qc = decode_quick_cues(blobs["quickCues"])  # type: ignore[arg-type]
    assert len(qc.cues) == MAX_QUICK_CUES
    for cue in qc.cues:
        assert cue.sample_offset == -1.0
        assert cue.label == ""
        assert cue.color == Color(0, 0, 0, 0)

    loops = decode_loops(blobs["loops"])  # type: ignore[arg-type]
    assert len(loops.loops) == MAX_LOOPS
    for loop in loops.loops:
        assert loop.start_sample_offset == -1.0
        assert loop.end_sample_offset == -1.0
        assert loop.is_start_set == 0
        assert loop.is_end_set == 0
        assert loop.label == ""
        assert loop.color == Color(0, 0, 0, 0)


def test_golden_overview_waveform_decodes() -> None:
    """overviewWaveFormData is read-only completeness; never written by rb2engine."""
    blobs = _load_performance_blobs()[0]
    ow = decode_overview_waveform(blobs["overviewWaveFormData"])  # type: ignore[arg-type]
    assert ow.num_points == 1024
    assert len(ow.points) == 1024
    assert ow.samples_per_waveform_point == 11887.0
    assert len(ow.max_point) == 3


# ---------------------------------------------------------------------------
# Endianness regression tests — the traps that corrupt every position
# ---------------------------------------------------------------------------


def test_beatdata_grid_count_is_big_endian_markers_are_little_endian() -> None:
    """beatData mixed endianness: count is BE, each 24-byte marker is LE.

    Failure this prevents: treating the whole grid as little-endian makes the
    marker count explode (BE 0x0000…0002 read as LE → huge N) or, if only the
    count is wrong-endian, sample_offset doubles land at astronomical values
    and every beat is unusable. This is the single most likely bug in blobs.py.
    """
    # Hand-built uncompressed payload — NOT produced by our encoder.
    # sample_rate=48000.0 BE, samples=96000.0 BE, is_beatgrid_set=1
    # default grid: count=1 BE, one marker LE
    # adjusted grid: count=0 BE
    marker = struct.pack(
        "<dqii",
        1234.5,  # sample_offset LE double
        -4,  # beat_number LE int64 (Engine first-beat convention)
        16,  # number_of_beats LE int32
        7,  # unknown LE int32
    )
    payload = (
        struct.pack(">d", 48000.0)
        + struct.pack(">d", 96000.0)
        + bytes([1])
        + struct.pack(">q", 1)  # BE count — the trap
        + marker
        + struct.pack(">q", 0)  # empty adjusted grid, BE count
    )
    blob = struct.pack(">i", len(payload)) + zlib.compress(payload)

    decoded = decode_beat_data(blob)
    assert decoded.sample_rate == 48000.0
    assert decoded.samples == 96000.0
    assert decoded.is_beatgrid_set == 1
    assert len(decoded.default_beat_grid.markers) == 1
    m = decoded.default_beat_grid.markers[0]
    assert m.sample_offset == 1234.5
    assert m.beat_number == -4
    assert m.number_of_beats == 16
    assert m.unknown == 7
    assert decoded.adjusted_beat_grid.markers == []

    # Encoder must emit the same mixed-endian layout (inspect raw payload).
    reencoded = encode_beat_data(decoded)
    raw = zlib.decompress(reencoded[4:])
    # After 8+8+1 header, count must be BE 1: 00 00 00 00 00 00 00 01
    assert raw[17:25] == b"\x00\x00\x00\x00\x00\x00\x00\x01"
    # Marker sample_offset at offset 25 must be LE encoding of 1234.5
    assert raw[25:33] == struct.pack("<d", 1234.5)
    # beat_number -4 as LE int64
    assert raw[33:41] == struct.pack("<q", -4)


def test_loops_count_is_little_endian_and_blob_is_not_zlib() -> None:
    """loops count is LE int64 and the blob is NEVER zlib-framed.

    Failure this prevents: applying the trackData/beatData/quickCues framing
    (BE length + zlib) to loops — or writing the count as BE like quickCues —
    yields a blob Engine cannot parse. Golden loops begin `08 00 00 00 00 00
    00 00` (LE 8), not `00 00 00 08 78 9C…`. Getting this wrong corrupts every
    loop on the stick while quickCues still look fine.
    """
    # Hand-built: count=8 LE, eight empty sentinels (no zlib, no length prefix).
    empty = (
        bytes([0])  # label_len
        + struct.pack("<d", -1.0)
        + struct.pack("<d", -1.0)
        + bytes([0, 0, 0, 0, 0, 0])  # is_start, is_end, ARGB
    )
    assert len(empty) == 23
    hand = struct.pack("<q", 8) + empty * 8
    assert hand[:8] == b"\x08\x00\x00\x00\x00\x00\x00\x00"
    # Must NOT look like zlib framing
    assert hand[4:6] != b"\x78\x9c"

    decoded = decode_loops(hand)
    assert len(decoded.loops) == 8
    for loop in decoded.loops:
        assert loop.start_sample_offset == -1.0
        assert loop.end_sample_offset == -1.0

    encoded = encode_loops(decoded)
    assert encoded[:8] == struct.pack("<q", 8)
    # Still uncompressed
    assert encoded[4:6] != b"\x78\x9c"
    assert encode_loops(decoded) == hand


def test_quickcues_count_is_big_endian_opposite_of_loops() -> None:
    """quickCues count is BE int64 (always 8) — opposite endianness from loops.

    Failure this prevents: sharing a 'count is LE' helper between quickCues and
    loops. quickCues is zlib-framed; inside the payload the count is BE. If it
    were LE, decode would see count=0x0800000000000000 and blow past the buffer
    or invent millions of cues.
    """
    # Eight empty cues + main-cue trailer, hand-built BE count.
    empty_cue = (
        bytes([0])
        + struct.pack(">d", -1.0)
        + bytes([0, 0, 0, 0])
    )
    assert len(empty_cue) == 13
    payload = (
        struct.pack(">q", 8)  # BE — opposite of loops
        + empty_cue * 8
        + struct.pack(">d", -1.0)  # adjusted_main_cue
        + bytes([0])  # is_main_cue_adjusted
        + struct.pack(">d", -1.0)  # default_main_cue
    )
    assert payload[:8] == b"\x00\x00\x00\x00\x00\x00\x00\x08"
    blob = struct.pack(">i", len(payload)) + zlib.compress(payload)

    decoded = decode_quick_cues(blob)
    assert len(decoded.cues) == 8
    assert all(c.sample_offset == -1.0 for c in decoded.cues)

    reencoded = encode_quick_cues(decoded)
    raw = zlib.decompress(reencoded[4:])
    assert raw[:8] == struct.pack(">q", 8)


def test_zlib_framing_is_be_length_plus_zlib_not_raw_deflate() -> None:
    """Compressed blobs start with BE int32 uncompressed length then zlib 78 9C.

    Failure this prevents: writing raw DEFLATE (no zlib wrapper) or a LE length
    prefix — Engine would reject or mis-size the payload. Confirmed against
    Engine 4.3.0: beatData begins 00 00 00 8A 78 9C….
    """
    blobs = _load_performance_blobs()[0]
    for col in ("trackData", "beatData", "quickCues"):
        blob = blobs[col]
        assert blob is not None
        unclen = struct.unpack(">i", blob[:4])[0]
        assert blob[4:6] == b"\x78\x9c", f"{col} must use zlib wrapper, not raw DEFLATE"
        raw = zlib.decompress(blob[4:])
        assert len(raw) == unclen


def test_quickcues_color_is_argb_alpha_first() -> None:
    """Color bytes are A,R,G,B — alpha first, not RGBA or BGRA.

    Failure this prevents: swapping to RGBA makes every pad render the wrong
    channel (red becomes green, etc.) while positions still look correct.
    """
    cue_bytes = (
        bytes([3])
        + b"Go!"
        + struct.pack(">d", 44100.0)
        + bytes([255, 0x11, 0x22, 0x33])  # A=255, R=0x11, G=0x22, B=0x33
    )
    empty = bytes([0]) + struct.pack(">d", -1.0) + bytes(4)
    payload = struct.pack(">q", 8) + cue_bytes + empty * 7
    payload += struct.pack(">d", 0.0) + bytes([0]) + struct.pack(">d", 0.0)
    blob = struct.pack(">i", len(payload)) + zlib.compress(payload)

    decoded = decode_quick_cues(blob)
    assert decoded.cues[0].color == Color(255, 0x11, 0x22, 0x33)
    assert decoded.cues[0].label == "Go!"
    assert decoded.cues[0].sample_offset == 44100.0


# ---------------------------------------------------------------------------
# Empty / default / sentinel behaviour
# ---------------------------------------------------------------------------


def test_zero_length_blobs_decode_to_defaults() -> None:
    """Empty BLOB is Engine's valid 'no data' sentinel — must not raise."""
    td = decode_track_data(b"")
    assert td.sample_rate == 0.0
    assert td.samples == 0

    bd = decode_beat_data(b"")
    assert bd.is_beatgrid_set == 0
    assert bd.default_beat_grid.markers == []
    assert bd.adjusted_beat_grid.markers == []

    qc = decode_quick_cues(b"")
    assert len(qc.cues) == MAX_QUICK_CUES
    assert all(c.sample_offset == -1.0 for c in qc.cues)

    loops = decode_loops(b"")
    assert len(loops.loops) == MAX_LOOPS
    assert all(lp.start_sample_offset == -1.0 for lp in loops.loops)

    ow = decode_overview_waveform(b"")
    assert ow.num_points == 0
    assert ow.points == []


def test_encode_always_emits_exactly_eight_quickcue_slots() -> None:
    """Partial cue lists must pad to 8 with empty-slot sentinels."""
    qc = QuickCues(
        cues=[
            QuickCue("A", 1000.0, Color(255, 255, 0, 0)),
            QuickCue("B", 2000.0, Color(255, 0, 255, 0)),
        ],
        adjusted_main_cue=0.0,
        is_main_cue_adjusted=0,
        default_main_cue=0.0,
    )
    decoded = decode_quick_cues(encode_quick_cues(qc))
    assert len(decoded.cues) == 8
    assert decoded.cues[0].label == "A"
    assert decoded.cues[1].label == "B"
    for c in decoded.cues[2:]:
        assert c.sample_offset == -1.0
        assert c.label == ""
        assert c.color == Color(0, 0, 0, 0)


def test_encode_always_emits_exactly_eight_loop_slots() -> None:
    """Partial loop lists must pad to MAX_LOOPS=8 with empty sentinels."""
    loops = Loops(
        loops=[
            Loop("L1", 100.0, 200.0, 1, 1, Color(255, 0, 0, 255)),
        ]
    )
    decoded = decode_loops(encode_loops(loops))
    assert len(decoded.loops) == 8
    assert decoded.loops[0].label == "L1"
    assert decoded.loops[0].is_start_set == 1
    for lp in decoded.loops[1:]:
        assert lp.start_sample_offset == -1.0
        assert lp.end_sample_offset == -1.0
        assert lp.is_start_set == 0
        assert lp.is_end_set == 0


# ---------------------------------------------------------------------------
# Hypothesis: decode(encode(x)) == x
# ---------------------------------------------------------------------------

_color_st = st.builds(
    Color,
    a=st.integers(0, 255),
    r=st.integers(0, 255),
    g=st.integers(0, 255),
    b=st.integers(0, 255),
)

_label_st = st.text(
    alphabet=st.characters(min_codepoint=32, max_codepoint=126),
    min_size=0,
    max_size=32,
)

_finite = st.floats(
    allow_nan=False, allow_infinity=False, width=64, min_value=-1e15, max_value=1e15
)


@given(
    sample_rate=st.floats(min_value=8000.0, max_value=192000.0, allow_nan=False),
    samples=st.integers(0, 2**40),
    key=st.integers(0, 23),
    lo=_finite,
    mid=_finite,
    hi=_finite,
)
@settings(max_examples=50)
def test_track_data_property_roundtrip(sample_rate, samples, key, lo, mid, hi) -> None:
    """decode(encode(x)) must preserve every trackData field exactly."""
    original = TrackData(sample_rate, samples, key, lo, mid, hi)
    assert decode_track_data(encode_track_data(original)) == original


_marker_st = st.builds(
    BeatMarker,
    sample_offset=_finite,
    beat_number=st.integers(-10000, 100000),  # includes Engine's -4 convention
    number_of_beats=st.integers(0, 100000),
    unknown=st.integers(-(2**31), 2**31 - 1),
)


@given(
    sample_rate=st.floats(min_value=8000.0, max_value=192000.0, allow_nan=False),
    samples=_finite,
    is_set=st.integers(0, 1),
    default_markers=st.lists(_marker_st, max_size=8),
    adjusted_markers=st.lists(_marker_st, max_size=8),
    extra=st.binary(max_size=16),
)
@settings(max_examples=40)
def test_beat_data_property_roundtrip(
    sample_rate, samples, is_set, default_markers, adjusted_markers, extra
) -> None:
    """Includes negative beat_number markers (first beat at -4)."""
    original = BeatData(
        sample_rate=sample_rate,
        samples=samples,
        is_beatgrid_set=is_set,
        default_beat_grid=BeatGrid(list(default_markers)),
        adjusted_beat_grid=BeatGrid(list(adjusted_markers)),
        extra_data=extra,
    )
    assert decode_beat_data(encode_beat_data(original)) == original


_cue_st = st.builds(
    QuickCue,
    label=_label_st,
    sample_offset=st.one_of(st.just(-1.0), _finite),
    color=_color_st,
)


@given(
    cues=st.lists(_cue_st, min_size=0, max_size=8),
    adj=_finite,
    is_adj=st.integers(0, 1),
    default=_finite,
    extra=st.binary(max_size=8),
)
@settings(max_examples=40)
def test_quick_cues_property_roundtrip(cues, adj, is_adj, default, extra) -> None:
    """Padding to 8 and empty-slot sentinels must survive round-trip."""
    original = QuickCues(
        cues=list(cues),
        adjusted_main_cue=adj,
        is_main_cue_adjusted=is_adj,
        default_main_cue=default,
        extra_data=extra,
    )
    decoded = decode_quick_cues(encode_quick_cues(original))
    assert len(decoded.cues) == 8
    # Compare after applying the same pad-to-8 the encoder uses.
    padded = list(cues) + [
        QuickCue("", -1.0, Color(0, 0, 0, 0)) for _ in range(8 - len(cues))
    ]
    assert decoded.cues == padded
    assert decoded.adjusted_main_cue == adj
    assert decoded.is_main_cue_adjusted == is_adj
    assert decoded.default_main_cue == default
    assert decoded.extra_data == extra


_loop_st = st.builds(
    Loop,
    label=_label_st,
    start_sample_offset=st.one_of(st.just(-1.0), _finite),
    end_sample_offset=st.one_of(st.just(-1.0), _finite),
    is_start_set=st.integers(0, 1),
    is_end_set=st.integers(0, 1),
    color=_color_st,
)


@given(
    loops=st.lists(_loop_st, min_size=0, max_size=8),
    extra=st.binary(max_size=8),
)
@settings(max_examples=40)
def test_loops_property_roundtrip(loops, extra) -> None:
    """Uncompressed LE loops with pad-to-8 must round-trip including sentinels."""
    original = Loops(loops=list(loops), extra_data=extra)
    decoded = decode_loops(encode_loops(original))
    assert len(decoded.loops) == 8
    padded = list(loops) + [
        Loop("", -1.0, -1.0, 0, 0, Color(0, 0, 0, 0)) for _ in range(8 - len(loops))
    ]
    assert decoded.loops == padded
    assert decoded.extra_data == extra


def test_negative_beat_number_roundtrip_explicit() -> None:
    """Engine indexes the first beat at -4; negative beat_number must survive.

    Failure this prevents: encoding beat_number as unsigned, which turns -4 into
    a huge positive index and shifts the entire grid display.
    """
    marker = BeatMarker(
        sample_offset=-80699.29825333723,
        beat_number=-4,
        number_of_beats=589,
        unknown=11,
    )
    bd = BeatData(
        sample_rate=44100.0,
        samples=12172288.0,
        is_beatgrid_set=1,
        default_beat_grid=BeatGrid([marker]),
        adjusted_beat_grid=BeatGrid([marker]),
    )
    out = decode_beat_data(encode_beat_data(bd))
    assert out.default_beat_grid.markers[0].beat_number == -4
    assert out.adjusted_beat_grid.markers[0].beat_number == -4
