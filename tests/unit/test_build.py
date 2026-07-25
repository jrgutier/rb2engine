"""Tests for writer/build.py — atomic m.db swap and safety boundary.

WHY: this is the only module that writes to the user's stick (44 GB of
irreplaceable music). The properties under test are the safety contract, not
implementation details:

1. Only ``Engine Library/`` is ever written; ``PIONEER/`` and ``Contents/`` are
   byte-identical inputs that must never be touched.
2. ``os.replace(m.db.tmp, m.db)`` is the unit of publication — a crash mid-
   write leaves the prior ``m.db`` intact and no orphaned ``.tmp``.
3. Sibling Engine state (``hm.db``, ``sm.db``, ``stm.db``, ``Music/``, foreign
   files) is preserved byte-identical across success and failure.
4. A stray ``m.db.tmp`` from a crashed prior run is deleted, never adopted.
5. Playlist linked lists reconstruct after a full build (orchestration check).

All paths use pytest's ``tmp_path`` as a fake drive — never a real stick.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from rb2engine.errors import FatalError
from rb2engine.ir import SourceLibrary, SourcePlaylist, SourceTrack
from rb2engine.ir_engine import (
    EMPTY_LOOP,
    EMPTY_QUICK_CUE,
    EngineBeatGrid,
    EngineTrack,
)
from rb2engine.report import ConversionReport
from rb2engine.writer import schema as schema_mod

# ---------------------------------------------------------------------------
# Dependency fakes — isolate build from concurrent workers' modules
# ---------------------------------------------------------------------------

def _install_database_fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide contract-shaped create_m_db / detect_schema via schema.py."""
    import rb2engine.writer.database as database_mod

    def create_m_db(
        path: Path, *, schema: tuple[int, int, int], uuid: str | None = None
    ) -> sqlite3.Connection:
        return schema_mod.create_database(
            path, schema, database_uuid=uuid
        )

    def detect_schema(m_db_path: Path) -> tuple[int, int, int] | None:
        p = Path(m_db_path)
        if not p.is_file():
            return None
        try:
            conn = sqlite3.connect(f"file:{p.resolve()}?mode=ro", uri=True)
            try:
                row = conn.execute(
                    "SELECT schemaVersionMajor, schemaVersionMinor, "
                    "schemaVersionPatch FROM Information LIMIT 1"
                ).fetchone()
            finally:
                conn.close()
        except sqlite3.Error:
            return None
        if row is None:
            return None
        return int(row[0]), int(row[1]), int(row[2])

    monkeypatch.setattr(database_mod, "create_m_db", create_m_db, raising=False)
    monkeypatch.setattr(database_mod, "detect_schema", detect_schema, raising=False)


def _minimal_engine_track(path: str = "../Contents/a.mp3") -> EngineTrack:
    return EngineTrack(
        path=path,
        title="T",
        artist="A",
        album="",
        genre="",
        label="",
        comment="",
        composer="",
        year=0,
        track_number=None,
        disc_number=None,
        bpm=120,
        bpm_analyzed=120.0,
        key=None,
        rating=0,
        sample_rate=44100.0,
        samples=44100,
        date_added=None,
        date_created=None,
        last_edit_time=None,
        album_art_hash=None,
        beat_grid=EngineBeatGrid(),
        quick_cues=[EMPTY_QUICK_CUE] * 8,
        loops=[EMPTY_LOOP] * 8,
    )


