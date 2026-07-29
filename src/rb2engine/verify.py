"""Mechanical fidelity check: decode written m.db and diff against source IR.

``verify_library`` re-reads the source stick, re-maps each track through the
same mapper path the writer used, decodes PerformanceData blobs with the
golden-verified codecs, and reports per-field discrepancies at sample
granularity. A verifier that cannot fail is worthless — unit tests deliberately
corrupt m.db to prove each check fires.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rb2engine.chain import ChainInconsistent, walk_entity_chain
from rb2engine.errors import FatalError
from rb2engine.ir import SourceLibrary, SourcePlaylist, SourceTrack
from rb2engine.ir_engine import artwork_content_hash
from rb2engine.mapper.track import map_track
from rb2engine.reader.library import read_library
from rb2engine.writer.blobs import (
    decode_beat_data,
    decode_loops,
    decode_quick_cues,
    decode_track_data,
)
from rb2engine.writer.database import detect_schema

ENGINE_LIBRARY_DIRNAME = "Engine Library"
DATABASE2_DIRNAME = "Database2"
M_DB_NAME = "m.db"

# Empty-slot sentinel shared with writer/blobs and ir_engine.
_EMPTY_SAMPLE = -1.0


@dataclass(frozen=True, slots=True)
class Discrepancy:
    """One field-level mismatch between source expectation and written m.db."""

    track_id: int | None  # SourceTrack.rb_id; None for library-level checks
    field: str
    expected: Any
    actual: Any


@dataclass(frozen=True, slots=True)
class VerifyResult:
    """Outcome of ``verify_library``.

    Counts are per-track: a track is mismatched if it contributed ≥1
    discrepancy. Library-level discrepancies (playlist count/order, artwork)
    set ``ok`` False without necessarily inflating track counts.
    """

    checked: int
    matched: int
    mismatched: int
    discrepancies: list[Discrepancy] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return len(self.discrepancies) == 0

    def render_text(self) -> str:
        """Human-readable summary for CLI / convert post-pass."""
        status = "OK" if self.ok else "FAILED"
        lines = [
            "rb2engine verify",
            "---------------",
            f"Status:      {status}",
            f"Checked:     {self.checked}",
            f"Matched:     {self.matched}",
            f"Mismatched:  {self.mismatched}",
            f"Discrepancies: {len(self.discrepancies)}",
        ]
        if self.discrepancies:
            lines.append("")
            lines.append("Discrepancies:")
            for d in self.discrepancies:
                tid = "library" if d.track_id is None else str(d.track_id)
                lines.append(
                    f"  track {tid}: {d.field}: "
                    f"expected={d.expected!r} actual={d.actual!r}"
                )
        return "\n".join(lines) + "\n"


def verify_library(
    drive_root: Path,
    *,
    with_artwork: bool = True,
    sample: int | None = None,
) -> VerifyResult:
    """Decode written m.db and diff against a fresh parse of the source stick.

    Parameters
    ----------
    drive_root:
        Stick mount point containing both the source (PIONEER/export.pdb,
        Contents/) and the written ``Engine Library/Database2/m.db``.
    with_artwork:
        Forwarded to ``read_library``; when True, also compare AlbumArt row
        count to unique extracted artwork keys.
    sample:
        If set, only the first N source tracks (sorted by rb_id) are checked.
        Full-library verify over USB on ~3,600 tracks is slow.
    """
    drive_root = Path(drive_root)
    m_db_path = drive_root / ENGINE_LIBRARY_DIRNAME / DATABASE2_DIRNAME / M_DB_NAME
    if not m_db_path.is_file():
        raise FatalError(f"no m.db to verify at {m_db_path}")

    # Schema probe — confirms m.db is a readable Engine library with a
    # supported schema. Reading rows is schema-agnostic, but an unknown or
    # unreadable schema must not report a false OK (CLI exit 2).
    schema = detect_schema(m_db_path)
    if schema is None:
        raise FatalError(f"unreadable or non-Engine m.db at {m_db_path}")
    from rb2engine.writer.schema import resolve_schema

    resolve_schema(schema)

    source = read_library(drive_root, with_anlz=True, with_artwork=with_artwork)
    engine_lib = drive_root / ENGINE_LIBRARY_DIRNAME

    conn = sqlite3.connect(f"file:{m_db_path.resolve()}?mode=ro", uri=True)
    try:
        return _verify_against_open_db(
            source,
            conn,
            drive_root=drive_root,
            engine_lib=engine_lib,
            with_artwork=with_artwork,
            sample=sample,
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


@dataclass
class _DbTrack:
    id: int
    path: str
    title: str
    artist: str
    album: str
    genre: str
    bpm: int | None
    bpm_analyzed: float | None
    key: int | None
    track_data: bytes | None
    beat_data: bytes | None
    quick_cues: bytes | None
    loops: bytes | None


def _verify_against_open_db(
    source: SourceLibrary,
    conn: sqlite3.Connection,
    *,
    drive_root: Path,
    engine_lib: Path,
    with_artwork: bool,
    sample: int | None,
) -> VerifyResult:
    db_tracks = _load_db_tracks(conn)
    by_path = {t.path: t for t in db_tracks}
    discrepancies: list[Discrepancy] = []

    rb_ids = sorted(source.tracks.keys())
    if sample is not None:
        if sample < 0:
            raise ValueError(f"sample must be >= 0, got {sample}")
        rb_ids = rb_ids[:sample]

    checked = 0
    matched = 0
    mismatched = 0

    for rb_id in rb_ids:
        src = source.tracks[rb_id]
        checked += 1
        before = len(discrepancies)
        _compare_track(
            src,
            by_path,
            drive_root=drive_root,
            engine_lib=engine_lib,
            discrepancies=discrepancies,
        )
        if len(discrepancies) > before:
            mismatched += 1
        else:
            matched += 1

    _compare_playlists(
        source,
        conn,
        by_path=by_path,
        drive_root=drive_root,
        engine_lib=engine_lib,
        discrepancies=discrepancies,
    )

    if with_artwork:
        _compare_artwork(source, conn, discrepancies=discrepancies)

    return VerifyResult(
        checked=checked,
        matched=matched,
        mismatched=mismatched,
        discrepancies=discrepancies,
    )


def _load_db_tracks(conn: sqlite3.Connection) -> list[_DbTrack]:
    rows = conn.execute(
        """
        SELECT
            t.id, t.path, t.title, t.artist, t.album, t.genre,
            t.bpm, t.bpmAnalyzed, t.key,
            p.trackData, p.beatData, p.quickCues, p.loops
        FROM Track t
        LEFT JOIN PerformanceData p ON p.trackId = t.id
        """
    ).fetchall()
    out: list[_DbTrack] = []
    for r in rows:
        out.append(
            _DbTrack(
                id=int(r[0]),
                path=r[1] or "",
                title=r[2] or "",
                artist=r[3] or "",
                album=r[4] or "",
                genre=r[5] or "",
                bpm=None if r[6] is None else int(r[6]),
                bpm_analyzed=None if r[7] is None else float(r[7]),
                key=None if r[8] is None else int(r[8]),
                track_data=r[9],
                beat_data=r[10],
                quick_cues=r[11],
                loops=r[12],
            )
        )
    return out


def _compare_track(
    src: SourceTrack,
    by_path: dict[str, _DbTrack],
    *,
    drive_root: Path,
    engine_lib: Path,
    discrepancies: list[Discrepancy],
) -> None:
    rb_id = src.rb_id
    expected = map_track(
        src, drive_root=drive_root, engine_library_dir=engine_lib
    )
    db = by_path.get(expected.path)
    if db is None:
        discrepancies.append(
            Discrepancy(
                track_id=rb_id,
                field="missing",
                expected="present",
                actual="absent",
            )
        )
        return

    # Path must resolve to an existing audio file relative to Engine Library/.
    resolved = _resolve_track_path(engine_lib, db.path)
    if not resolved.is_file():
        discrepancies.append(
            Discrepancy(
                track_id=rb_id,
                field="path_exists",
                expected=True,
                actual=False,
            )
        )

    _eq(rb_id, "title", expected.title, db.title, discrepancies)
    _eq(rb_id, "artist", expected.artist, db.artist, discrepancies)
    _eq(rb_id, "album", expected.album, db.album, discrepancies)
    _eq(rb_id, "genre", expected.genre, db.genre, discrepancies)
    _eq(rb_id, "bpm", int(expected.bpm), db.bpm, discrepancies)
    _eq(
        rb_id,
        "bpm_analyzed",
        float(expected.bpm_analyzed),
        db.bpm_analyzed,
        discrepancies,
    )
    _eq(rb_id, "key", expected.key, db.key, discrepancies)

    # trackData.key ordinal (blob) — may differ from Track.key only when key
    # is None (blob stores 0); compare against mapper expectation.
    _compare_track_data_blob(rb_id, expected, db, discrepancies)
    _compare_beatgrid(rb_id, expected, db, discrepancies)
    _compare_quick_cues(rb_id, expected, db, discrepancies)
    _compare_loops(rb_id, expected, db, discrepancies)


def _compare_track_data_blob(
    rb_id: int,
    expected: Any,
    db: _DbTrack,
    discrepancies: list[Discrepancy],
) -> None:
    try:
        td = decode_track_data(db.track_data or b"")
    except (ValueError, Exception) as exc:  # noqa: BLE001 - report decode failure as discrepancy
        discrepancies.append(
            Discrepancy(
                track_id=rb_id,
                field="track_data",
                expected="decodable",
                actual=f"decode_error: {exc}",
            )
        )
        return
    exp_key = int(expected.key) if expected.key is not None else 0
    if int(td.key) != exp_key:
        discrepancies.append(
            Discrepancy(
                track_id=rb_id,
                field="track_data.key",
                expected=exp_key,
                actual=int(td.key),
            )
        )


def _compare_beatgrid(
    rb_id: int,
    expected: Any,
    db: _DbTrack,
    discrepancies: list[Discrepancy],
) -> None:
    try:
        bd = decode_beat_data(db.beat_data or b"")
    except (ValueError, Exception) as exc:  # noqa: BLE001
        discrepancies.append(
            Discrepancy(
                track_id=rb_id,
                field="beat_data",
                expected="decodable",
                actual=f"decode_error: {exc}",
            )
        )
        return

    exp_markers = expected.beat_grid.default_markers
    act_markers = bd.default_beat_grid.markers

    # Exact integer equality on sample offsets (no float tolerance).
    exp_offsets = [int(m.sample_offset) for m in exp_markers]
    act_offsets = [int(m.sample_offset) for m in act_markers]
    if exp_offsets != act_offsets:
        discrepancies.append(
            Discrepancy(
                track_id=rb_id,
                field="beatgrid.sample_offsets",
                expected=exp_offsets,
                actual=act_offsets,
            )
        )


def _compare_quick_cues(
    rb_id: int,
    expected: Any,
    db: _DbTrack,
    discrepancies: list[Discrepancy],
) -> None:
    try:
        qc = decode_quick_cues(db.quick_cues or b"")
    except (ValueError, Exception) as exc:  # noqa: BLE001
        discrepancies.append(
            Discrepancy(
                track_id=rb_id,
                field="quick_cues",
                expected="decodable",
                actual=f"decode_error: {exc}",
            )
        )
        return

    # Pad both sides to 8 so pad indices line up; None means "empty slot".
    exp_slots: list[Any] = list(expected.quick_cues)
    act_slots: list[Any] = list(qc.cues)
    # Normalize lengths to 8 for pad-index comparison.
    while len(exp_slots) < 8:
        exp_slots.append(None)
    while len(act_slots) < 8:
        act_slots.append(None)

    for pad in range(8):
        exp = exp_slots[pad]
        act = act_slots[pad]
        if exp is None and act is None:
            continue
        if exp is None:
            continue

        exp_off = int(exp.sample_offset)
        act_off = int(act.sample_offset) if act is not None else None
        if exp_off != act_off:
            # Empty expected vs empty actual is fine.
            if exp.sample_offset == _EMPTY_SAMPLE and (
                act is None or act.sample_offset == _EMPTY_SAMPLE
            ):
                pass
            else:
                discrepancies.append(
                    Discrepancy(
                        track_id=rb_id,
                        field=f"quick_cue[{pad}].sample_offset",
                        expected=exp_off,
                        actual=act_off,
                    )
                )

        exp_label = exp.label
        act_label = act.label if act is not None else None
        if exp_label != act_label:
            discrepancies.append(
                Discrepancy(
                    track_id=rb_id,
                    field=f"quick_cue[{pad}].label",
                    expected=exp_label,
                    actual=act_label,
                )
            )

        exp_color = tuple(exp.color)
        act_color = (
            (act.color.a, act.color.r, act.color.g, act.color.b)
            if act is not None
            else None
        )
        if exp_color != act_color:
            discrepancies.append(
                Discrepancy(
                    track_id=rb_id,
                    field=f"quick_cue[{pad}].color",
                    expected=exp_color,
                    actual=act_color,
                )
            )


def _compare_loops(
    rb_id: int,
    expected: Any,
    db: _DbTrack,
    discrepancies: list[Discrepancy],
) -> None:
    try:
        lp = decode_loops(db.loops or b"")
    except (ValueError, Exception) as exc:  # noqa: BLE001
        discrepancies.append(
            Discrepancy(
                track_id=rb_id,
                field="loops",
                expected="decodable",
                actual=f"decode_error: {exc}",
            )
        )
        return

    exp_slots = list(expected.loops)
    act_slots = list(lp.loops)
    n = max(len(exp_slots), len(act_slots), 8)
    for i in range(n):
        exp = exp_slots[i] if i < len(exp_slots) else None
        act = act_slots[i] if i < len(act_slots) else None
        if exp is None:
            continue
        exp_start = int(exp.start_sample_offset)
        exp_end = int(exp.end_sample_offset)
        act_start = int(act.start_sample_offset) if act is not None else None
        act_end = int(act.end_sample_offset) if act is not None else None
        if exp_start == int(_EMPTY_SAMPLE) and (
            act is None or act.start_sample_offset == _EMPTY_SAMPLE
        ):
            continue
        if exp_start != act_start:
            discrepancies.append(
                Discrepancy(
                    track_id=rb_id,
                    field=f"loop[{i}].start",
                    expected=exp_start,
                    actual=act_start,
                )
            )
        if exp_end != act_end:
            discrepancies.append(
                Discrepancy(
                    track_id=rb_id,
                    field=f"loop[{i}].end",
                    expected=exp_end,
                    actual=act_end,
                )
            )


def _compare_playlists(
    source: SourceLibrary,
    conn: sqlite3.Connection,
    *,
    by_path: dict[str, _DbTrack],
    drive_root: Path,
    engine_lib: Path,
    discrepancies: list[Discrepancy],
) -> None:
    db_count = conn.execute("SELECT COUNT(*) FROM Playlist").fetchone()[0]
    exp_count = len(source.playlists)
    if int(db_count) != exp_count:
        discrepancies.append(
            Discrepancy(
                track_id=None,
                field="playlist_count",
                expected=exp_count,
                actual=int(db_count),
            )
        )

    # Title → list id (first match; Engine renames duplicates with " (N)").
    title_to_id: dict[str, int] = {}
    for row in conn.execute("SELECT id, title FROM Playlist"):
        title_to_id[str(row[1])] = int(row[0])

    for pl in source.playlists:
        list_id = title_to_id.get(pl.name)
        if list_id is None:
            # Renamed duplicate titles — try ordered suffix scan.
            list_id = _find_playlist_id(title_to_id, pl)
        if list_id is None:
            discrepancies.append(
                Discrepancy(
                    track_id=None,
                    field=f"playlist[{pl.name}].missing",
                    expected="present",
                    actual="absent",
                )
            )
            continue

        expected_track_ids = _expected_entity_track_ids(
            pl, source, by_path=by_path, drive_root=drive_root, engine_lib=engine_lib
        )
        actual_track_ids, chain_problem = _entity_track_order(conn, list_id)
        if chain_problem is not None:
            # Report it in its own right: a broken chain is a defect even when
            # the set of tracks happens to match what the source expected.
            discrepancies.append(
                Discrepancy(
                    track_id=None,
                    field=f"playlist[{pl.name}].chain",
                    expected="every row reachable from the nextEntityId chain",
                    actual=chain_problem,
                )
            )
        if expected_track_ids != actual_track_ids:
            discrepancies.append(
                Discrepancy(
                    track_id=None,
                    field=f"playlist[{pl.name}].track_order",
                    expected=expected_track_ids,
                    actual=actual_track_ids,
                )
            )


def _find_playlist_id(
    title_to_id: dict[str, int], pl: SourcePlaylist
) -> int | None:
    if pl.name in title_to_id:
        return title_to_id[pl.name]
    # insert_playlists renames duplicates to "Name (2)", "Name (3)", …
    for title, lid in title_to_id.items():
        if title == pl.name or title.startswith(f"{pl.name} ("):
            return lid
    return None


def _expected_entity_track_ids(
    pl: SourcePlaylist,
    source: SourceLibrary,
    *,
    by_path: dict[str, _DbTrack],
    drive_root: Path,
    engine_lib: Path,
) -> list[int]:
    """Map source track_rb_ids → Engine Track.id via expected path (order preserved)."""
    out: list[int] = []
    seen: set[int] = set()
    for rb in pl.track_rb_ids:
        src = source.tracks.get(rb)
        if src is None:
            continue
        if src.resolved_path is None:
            continue
        et = map_track(src, drive_root=drive_root, engine_library_dir=engine_lib)
        db = by_path.get(et.path)
        if db is None:
            continue
        if db.id in seen:
            continue  # Engine de-dupes within a playlist (first occurrence wins)
        seen.add(db.id)
        out.append(db.id)
    return out


def _entity_track_order(
    conn: sqlite3.Connection, list_id: int
) -> tuple[list[int], str | None]:
    """Track order from the nextEntityId chain, plus any inconsistency found.

    Returns ``(order, problem)``. ``problem`` is None when the chain accounts
    for every row; otherwise it describes what is wrong and ``order`` holds the
    rows in id order as a best effort.

    This used to swallow an inconsistent chain and return the shorter, tidier
    list, which reported a clean library while the writer's own gate would
    refuse to publish that exact database. Both now walk the same code
    (``rb2engine.chain``); they differ only in how they react, because ``verify``
    must record the finding and keep checking rather than abort.
    """
    rows = [
        (int(eid), int(track_id), int(next_id))
        for eid, track_id, next_id in conn.execute(
            "SELECT id, trackId, nextEntityId FROM PlaylistEntity WHERE listId = ?",
            (list_id,),
        )
    ]
    try:
        return walk_entity_chain(list_id, rows), None
    except ChainInconsistent as exc:
        return [track_id for _, track_id, _ in rows], str(exc)


def _compare_artwork(
    source: SourceLibrary,
    conn: sqlite3.Connection,
    *,
    discrepancies: list[Discrepancy],
) -> None:
    keys: set[str] = set()
    for t in source.tracks.values():
        if t.artwork is not None and t.artwork.content_key:
            keys.add(t.artwork.content_key)
    expected = len(keys)
    actual = int(conn.execute("SELECT COUNT(*) FROM AlbumArt").fetchone()[0])
    if expected != actual:
        discrepancies.append(
            Discrepancy(
                track_id=None,
                field="album_art_count",
                expected=expected,
                actual=actual,
            )
        )

    # Row count alone is not verification: swapped, truncated or re-encoded
    # image bytes keep the count identical while the user sees wrong covers.
    # Recompute the dedup key over the stored BLOB and compare it to the keys
    # the source produced — that is the same function the writer used, so any
    # byte drift shows up as a key that should not exist.
    if not keys:
        return
    for row_id, blob in conn.execute(
        "SELECT id, albumArt FROM AlbumArt ORDER BY id"
    ).fetchall():
        if not blob:
            discrepancies.append(
                Discrepancy(
                    track_id=None,
                    field=f"album_art[{row_id}].bytes",
                    expected="non-empty image",
                    actual="empty blob",
                )
            )
            continue
        stored_key = artwork_content_hash(bytes(blob))
        if stored_key not in keys:
            discrepancies.append(
                Discrepancy(
                    track_id=None,
                    field=f"album_art[{row_id}].content_key",
                    expected=f"one of {len(keys)} source artwork keys",
                    actual=stored_key,
                )
            )


def _resolve_track_path(engine_lib: Path, track_path: str) -> Path:
    """Resolve Track.path relative to Engine Library/ (../Contents/… form)."""
    p = Path(track_path)
    if p.is_absolute():
        return p
    # Pure relative (including .. segments): resolve against Engine Library.
    return (engine_lib / track_path).resolve()


def _eq(
    track_id: int | None,
    field_name: str,
    expected: Any,
    actual: Any,
    discrepancies: list[Discrepancy],
) -> None:
    if expected != actual:
        discrepancies.append(
            Discrepancy(
                track_id=track_id,
                field=field_name,
                expected=expected,
                actual=actual,
            )
        )
