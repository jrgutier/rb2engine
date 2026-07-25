"""Track INSERT → PerformanceData UPDATE for trigger-created rows.

Never INSERT into PerformanceData — trigger_after_insert_Track_insert_performance_data
already creates the row. overviewWaveFormData is left at the trigger default
(NULL): Engine regenerates waveforms on analysis (project non-goal).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from pathlib import PurePosixPath

from rb2engine.ir_engine import EngineTrack
from rb2engine.writer.blobs import (
    BeatData,
    BeatGrid,
    BeatMarker,
    Color,
    Loop,
    Loops,
    QuickCue,
    QuickCues,
    TrackData,
    encode_beat_data,
    encode_loops,
    encode_quick_cues,
    encode_track_data,
)

# Engine-exported shape for Track.albumArt when albumArtId is set.
# Whether Engine requires this URI for rendering is unverified; replicate it.
_ALBUM_ART_URI = "image://planck/0"


def _filename_from_path(path: str) -> str:
    # Track.path is always forward-slash (PurePosixPath contract).
    return PurePosixPath(path).name


def _file_type_from_path(path: str) -> str | None:
    suffix = PurePosixPath(path).suffix
    if not suffix:
        return None
    return suffix.lstrip(".").lower() or None


def _length_seconds(track: EngineTrack) -> int | None:
    """Track.length is integer seconds; prefer explicit attr, else samples/rate."""
    explicit = getattr(track, "length", None)
    if explicit is not None:
        return int(explicit)
    if track.sample_rate and track.sample_rate > 0:
        return int(track.samples // track.sample_rate)
    return None


def _track_data(track: EngineTrack) -> TrackData:
    # key None → blob key 0 (Track.key column stays NULL separately).
    key = int(track.key) if track.key is not None else 0
    return TrackData(
        sample_rate=float(track.sample_rate),
        samples=int(track.samples),
        key=key,
    )


def _beat_data(track: EngineTrack) -> BeatData:
    bg = track.beat_grid
    return BeatData(
        sample_rate=float(track.sample_rate),
        samples=float(track.samples),
        is_beatgrid_set=1 if bg.is_beatgrid_set else 0,
        default_beat_grid=BeatGrid(
            markers=[
                BeatMarker(
                    m.sample_offset, m.beat_number, m.number_of_beats, m.unknown
                )
                for m in bg.default_markers
            ]
        ),
        adjusted_beat_grid=BeatGrid(
            markers=[
                BeatMarker(
                    m.sample_offset, m.beat_number, m.number_of_beats, m.unknown
                )
                for m in bg.adjusted_markers
            ]
        ),
    )


def _quick_cues(track: EngineTrack) -> QuickCues:
    return QuickCues(
        cues=[
            QuickCue(s.label, float(s.sample_offset), Color(*s.color))
            for s in track.quick_cues
        ]
    )


def _loops(track: EngineTrack) -> Loops:
    return Loops(
        loops=[
            Loop(
                s.label,
                float(s.start_sample_offset),
                float(s.end_sample_offset),
                int(s.is_start_set),
                int(s.is_end_set),
                Color(*s.color),
            )
            for s in track.loops
        ]
    )


def _resolve_art(
    track: EngineTrack, art_ids: Mapping[str, int] | None
) -> tuple[int | None, str | None]:
    if not track.album_art_hash or not art_ids:
        return None, None
    art_id = art_ids.get(track.album_art_hash)
    if art_id is None:
        return None, None
    return int(art_id), _ALBUM_ART_URI


def _insert_one_track(
    conn: sqlite3.Connection,
    track: EngineTrack,
    *,
    art_ids: Mapping[str, int] | None,
) -> int:
    album_art_id, album_art_uri = _resolve_art(track, art_ids)
    filename = _filename_from_path(track.path)
    file_type = getattr(track, "file_type", None) or _file_type_from_path(track.path)
    bitrate = getattr(track, "bitrate", None)
    file_bytes = getattr(track, "file_size", None)
    if file_bytes is None:
        file_bytes = getattr(track, "file_bytes", None)
    remixer = getattr(track, "remixer", None)
    length = _length_seconds(track)

    # Column-name-explicit INSERT works on both 3.0.1 (42 cols) and 3.0.2
    # (43 cols, albumArtSourceHash). Never list albumArtSourceHash — leave default.
    # origin* left NULL/0 so trigger_after_insert_Track_fix_origin fills them
    # from Information.uuid and NEW.id (verified against captured DDL).
    cur = conn.execute(
        """
        INSERT INTO Track (
            playOrder,
            length,
            bpm,
            year,
            path,
            filename,
            bitrate,
            bpmAnalyzed,
            albumArtId,
            fileBytes,
            title,
            artist,
            album,
            genre,
            comment,
            label,
            composer,
            remixer,
            key,
            rating,
            albumArt,
            timeLastPlayed,
            isPlayed,
            fileType,
            isAnalyzed,
            dateCreated,
            dateAdded,
            isAvailable,
            isMetadataOfPackedTrackChanged,
            isPerfomanceDataOfPackedTrackChanged,
            playedIndicator,
            isMetadataImported,
            pdbImportKey,
            streamingSource,
            uri,
            isBeatGridLocked,
            originDatabaseUuid,
            originTrackId,
            streamingFlags,
            explicitLyrics,
            lastEditTime
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?
        )
        """,
        (
            None,  # playOrder
            length,
            int(track.bpm),
            int(track.year) if track.year else None,
            track.path,  # verbatim — do not recompute
            filename,
            int(bitrate) if bitrate is not None else None,
            float(track.bpm_analyzed),
            album_art_id,
            int(file_bytes) if file_bytes is not None else None,
            track.title,
            track.artist,
            track.album,
            track.genre,
            track.comment,
            track.label,
            track.composer,
            remixer,
            int(track.key) if track.key is not None else None,
            int(track.rating),
            album_art_uri,
            None,  # timeLastPlayed
            0,  # isPlayed
            file_type,
            1,  # isAnalyzed — we write performance blobs
            track.date_created,
            track.date_added,
            1,  # isAvailable
            0,  # isMetadataOfPackedTrackChanged
            0,  # isPerfomanceDataOfPackedTrackChanged (Engine spelling)
            None,  # playedIndicator
            1,  # isMetadataImported
            int(getattr(track, "rb_id", 0) or 0),  # pdbImportKey: stable rb id when present
            None,  # streamingSource
            None,  # uri
            0,  # isBeatGridLocked
            None,  # originDatabaseUuid — trigger fills
            None,  # originTrackId — trigger fills
            0,  # streamingFlags
            0,  # explicitLyrics
            track.last_edit_time,
        ),
    )
    track_id = cur.lastrowid
    if track_id is None:
        raise RuntimeError("Track INSERT did not produce lastrowid")
    return int(track_id)


def _update_performance_data(conn: sqlite3.Connection, track_id: int, track: EngineTrack) -> None:
    """UPDATE only the four blob columns we own — never overviewWaveFormData."""
    conn.execute(
        """
        UPDATE PerformanceData
        SET trackData = ?,
            beatData = ?,
            quickCues = ?,
            loops = ?
        WHERE trackId = ?
        """,
        (
            encode_track_data(_track_data(track)),
            encode_beat_data(_beat_data(track)),
            encode_quick_cues(_quick_cues(track)),
            encode_loops(_loops(track)),
            track_id,
        ),
    )


def insert_tracks(
    conn: sqlite3.Connection,
    tracks: Sequence[EngineTrack],
    *,
    art_ids: Mapping[str, int] | None = None,
) -> dict[int, int]:
    """INSERT INTO Track, then UPDATE PerformanceData (trigger-created rows).

    Blobs via writer.blobs encode_*. Returns rb_id → Engine Track.id.

    EngineTrack currently has no ``rb_id`` field (IR seam gap vs CONTRACT).
    When absent, the map key falls back to the Engine Track.id so the return
    value stays a usable bijection; when ``rb_id`` is present (getattr), that
    is the key. pdbImportKey is also set from rb_id when available.
    """
    id_map: dict[int, int] = {}
    for track in tracks:
        track_id = _insert_one_track(conn, track, art_ids=art_ids)
        # Trigger must have created PerformanceData; only UPDATE.
        exists = conn.execute(
            "SELECT 1 FROM PerformanceData WHERE trackId = ?",
            (track_id,),
        ).fetchone()
        if exists is None:
            raise RuntimeError(
                f"PerformanceData row missing for trackId={track_id}: "
                "trigger_after_insert_Track_insert_performance_data did not fire "
                "(DDL incomplete?)"
            )
        _update_performance_data(conn, track_id, track)

        rb_id = getattr(track, "rb_id", None)
        rb_id = track_id if rb_id is None else int(rb_id)
        id_map[rb_id] = track_id
    return id_map