def _install_pipeline_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    fail_after_tmp: bool = False,
) -> dict[str, Any]:
    """Stub map/artwork/tracks so build can run without those workers.

    When fail_after_tmp is True, raise after the tmp DB exists (simulates a
    mid-write fatal before os.replace).
    """
    import rb2engine.mapper.track as map_track_mod
    import rb2engine.writer.artwork as artwork_mod
    import rb2engine.writer.tracks as tracks_mod

    state: dict[str, Any] = {"mapped": 0}

    def map_track(
        src: SourceTrack, *, drive_root: Path, engine_library_dir: Path
    ) -> EngineTrack:
        state["mapped"] += 1
        path = f"../Contents/track_{src.rb_id}.mp3"
        return _minimal_engine_track(path=path)

    def insert_artwork(conn: sqlite3.Connection, arts: Sequence[Any]) -> dict[str, int]:
        return {}

    def insert_tracks(
        conn: sqlite3.Connection,
        tracks: Sequence[EngineTrack],
        *,
        art_ids: Mapping[str, int] | None = None,
    ) -> dict[int, int]:
        if fail_after_tmp:
            raise RuntimeError("simulated mid-write failure")
        # Map by path suffix rb_id if present; else sequential.
        # build passes EngineTracks in source order with rb_id known via pairing.
        # Our fake is invoked with tracks only — build must pass rb_ids via
        # a parallel structure. We read origin from a side channel:
        rb_ids: list[int] = state.get("rb_ids", list(range(1, len(tracks) + 1)))
        out: dict[int, int] = {}
        for i, et in enumerate(tracks):
            rb = rb_ids[i] if i < len(rb_ids) else i + 1
            cur = conn.execute(
                "INSERT INTO Track (path, title, artist, originDatabaseUuid, "
                "originTrackId) VALUES (?, ?, ?, "
                "(SELECT uuid FROM Information LIMIT 1), ?)",
                (et.path, et.title, et.artist, rb),
            )
            out[rb] = int(cur.lastrowid)
        return out

    monkeypatch.setattr(map_track_mod, "map_track", map_track, raising=False)
    monkeypatch.setattr(artwork_mod, "insert_artwork", insert_artwork, raising=False)
    monkeypatch.setattr(tracks_mod, "insert_tracks", insert_tracks, raising=False)
    return state


