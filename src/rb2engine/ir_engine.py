"""Engine-side mapped IR: what the writer consumes.

Produced by mapper/; consumed by writer/. Never import writer from here —
codec types in writer/blobs.py are the on-disk encoding face; these types are
the mapper→writer contract and are field-aligned with the blob encoder.

Image bytes never enter this IR: EngineAlbumArt carries a Path only.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

# Empty-slot sentinels (aligned with writer/blobs.py).
EMPTY_SAMPLE_OFFSET = -1.0
EMPTY_ARGB: tuple[int, int, int, int] = (0, 0, 0, 0)
NUM_SLOTS = 8


def artwork_content_hash(image_bytes: bytes) -> str:
    """sha1 of image bytes, lowercase hex, leading zero digits stripped.

    Matches the shape Engine stores (observed 39-char values = 40-char sha1
    with a leading zero removed). Used as the internal AlbumArt dedup key.
    """
    digest = hashlib.sha1(image_bytes).hexdigest()
    return digest.lstrip("0") or "0"


@dataclass(frozen=True, slots=True)
class QuickCueSlot:
    """One Engine quick-cue pad. Empty sentinel: sample_offset=-1.0, color zeros."""

    label: str
    sample_offset: float
    color: tuple[int, int, int, int]  # ARGB


@dataclass(frozen=True, slots=True)
class LoopSlot:
    """One Engine loop slot. Empty sentinel: offsets=-1.0, flags=0, color zeros."""

    label: str
    start_sample_offset: float
    end_sample_offset: float
    is_start_set: int
    is_end_set: int
    color: tuple[int, int, int, int]  # ARGB


@dataclass(frozen=True, slots=True)
class EngineBeatMarker:
    """One beat-grid marker — fields the beatData encoder packs (LE internals)."""

    sample_offset: float
    beat_number: int
    number_of_beats: int
    unknown: int = 0


@dataclass(frozen=True, slots=True)
class EngineBeatGrid:
    """Marker lists the beatData blob encoder needs (default + adjusted)."""

    default_markers: list[EngineBeatMarker] = field(default_factory=list)
    adjusted_markers: list[EngineBeatMarker] = field(default_factory=list)
    is_beatgrid_set: bool = False


@dataclass(frozen=True, slots=True)
class EngineAlbumArt:
    """Deduped artwork row. Path only — never image bytes (library-scale memory)."""

    hash: str
    image_path: Path


@dataclass(frozen=True, slots=True)
class EngineTrack:
    """Mapped track ready for writer/tracks.py + blob encoding."""

    path: str
    title: str
    artist: str
    album: str
    genre: str
    label: str
    comment: str
    composer: str
    year: int
    track_number: int | None
    disc_number: int | None
    bpm: int
    bpm_analyzed: float
    key: int | None  # Engine ordinal 0–23; None → Track.key NULL, blob key 0
    rating: int
    sample_rate: float
    samples: int
    date_added: int | None
    date_created: int | None
    last_edit_time: int | None
    album_art_hash: str | None
    beat_grid: EngineBeatGrid
    quick_cues: list[QuickCueSlot]  # always length 8
    loops: list[LoopSlot]  # always length 8


@dataclass(frozen=True, slots=True)
class EngineLibrary:
    """Full mapped library: tracks + deduped art in insertion (= id) order."""

    database_uuid: str
    tracks: list[EngineTrack]
    album_art: list[EngineAlbumArt]


EMPTY_QUICK_CUE = QuickCueSlot(
    label="",
    sample_offset=EMPTY_SAMPLE_OFFSET,
    color=EMPTY_ARGB,
)

EMPTY_LOOP = LoopSlot(
    label="",
    start_sample_offset=EMPTY_SAMPLE_OFFSET,
    end_sample_offset=EMPTY_SAMPLE_OFFSET,
    is_start_set=0,
    is_end_set=0,
    color=EMPTY_ARGB,
)
