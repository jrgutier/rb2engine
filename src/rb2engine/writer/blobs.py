"""decode_*/encode_* for trackData, beatData, quickCues, loops (and overviewWaveForm decode).

Byte layouts are empirically confirmed against Engine DJ 4.3.0 output (engine_ref.db).
Do not "correct" mixed endianness — it is intentional:

  Compression framing (trackData, beatData, quickCues, overviewWaveFormData):
    4-byte BIG-ENDIAN int32 = uncompressed length, then zlib (header 78 9C).
    Zero-length blob = valid "no data" sentinel.

  loops is the exception: NOT compressed, no length prefix.
    count is LITTLE-ENDIAN int64 (opposite of quickCues' BE count).

  beatData mixed endianness (highest-risk trap):
    grid count: int64 BIG-ENDIAN
    each marker's fields: LITTLE-ENDIAN (sample_offset double, beat_number int64, …)

Write policy notes resolved at M2 against engine_ref.db:
  - beatData.extra_data may be non-empty (golden has 9 trailing zero bytes); preserve
    on decode→encode; new writers may emit empty extra_data.
  - quickCues/loops always exactly 8 slots; empty sentinel sample_offset=-1, color 0s.
  - Positions are samples, never seconds/milliseconds.
  - First beat index convention: -4 (Engine normalize_beatgrid).
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass, field

MAX_QUICK_CUES = 8
MAX_LOOPS = 8

_EMPTY_COLOR = (0, 0, 0, 0)
_EMPTY_CUE_OFFSET = -1.0
_EMPTY_LOOP_OFFSET = -1.0


# ---------------------------------------------------------------------------
# Data types (codec-facing; mapper types live in ir_engine.py)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Color:
    """ARGB — alpha first (Engine pad color order)."""

    a: int
    r: int
    g: int
    b: int


@dataclass(frozen=True, slots=True)
class TrackData:
    sample_rate: float
    samples: int
    key: int
    average_loudness_low: float = 0.0
    average_loudness_mid: float = 0.0
    average_loudness_high: float = 0.0


@dataclass(frozen=True, slots=True)
class BeatMarker:
    sample_offset: float
    beat_number: int
    number_of_beats: int
    unknown: int = 0


@dataclass(frozen=True, slots=True)
class BeatGrid:
    markers: list[BeatMarker] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class BeatData:
    sample_rate: float
    samples: float
    is_beatgrid_set: int
    default_beat_grid: BeatGrid
    adjusted_beat_grid: BeatGrid
    extra_data: bytes = b""


@dataclass(frozen=True, slots=True)
class QuickCue:
    label: str
    sample_offset: float
    color: Color


@dataclass(frozen=True, slots=True)
class QuickCues:
    cues: list[QuickCue]
    adjusted_main_cue: float = 0.0
    is_main_cue_adjusted: int = 0
    default_main_cue: float = 0.0
    extra_data: bytes = b""


@dataclass(frozen=True, slots=True)
class Loop:
    label: str
    start_sample_offset: float
    end_sample_offset: float
    is_start_set: int
    is_end_set: int
    color: Color


@dataclass(frozen=True, slots=True)
class Loops:
    loops: list[Loop]
    extra_data: bytes = b""


@dataclass(frozen=True, slots=True)
class OverviewWaveform:
    """Decoded overviewWaveFormData (read-only; rb2engine never writes this)."""

    num_points: int
    samples_per_waveform_point: float
    points: list[tuple[int, int, int]]  # (low, mid, high) per point
    max_point: tuple[int, int, int]


def _empty_quick_cue() -> QuickCue:
    return QuickCue("", _EMPTY_CUE_OFFSET, Color(0, 0, 0, 0))


def _empty_loop() -> Loop:
    return Loop("", _EMPTY_LOOP_OFFSET, _EMPTY_LOOP_OFFSET, 0, 0, Color(0, 0, 0, 0))


# ---------------------------------------------------------------------------
# Compression framing
# ---------------------------------------------------------------------------


def _compress_frame(payload: bytes) -> bytes:
    """4-byte BE uncompressed length + zlib-wrapped deflate (level 6 / default)."""
    compressed = zlib.compress(payload)  # Z_DEFAULT_COMPRESSION == 6; matches Engine
    return struct.pack(">i", len(payload)) + compressed


def _decompress_frame(blob: bytes) -> bytes:
    if not blob:
        return b""
    if len(blob) < 4:
        raise ValueError(f"compressed blob too short: {len(blob)} bytes")
    unclen = struct.unpack_from(">i", blob, 0)[0]
    raw = zlib.decompress(blob[4:])
    if len(raw) != unclen:
        raise ValueError(
            f"uncompressed length mismatch: header={unclen} actual={len(raw)}"
        )
    return raw


# ---------------------------------------------------------------------------
# trackData — zlib, 44-byte BE payload
# ---------------------------------------------------------------------------


def decode_track_data(blob: bytes) -> TrackData:
    if not blob:
        return TrackData(0.0, 0, 0)
    raw = _decompress_frame(blob)
    if len(raw) != 44:
        raise ValueError(f"trackData payload must be 44 bytes, got {len(raw)}")
    sample_rate, samples, key, lo, mid, hi = struct.unpack(">dqiddd", raw)
    return TrackData(sample_rate, samples, key, lo, mid, hi)


def encode_track_data(data: TrackData) -> bytes:
    payload = struct.pack(
        ">dqiddd",
        data.sample_rate,
        data.samples,
        data.key,
        data.average_loudness_low,
        data.average_loudness_mid,
        data.average_loudness_high,
    )
    assert len(payload) == 44
    return _compress_frame(payload)


# ---------------------------------------------------------------------------
# beatData — zlib; BE scalars + BE grid counts; LE 24-byte markers
# ---------------------------------------------------------------------------


def _decode_beat_grid(raw: bytes, offset: int) -> tuple[BeatGrid, int]:
    if offset + 8 > len(raw):
        raise ValueError("truncated beat grid count")
    # ⚠️ COUNT is BIG-ENDIAN
    count = struct.unpack_from(">q", raw, offset)[0]
    offset += 8
    if count < 0:
        raise ValueError(f"negative beat grid count: {count}")
    markers: list[BeatMarker] = []
    for _ in range(count):
        if offset + 24 > len(raw):
            raise ValueError("truncated beat marker")
        # ⚠️ MARKER FIELDS are LITTLE-ENDIAN
        sample_offset, beat_number, number_of_beats, unknown = struct.unpack_from(
            "<dqii", raw, offset
        )
        offset += 24
        markers.append(
            BeatMarker(sample_offset, beat_number, number_of_beats, unknown)
        )
    return BeatGrid(markers), offset


def _encode_beat_grid(grid: BeatGrid) -> bytes:
    out = struct.pack(">q", len(grid.markers))  # BE count
    for m in grid.markers:
        out += struct.pack(
            "<dqii",  # LE marker internals
            m.sample_offset,
            m.beat_number,
            m.number_of_beats,
            m.unknown,
        )
    return out


def decode_beat_data(blob: bytes) -> BeatData:
    if not blob:
        return BeatData(
            0.0,
            0.0,
            0,
            BeatGrid(),
            BeatGrid(),
        )
    raw = _decompress_frame(blob)
    if len(raw) < 17:
        raise ValueError(f"beatData payload too short: {len(raw)}")
    sample_rate = struct.unpack_from(">d", raw, 0)[0]
    samples = struct.unpack_from(">d", raw, 8)[0]
    is_beatgrid_set = raw[16]
    offset = 17
    default_grid, offset = _decode_beat_grid(raw, offset)
    adjusted_grid, offset = _decode_beat_grid(raw, offset)
    extra_data = raw[offset:]
    return BeatData(
        sample_rate=sample_rate,
        samples=samples,
        is_beatgrid_set=is_beatgrid_set,
        default_beat_grid=default_grid,
        adjusted_beat_grid=adjusted_grid,
        extra_data=extra_data,
    )


def encode_beat_data(data: BeatData) -> bytes:
    payload = (
        struct.pack(">d", data.sample_rate)
        + struct.pack(">d", data.samples)
        + bytes([data.is_beatgrid_set & 0xFF])
        + _encode_beat_grid(data.default_beat_grid)
        + _encode_beat_grid(data.adjusted_beat_grid)
        + data.extra_data
    )
    return _compress_frame(payload)


# ---------------------------------------------------------------------------
# quickCues — zlib; count int64 BE = always 8; cue fields BE; color ARGB
# ---------------------------------------------------------------------------


def _decode_color(raw: bytes, offset: int) -> tuple[Color, int]:
    a, r, g, b = raw[offset : offset + 4]
    return Color(a, r, g, b), offset + 4


def _encode_color(color: Color) -> bytes:
    return bytes(
        [color.a & 0xFF, color.r & 0xFF, color.g & 0xFF, color.b & 0xFF]
    )


def _decode_label(raw: bytes, offset: int) -> tuple[str, int]:
    if offset >= len(raw):
        raise ValueError("truncated label length")
    label_len = raw[offset]
    offset += 1
    if offset + label_len > len(raw):
        raise ValueError("truncated label")
    # latin-1 preserves any byte; Engine labels are ASCII in practice
    label = raw[offset : offset + label_len].decode("latin-1")
    return label, offset + label_len


def _encode_label(label: str) -> bytes:
    encoded = label.encode("latin-1")
    if len(encoded) > 255:
        raise ValueError(f"label too long: {len(encoded)} bytes")
    return bytes([len(encoded)]) + encoded


def _pad_quick_cues(cues: list[QuickCue]) -> list[QuickCue]:
    if len(cues) > MAX_QUICK_CUES:
        raise ValueError(f"at most {MAX_QUICK_CUES} quick cues, got {len(cues)}")
    padded = list(cues)
    while len(padded) < MAX_QUICK_CUES:
        padded.append(_empty_quick_cue())
    return padded


def decode_quick_cues(blob: bytes) -> QuickCues:
    if not blob:
        return QuickCues(
            cues=[_empty_quick_cue() for _ in range(MAX_QUICK_CUES)],
            adjusted_main_cue=_EMPTY_CUE_OFFSET,
            is_main_cue_adjusted=0,
            default_main_cue=_EMPTY_CUE_OFFSET,
        )
    raw = _decompress_frame(blob)
    if len(raw) < 8:
        raise ValueError("quickCues payload too short for count")
    # ⚠️ COUNT is BIG-ENDIAN (opposite of loops)
    count = struct.unpack_from(">q", raw, 0)[0]
    # Engine always writes 8; still parse whatever count says if plausible
    if count != MAX_QUICK_CUES and (count < 0 or count > 64):
        raise ValueError(f"implausible quickCues count: {count}")
    offset = 8
    cues: list[QuickCue] = []
    for _ in range(count):
        label, offset = _decode_label(raw, offset)
        if offset + 8 > len(raw):
            raise ValueError("truncated quickCue sample_offset")
        sample_offset = struct.unpack_from(">d", raw, offset)[0]
        offset += 8
        color, offset = _decode_color(raw, offset)
        cues.append(QuickCue(label, sample_offset, color))
    if offset + 17 > len(raw):
        raise ValueError("truncated quickCues main-cue trailer")
    adjusted_main_cue = struct.unpack_from(">d", raw, offset)[0]
    offset += 8
    is_main_cue_adjusted = raw[offset]
    offset += 1
    default_main_cue = struct.unpack_from(">d", raw, offset)[0]
    offset += 8
    extra_data = raw[offset:]
    # Normalize to exactly 8 for callers
    if len(cues) < MAX_QUICK_CUES:
        cues = _pad_quick_cues(cues)
    elif len(cues) > MAX_QUICK_CUES:
        cues = cues[:MAX_QUICK_CUES]
    return QuickCues(
        cues=cues,
        adjusted_main_cue=adjusted_main_cue,
        is_main_cue_adjusted=is_main_cue_adjusted,
        default_main_cue=default_main_cue,
        extra_data=extra_data,
    )


def encode_quick_cues(data: QuickCues) -> bytes:
    cues = _pad_quick_cues(list(data.cues))
    parts = [struct.pack(">q", MAX_QUICK_CUES)]  # always 8, BE
    for cue in cues:
        parts.append(_encode_label(cue.label))
        parts.append(struct.pack(">d", cue.sample_offset))
        parts.append(_encode_color(cue.color))
    parts.append(struct.pack(">d", data.adjusted_main_cue))
    parts.append(bytes([data.is_main_cue_adjusted & 0xFF]))
    parts.append(struct.pack(">d", data.default_main_cue))
    parts.append(data.extra_data)
    return _compress_frame(b"".join(parts))


# ---------------------------------------------------------------------------
# loops — NOT compressed; count int64 LE; loop fields LE
# ---------------------------------------------------------------------------


def _pad_loops(loops: list[Loop]) -> list[Loop]:
    if len(loops) > MAX_LOOPS:
        raise ValueError(f"at most {MAX_LOOPS} loops, got {len(loops)}")
    padded = list(loops)
    while len(padded) < MAX_LOOPS:
        padded.append(_empty_loop())
    return padded


def decode_loops(blob: bytes) -> Loops:
    if not blob:
        return Loops(loops=[_empty_loop() for _ in range(MAX_LOOPS)])
    if len(blob) < 8:
        raise ValueError("loops blob too short for count")
    # ⚠️ COUNT is LITTLE-ENDIAN (opposite of quickCues); NO zlib
    count = struct.unpack_from("<q", blob, 0)[0]
    if count < 0 or count > 64:
        raise ValueError(f"implausible loops count: {count}")
    offset = 8
    loops: list[Loop] = []
    for _ in range(count):
        label, offset = _decode_label(blob, offset)
        if offset + 18 > len(blob):
            raise ValueError("truncated loop entry")
        start = struct.unpack_from("<d", blob, offset)[0]
        offset += 8
        end = struct.unpack_from("<d", blob, offset)[0]
        offset += 8
        is_start_set = blob[offset]
        offset += 1
        is_end_set = blob[offset]
        offset += 1
        color, offset = _decode_color(blob, offset)
        loops.append(Loop(label, start, end, is_start_set, is_end_set, color))
    extra_data = blob[offset:]
    if len(loops) < MAX_LOOPS:
        loops = _pad_loops(loops)
    elif len(loops) > MAX_LOOPS:
        loops = loops[:MAX_LOOPS]
    return Loops(loops=loops, extra_data=extra_data)


def encode_loops(data: Loops) -> bytes:
    """Uncompressed; LE count always 8. Never zlib-frame this blob."""
    loops = _pad_loops(list(data.loops))
    parts = [struct.pack("<q", MAX_LOOPS)]  # LE count — opposite of quickCues
    for loop in loops:
        parts.append(_encode_label(loop.label))
        parts.append(struct.pack("<d", loop.start_sample_offset))
        parts.append(struct.pack("<d", loop.end_sample_offset))
        parts.append(bytes([loop.is_start_set & 0xFF, loop.is_end_set & 0xFF]))
        parts.append(_encode_color(loop.color))
    parts.append(data.extra_data)
    return b"".join(parts)


# ---------------------------------------------------------------------------
# overviewWaveFormData — zlib; decode only (we never write it)
# ---------------------------------------------------------------------------


def decode_overview_waveform(blob: bytes) -> OverviewWaveform:
    if not blob:
        return OverviewWaveform(0, 0.0, [], (0, 0, 0))
    raw = _decompress_frame(blob)
    if len(raw) < 24:
        raise ValueError(f"overviewWaveFormData too short: {len(raw)}")
    # Two redundant int64 BE counts
    count1 = struct.unpack_from(">q", raw, 0)[0]
    count2 = struct.unpack_from(">q", raw, 8)[0]
    if count1 != count2:
        raise ValueError(f"overview count mismatch: {count1} vs {count2}")
    samples_per_point = struct.unpack_from(">d", raw, 16)[0]
    offset = 24
    points: list[tuple[int, int, int]] = []
    for _ in range(count1):
        if offset + 3 > len(raw):
            raise ValueError("truncated overview waveform point")
        lo, mid, hi = raw[offset], raw[offset + 1], raw[offset + 2]
        points.append((lo, mid, hi))
        offset += 3
    if offset + 3 > len(raw):
        raise ValueError("truncated overview max point")
    max_point = (raw[offset], raw[offset + 1], raw[offset + 2])
    return OverviewWaveform(
        num_points=count1,
        samples_per_waveform_point=samples_per_point,
        points=points,
        max_point=max_point,
    )