def _source_track(
    rb_id: int, title: str = "t", *, drive: Path | None = None
) -> SourceTrack:
    # resolved_path must be set for conversion; build skips None as unresolvable.
    resolved = (drive / "Contents" / f"{rb_id}.mp3") if drive is not None else Path(
        f"/fake/Contents/{rb_id}.mp3"
    )
    return SourceTrack(
        rb_id=rb_id,
        title=title,
        artist="a",
        album="",
        genre="",
        label="",
        comment="",
        composer="",
        remixer="",
        year=0,
        track_number=None,
        disc_number=None,
        bpm=120.0,
        key_name=None,
        rating=0,
        play_count=0,
        bitrate=320,
        file_size=1000,
        file_type="mp3",
        sample_rate=44100,
        duration_s=60,
        total_samples=44100 * 60,
        raw_path=f"/Contents/{rb_id}.mp3",
        resolved_path=resolved,
        beatgrid=None,
        cues=[],
        artwork=None,
    )


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def _seed_existing_library(drive: Path, *, uuid: str = "prior-uuid-0001") -> Path:
    """Create Engine Library with m.db + siblings + foreign file; return m.db."""
    eng = drive / "Engine Library"
    db2 = eng / "Database2"
    db2.mkdir(parents=True)
    (eng / "Music").mkdir()
    (eng / "Music" / "keep.me").write_bytes(b"music-bytes-v1")
    (eng / "Artwork").mkdir()
    (eng / "OverviewData").mkdir()
    (eng / "foreign-keep.bin").write_bytes(b"foreign-payload")

    (db2 / "hm.db").write_bytes(b"hm-sibling-content")
    (db2 / "sm.db").write_bytes(b"sm-sibling-content")
    (db2 / "stm.db").write_bytes(b"stm-sibling-content")

    m_db = db2 / "m.db"
    conn = schema_mod.create_database(m_db, (3, 0, 1), database_uuid=uuid)
    conn.execute(
        "INSERT INTO Track (path, title, originDatabaseUuid, originTrackId) "
        "VALUES ('old', 'prior-track', ?, 1)",
        (uuid,),
    )
    conn.commit()
    conn.close()
    return m_db


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_fresh_drive_gets_valid_library(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty drive → Engine Library/Database2/m.db with Information + schema."""
    _install_database_fakes(monkeypatch)
    state = _install_pipeline_fakes(monkeypatch)

    drive = tmp_path / "stick"
    drive.mkdir()
    (drive / "Contents").mkdir()
    (drive / "PIONEER").mkdir()
    (drive / "Contents" / "song.mp3").write_bytes(b"audio")
    (drive / "PIONEER" / "export.pdb").write_bytes(b"pdb")

    lib = SourceLibrary(
        drive_root=drive,
        tracks={1: _source_track(1, "Song")},
        playlists=[
            SourcePlaylist(
                rb_id=1,
                parent_rb_id=0,
                name="Main",
                sort_order=0,
                is_folder=False,
                track_rb_ids=[1],
            )
        ],
        warnings=[],
    )
    state["rb_ids"] = [1]
    report = ConversionReport()

    from rb2engine.writer.build import build_library

    m_db = build_library(lib, drive_root=drive, report=report, target_schema=(3, 0, 1))

    assert m_db == drive / "Engine Library" / "Database2" / "m.db"
    assert m_db.is_file()
    assert not (m_db.parent / "m.db.tmp").exists()

    conn = sqlite3.connect(f"file:{m_db}?mode=ro", uri=True)
    try:
        info = conn.execute(
            "SELECT schemaVersionMajor, schemaVersionMinor, schemaVersionPatch "
            "FROM Information"
        ).fetchone()
        assert info == (3, 0, 1)
        assert conn.execute("SELECT COUNT(*) FROM Track").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM Playlist").fetchone()[0] == 1
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()

    assert report.counters.tracks_converted == 1
    assert report.counters.playlists_converted == 1
    assert report.fatal is False


def test_rerun_preserves_sibling_files_byte_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-run replaces only m.db; hm/sm/stm/Music/foreign survive unchanged."""
    _install_database_fakes(monkeypatch)
    state = _install_pipeline_fakes(monkeypatch)

    drive = tmp_path / "stick"
    drive.mkdir()
    m_db = _seed_existing_library(drive, uuid="carry-me-uuid")
    eng = drive / "Engine Library"
    db2 = eng / "Database2"

    prior_hashes = {
        "hm": _sha256(db2 / "hm.db"),
        "sm": _sha256(db2 / "sm.db"),
        "stm": _sha256(db2 / "stm.db"),
        "music": _sha256(eng / "Music" / "keep.me"),
        "foreign": _sha256(eng / "foreign-keep.bin"),
    }
    prior_m_db_bytes = m_db.read_bytes()

    lib = SourceLibrary(
        drive_root=drive,
        tracks={1: _source_track(1)},
        playlists=[],
        warnings=[],
    )
    state["rb_ids"] = [1]
    report = ConversionReport()

    from rb2engine.writer.build import build_library

    new_m_db = build_library(
        lib,
        drive_root=drive,
        report=report,
        target_schema=(3, 0, 1),
    )
    assert new_m_db == m_db
    # m.db was replaced (content changed — new library, not the prior track)
    assert m_db.read_bytes() != prior_m_db_bytes

    assert _sha256(db2 / "hm.db") == prior_hashes["hm"]
    assert _sha256(db2 / "sm.db") == prior_hashes["sm"]
    assert _sha256(db2 / "stm.db") == prior_hashes["stm"]
    assert _sha256(eng / "Music" / "keep.me") == prior_hashes["music"]
    assert _sha256(eng / "foreign-keep.bin") == prior_hashes["foreign"]

    # uuid carried forward so hm.db origin links stay valid
    conn = sqlite3.connect(f"file:{m_db}?mode=ro", uri=True)
    try:
        uuid = conn.execute("SELECT uuid FROM Information").fetchone()[0]
        assert uuid == "carry-me-uuid"
    finally:
        conn.close()


def test_mid_write_failure_leaves_original_mdb_and_no_tmp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fatal during write deletes m.db.tmp and never touches the prior m.db."""
    _install_database_fakes(monkeypatch)
    _install_pipeline_fakes(monkeypatch, fail_after_tmp=True)

    drive = tmp_path / "stick"
    drive.mkdir()
    m_db = _seed_existing_library(drive, uuid="must-survive")
    prior = m_db.read_bytes()
    prior_hash = _sha256(m_db)

    lib = SourceLibrary(
        drive_root=drive,
        tracks={1: _source_track(1)},
        playlists=[],
        warnings=[],
    )
    report = ConversionReport()

    from rb2engine.writer.build import build_library

    # Narrow on purpose: build_library wraps any mid-write failure into
    # FatalError (the exit-2 path). Accepting a bare Exception would let an
    # unrelated crash masquerade as "rollback worked".
    with pytest.raises(FatalError):
        build_library(lib, drive_root=drive, report=report, target_schema=(3, 0, 1))

    assert m_db.is_file()
    assert m_db.read_bytes() == prior
    assert _sha256(m_db) == prior_hash
    assert not (m_db.parent / "m.db.tmp").exists()
    assert report.fatal is True


def test_stray_tmp_is_deleted_never_adopted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crashed prior run's m.db.tmp must be wiped on the next reconcile."""
    _install_database_fakes(monkeypatch)
    _install_pipeline_fakes(monkeypatch)

    drive = tmp_path / "stick"
    drive.mkdir()
    m_db = _seed_existing_library(drive)
    tmp = m_db.parent / "m.db.tmp"
    tmp.write_bytes(b"CORRUPT-PARTIAL-DATABASE-FROM-CRASH")
    journal = m_db.parent / "m.db.tmp-journal"
    journal.write_bytes(b"stale-journal")

    lib = SourceLibrary(drive_root=drive, tracks={}, playlists=[], warnings=[])
    report = ConversionReport()

    from rb2engine.writer.build import build_library

    build_library(lib, drive_root=drive, report=report, target_schema=(3, 0, 1))

    assert not tmp.exists()
    assert not journal.exists()
    # New m.db is a real sqlite DB, not the corrupt tmp content
    assert m_db.read_bytes() != b"CORRUPT-PARTIAL-DATABASE-FROM-CRASH"
    conn = sqlite3.connect(f"file:{m_db}?mode=ro", uri=True)
    try:
        assert conn.execute("SELECT COUNT(*) FROM Information").fetchone()[0] == 1
    finally:
        conn.close()


def test_nothing_created_outside_engine_library(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Walk the whole drive: only Engine Library/ may gain new paths."""
    _install_database_fakes(monkeypatch)
    state = _install_pipeline_fakes(monkeypatch)

    drive = tmp_path / "stick"
    drive.mkdir()
    (drive / "Contents").mkdir()
    (drive / "Contents" / "A").mkdir()
    (drive / "Contents" / "A" / "b.mp3").write_bytes(b"track-audio")
    (drive / "PIONEER").mkdir()
    (drive / "PIONEER" / "export.pdb").write_bytes(b"export")
    (drive / "PIONEER" / "USBANLZ").mkdir()

    def _manifest() -> dict[str, bytes]:
        out: dict[str, bytes] = {}
        for root, dirs, files in os.walk(drive):
            # stable walk
            dirs.sort()
            for name in sorted(files):
                p = Path(root) / name
                rel = p.relative_to(drive).as_posix()
                if rel.startswith("Engine Library/") or rel == "Engine Library":
                    continue
                out[rel] = p.read_bytes()
        return out

    before = _manifest()

    lib = SourceLibrary(
        drive_root=drive,
        tracks={1: _source_track(1)},
        playlists=[],
        warnings=[],
    )
    state["rb_ids"] = [1]
    report = ConversionReport()

    from rb2engine.writer.build import build_library

    build_library(lib, drive_root=drive, report=report, target_schema=(3, 0, 1))

    after = _manifest()
    assert after == before, (
        f"files outside Engine Library/ changed: "
        f"added={set(after)-set(before)} removed={set(before)-set(after)} "
        f"changed={[k for k in before if k in after and before[k] != after[k]]}"
    )

    # And Engine Library does exist with Database2/m.db
    assert (drive / "Engine Library" / "Database2" / "m.db").is_file()

    # No writes landed beside Engine Library at the drive root
    root_entries = {p.name for p in drive.iterdir()}
    assert root_entries <= {"Contents", "PIONEER", "Engine Library"}


def test_build_playlist_tree_reconstructs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Full build_library path: playlist linked lists match the SourceLibrary."""
    _install_database_fakes(monkeypatch)
    state = _install_pipeline_fakes(monkeypatch)

    drive = tmp_path / "stick"
    drive.mkdir()

    lib = SourceLibrary(
        drive_root=drive,
        tracks={
            10: _source_track(10, "a"),
            20: _source_track(20, "b"),
            30: _source_track(30, "c"),
        },
        playlists=[
            SourcePlaylist(
                rb_id=1,
                parent_rb_id=0,
                name="Folder",
                sort_order=0,
                is_folder=True,
                track_rb_ids=[],
            ),
            SourcePlaylist(
                rb_id=2,
                parent_rb_id=1,
                name="Inner",
                sort_order=0,
                is_folder=False,
                track_rb_ids=[10, 20, 30],
            ),
            SourcePlaylist(
                rb_id=3,
                parent_rb_id=0,
                name="RootList",
                sort_order=1,
                is_folder=False,
                track_rb_ids=[30, 10],
            ),
        ],
        warnings=[],
    )
    state["rb_ids"] = [10, 20, 30]
    report = ConversionReport()

    from rb2engine.writer.build import build_library

    m_db = build_library(lib, drive_root=drive, report=report, target_schema=(3, 0, 1))

    conn = sqlite3.connect(f"file:{m_db}?mode=ro", uri=True)
    try:
        # Root order: Folder, RootList
        rows = conn.execute(
            "SELECT id, title, nextListId FROM Playlist WHERE parentListId = 0"
        ).fetchall()
        by_next = {n: (i, t) for i, t, n in rows}
        order: list[str] = []
        curr = 0
        while curr in by_next:
            i, t = by_next[curr]
            order.insert(0, t)
            curr = i
        assert order == ["Folder", "RootList"]

        folder_id = next(i for i, t, _ in rows if t == "Folder")
        inner_id = conn.execute(
            "SELECT id FROM Playlist WHERE title = 'Inner' AND parentListId = ?",
            (folder_id,),
        ).fetchone()[0]

        # Entity chain for Inner: tracks 10,20,30 → engine ids from insert order
        ents = conn.execute(
            "SELECT id, trackId, nextEntityId FROM PlaylistEntity WHERE listId = ?",
            (inner_id,),
        ).fetchall()
        by_next_e = {n: (i, tid) for i, tid, n in ents}
        track_order: list[int] = []
        curr = 0
        while curr in by_next_e:
            i, tid = by_next_e[curr]
            track_order.insert(0, tid)
            curr = i
        # Engine ids are whatever insert_tracks assigned; order of rb 10,20,30
        # maps to sequential lastrowids in our fake: check title order via Track
        titles = []
        for tid in track_order:
            titles.append(
                conn.execute(
                    "SELECT title FROM Track WHERE id = ?", (tid,)
                ).fetchone()[0]
            )
        # Our fake uses title "T" for all — check count and chain length instead
        assert len(track_order) == 3
        assert len(set(track_order)) == 3
    finally:
        conn.close()

    assert report.counters.playlists_converted == 3
    assert report.counters.tracks_converted == 3
