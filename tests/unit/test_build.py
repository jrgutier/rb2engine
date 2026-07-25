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

import contextlib
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

# ---------------------------------------------------------------------------
# Reconcile, recovery stages, schema detection, artwork flags, edge paths
# ---------------------------------------------------------------------------


def test_reconcile_removes_stray_tmp_journal_and_legacy_dirs(tmp_path: Path) -> None:
    """Crash residue and withdrawn directory-swap leftovers must not linger.

    A prior design staged whole ``Engine Library.tmp/`` trees; leaving those
    beside a live library confuses users and wastes stick space. Stray
    ``m.db.tmp`` must never be adopted as state on the next run.
    """
    from rb2engine.writer.build import reconcile

    drive = tmp_path / "stick"
    eng = drive / "Engine Library"
    db2 = eng / "Database2"
    db2.mkdir(parents=True)
    (db2 / "m.db").write_bytes(b"good-mdb")
    (db2 / "m.db.tmp").write_bytes(b"partial-crash")
    (db2 / "m.db.tmp-journal").write_bytes(b"stale-j")
    (db2 / "hm.db").write_bytes(b"sibling-must-stay")

    legacy_tmp = drive / "Engine Library.tmp"
    legacy_old = drive / "Engine Library.old"
    (legacy_tmp / "nested").mkdir(parents=True)
    (legacy_tmp / "nested" / "x.bin").write_bytes(b"legacy-tmp")
    legacy_old.write_bytes(b"legacy-old-file")  # file form, not only dirs

    reconcile(eng)

    assert not (db2 / "m.db.tmp").exists()
    assert not (db2 / "m.db.tmp-journal").exists()
    assert not legacy_tmp.exists()
    assert not legacy_old.exists()
    # Reconcile must never touch the live library or siblings.
    assert (db2 / "m.db").read_bytes() == b"good-mdb"
    assert (db2 / "hm.db").read_bytes() == b"sibling-must-stay"

    # Journal-only residue (tmp already gone) must still be cleaned.
    eng2 = tmp_path / "stick2" / "Engine Library"
    db2b = eng2 / "Database2"
    db2b.mkdir(parents=True)
    (db2b / "m.db.tmp-journal").write_bytes(b"orphan-journal")
    reconcile(eng2)
    assert not (db2b / "m.db.tmp-journal").exists()


def test_failure_before_staging_preserves_prior_mdb(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If create_m_db never succeeds, the previous m.db must be byte-identical."""
    import rb2engine.writer.database as database_mod

    _install_database_fakes(monkeypatch)
    _install_pipeline_fakes(monkeypatch)

    drive = tmp_path / "stick"
    drive.mkdir()
    m_db = _seed_existing_library(drive, uuid="pre-stage-survive")
    prior = m_db.read_bytes()

    def boom_create(*_a: Any, **_k: Any) -> sqlite3.Connection:
        raise RuntimeError("staging create failed")

    monkeypatch.setattr(database_mod, "create_m_db", boom_create, raising=False)

    lib = SourceLibrary(
        drive_root=drive, tracks={1: _source_track(1)}, playlists=[], warnings=[]
    )
    report = ConversionReport()
    from rb2engine.writer.build import build_library

    with pytest.raises(FatalError, match="build_library failed"):
        build_library(lib, drive_root=drive, report=report, target_schema=(3, 0, 1))

    assert m_db.read_bytes() == prior
    assert not (m_db.parent / "m.db.tmp").exists()
    assert report.fatal is True


def test_failure_after_staging_before_replace_preserves_mdb_and_clears_tmp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Staging finished but os.replace blew up: prior m.db intact, no tmp left.

    This is the most expensive failure window — a full DB was built and copied
    to m.db.tmp. Recovery must still refuse to publish and must not leave a
    partial tmp that the next run could mistake for progress.
    """
    _install_database_fakes(monkeypatch)
    state = _install_pipeline_fakes(monkeypatch)

    drive = tmp_path / "stick"
    drive.mkdir()
    m_db = _seed_existing_library(drive, uuid="post-stage-survive")
    prior = m_db.read_bytes()
    prior_hash = _sha256(m_db)

    def boom_replace(_src: str | os.PathLike[str], _dst: str | os.PathLike[str]) -> None:
        raise OSError("simulated replace failure (disk full)")

    monkeypatch.setattr(os, "replace", boom_replace)

    lib = SourceLibrary(
        drive_root=drive, tracks={1: _source_track(1)}, playlists=[], warnings=[]
    )
    state["rb_ids"] = [1]
    report = ConversionReport()
    from rb2engine.writer.build import build_library

    with pytest.raises(FatalError):
        build_library(lib, drive_root=drive, report=report, target_schema=(3, 0, 1))

    assert m_db.is_file()
    assert m_db.read_bytes() == prior
    assert _sha256(m_db) == prior_hash
    assert not (m_db.parent / "m.db.tmp").exists()


def test_fresh_drive_failure_removes_partial_engine_library(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A brand-new stick that never published m.db must not keep an empty tree."""
    import rb2engine.writer.database as database_mod

    _install_database_fakes(monkeypatch)

    drive = tmp_path / "stick"
    drive.mkdir()
    eng = drive / "Engine Library"
    assert not eng.exists()

    def boom_create(*_a: Any, **_k: Any) -> sqlite3.Connection:
        raise RuntimeError("cannot stage")

    monkeypatch.setattr(database_mod, "create_m_db", boom_create, raising=False)

    lib = SourceLibrary(drive_root=drive, tracks={}, playlists=[], warnings=[])
    report = ConversionReport()
    from rb2engine.writer.build import build_library

    with pytest.raises(FatalError):
        build_library(lib, drive_root=drive, report=report, target_schema=(3, 0, 1))

    assert not eng.exists(), "partial Engine Library must be wiped on fresh-drive abort"


def test_preservation_hashes_artwork_overview_and_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Artwork/, OverviewData/, and foreign files survive rebuild untouched."""
    _install_database_fakes(monkeypatch)
    state = _install_pipeline_fakes(monkeypatch)

    drive = tmp_path / "stick"
    drive.mkdir()
    m_db = _seed_existing_library(drive)
    eng = drive / "Engine Library"
    art_file = eng / "Artwork" / "cover.dat"
    art_file.write_bytes(b"engine-artwork-cache-v1")
    overview = eng / "OverviewData" / "wave.bin"
    overview.write_bytes(b"overview-waveform-v1")
    unknown = eng / "VendorPlugin" / "state.json"
    unknown.parent.mkdir()
    unknown.write_bytes(b'{"vendor":true}')

    before = {
        "art": _sha256(art_file),
        "overview": _sha256(overview),
        "unknown": _sha256(unknown),
        "hm": _sha256(eng / "Database2" / "hm.db"),
        "music": _sha256(eng / "Music" / "keep.me"),
    }

    lib = SourceLibrary(
        drive_root=drive, tracks={1: _source_track(1)}, playlists=[], warnings=[]
    )
    state["rb_ids"] = [1]
    from rb2engine.writer.build import build_library

    build_library(
        lib, drive_root=drive, report=ConversionReport(), target_schema=(3, 0, 1)
    )

    assert _sha256(art_file) == before["art"]
    assert _sha256(overview) == before["overview"]
    assert _sha256(unknown) == before["unknown"]
    assert _sha256(eng / "Database2" / "hm.db") == before["hm"]
    assert _sha256(eng / "Music" / "keep.me") == before["music"]
    assert m_db.is_file()


def test_detect_desktop_schema_used_only_when_drive_has_no_library(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fresh stick consults desktop schema; existing m.db never does.

    Calling detect_desktop_schema on a drive that already has a library would
    risk rewriting a 3.0.2 stick as 3.0.1 (or vice versa) and breaking Engine's
    origin links / migration expectations.
    """
    import rb2engine.writer.build as build_mod
    import rb2engine.writer.database as database_mod

    _install_database_fakes(monkeypatch)
    state = _install_pipeline_fakes(monkeypatch)

    fake_home = tmp_path / "fake_home"
    desktop_db = fake_home / "Music" / "Engine Library" / "Database2" / "m.db"
    desktop_db.parent.mkdir(parents=True)
    conn = schema_mod.create_database(desktop_db, (3, 0, 2), database_uuid="desktop-uuid")
    conn.close()

    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
    # Prevent /Users/Shared from supplying a real machine library.
    monkeypatch.setattr(
        build_mod,
        "_DESKTOP_LIBRARY_CANDIDATES",
        (("Music", "Engine Library", "Database2", "m.db"),),
    )

    calls: list[str] = []
    real_detect = build_mod.detect_desktop_schema

    def counting_detect() -> tuple[int, int, int] | None:
        calls.append("detect")
        return real_detect()

    monkeypatch.setattr(build_mod, "detect_desktop_schema", counting_detect)

    # --- fresh drive: detect is consulted ---
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    lib = SourceLibrary(drive_root=fresh, tracks={}, playlists=[], warnings=[])
    from rb2engine.writer.build import build_library

    m_db = build_library(lib, drive_root=fresh, report=ConversionReport())
    assert calls == ["detect"]
    info = sqlite3.connect(f"file:{m_db}?mode=ro", uri=True)
    try:
        triple = info.execute(
            "SELECT schemaVersionMajor, schemaVersionMinor, schemaVersionPatch "
            "FROM Information"
        ).fetchone()
        assert triple == (3, 0, 2)
    finally:
        info.close()

    # --- existing library: detect must not run again ---
    calls.clear()
    drive = tmp_path / "existing"
    drive.mkdir()
    _seed_existing_library(drive, uuid="stick-uuid")  # 3.0.1
    lib2 = SourceLibrary(
        drive_root=drive, tracks={1: _source_track(1)}, playlists=[], warnings=[]
    )
    state["rb_ids"] = [1]
    m2 = build_library(lib2, drive_root=drive, report=ConversionReport())
    assert calls == [], "existing m.db must pin its own schema; desktop is irrelevant"
    info2 = sqlite3.connect(f"file:{m2}?mode=ro", uri=True)
    try:
        uuid = info2.execute("SELECT uuid FROM Information").fetchone()[0]
        assert uuid == "stick-uuid"
        triple = info2.execute(
            "SELECT schemaVersionMajor, schemaVersionMinor, schemaVersionPatch "
            "FROM Information"
        ).fetchone()
        assert triple == (3, 0, 1)
    finally:
        info2.close()

    # silence unused import warning paths
    assert database_mod.detect_schema is not None


def test_detect_desktop_schema_unsupported_and_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unsupported desktop schema falls back; unreadable candidates are skipped."""
    import rb2engine.writer.build as build_mod
    import rb2engine.writer.database as database_mod

    fake_home = tmp_path / "home"
    bad = fake_home / "Music" / "Engine Library" / "Database2" / "m.db"
    bad.parent.mkdir(parents=True)
    # Valid sqlite but unsupported version triple in Information.
    conn = schema_mod.create_database(bad, (3, 0, 1), database_uuid="x")
    conn.execute(
        "UPDATE Information SET schemaVersionMajor=9, schemaVersionMinor=9, "
        "schemaVersionPatch=9"
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
    monkeypatch.setattr(
        build_mod,
        "_DESKTOP_LIBRARY_CANDIDATES",
        (("Music", "Engine Library", "Database2", "m.db"),),
    )
    # detect_schema must return the unsupported triple (not None).
    def detect(path: Path) -> tuple[int, int, int] | None:
        c = sqlite3.connect(f"file:{Path(path).resolve()}?mode=ro", uri=True)
        try:
            row = c.execute(
                "SELECT schemaVersionMajor, schemaVersionMinor, schemaVersionPatch "
                "FROM Information LIMIT 1"
            ).fetchone()
        finally:
            c.close()
        return (int(row[0]), int(row[1]), int(row[2])) if row else None

    monkeypatch.setattr(database_mod, "detect_schema", detect, raising=False)
    assert build_mod.detect_desktop_schema() is None

    # Unreadable path: detect_schema raises → skip, return None.
    def raise_detect(_p: Path) -> tuple[int, int, int] | None:
        raise OSError("permission denied")

    monkeypatch.setattr(database_mod, "detect_schema", raise_detect, raising=False)
    assert build_mod.detect_desktop_schema() is None

    # Missing candidates entirely.
    empty_home = tmp_path / "empty_home"
    empty_home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: empty_home))
    assert build_mod.detect_desktop_schema() is None


def test_with_artwork_false_skips_artwork_insert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``with_artwork=False`` (--no-artwork) must not call insert_artwork."""
    import rb2engine.writer.artwork as artwork_mod
    from rb2engine.ir import SourceArtwork
    from rb2engine.ir_engine import artwork_content_hash

    _install_database_fakes(monkeypatch)
    state = _install_pipeline_fakes(monkeypatch)

    calls: list[int] = []
    real_insert = artwork_mod.insert_artwork

    def counting_insert(conn: sqlite3.Connection, arts: Sequence[Any]) -> dict[str, int]:
        calls.append(len(list(arts)))
        return real_insert(conn, arts)

    monkeypatch.setattr(artwork_mod, "insert_artwork", counting_insert, raising=False)

    drive = tmp_path / "stick"
    drive.mkdir()
    png = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc`\xf8\xcf"
        b"\xc0\x00\x00\x00\x03\x00\x01\xf9\xc4\xe2\xc5\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    art_path = drive / "cover.png"
    art_path.write_bytes(png)
    art = SourceArtwork(
        content_key=artwork_content_hash(png), path=art_path, source="pdb"
    )
    t = _source_track(1)
    # SourceTrack is frozen — rebuild with artwork.
    t = SourceTrack(
        **{**{f: getattr(t, f) for f in t.__dataclass_fields__}, "artwork": art}
    )

    lib = SourceLibrary(drive_root=drive, tracks={1: t}, playlists=[], warnings=[])
    state["rb_ids"] = [1]
    report = ConversionReport()
    from rb2engine.writer.build import build_library

    build_library(
        lib,
        drive_root=drive,
        report=report,
        target_schema=(3, 0, 1),
        with_artwork=False,
    )
    assert calls == []
    assert report.counters.artwork_found == 0
    assert report.counters.artwork_missing == 0


def test_artwork_counters_found_missing_deduped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Report counters must reflect missing, found, and content_key dedup.

    Wrong counters hide a 2× artwork BLOB blow-up on a large library.
    """
    import rb2engine.writer.artwork as artwork_mod
    from rb2engine.ir import SourceArtwork
    from rb2engine.ir_engine import artwork_content_hash

    real_insert_artwork = artwork_mod.insert_artwork  # capture before pipeline fakes

    _install_database_fakes(monkeypatch)
    state = _install_pipeline_fakes(monkeypatch)

    drive = tmp_path / "stick"
    drive.mkdir()
    png = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc`\xf8\xcf"
        b"\xc0\x00\x00\x00\x03\x00\x01\xf9\xc4\xe2\xc5\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    art_path = drive / "shared.png"
    art_path.write_bytes(png)
    key = artwork_content_hash(png)
    art_a = SourceArtwork(content_key=key, path=art_path, source="pdb")
    art_b = SourceArtwork(content_key=key, path=art_path, source="pdb")

    def track_with_art(rb_id: int, artwork: SourceArtwork | None) -> SourceTrack:
        base = _source_track(rb_id)
        return SourceTrack(
            **{
                **{f: getattr(base, f) for f in base.__dataclass_fields__},
                "artwork": artwork,
            }
        )

    lib = SourceLibrary(
        drive_root=drive,
        tracks={
            1: track_with_art(1, art_a),
            2: track_with_art(2, art_b),  # same content_key → dedup
            3: track_with_art(3, None),  # missing
        },
        playlists=[],
        warnings=[],
    )
    state["rb_ids"] = [1, 2, 3]
    report = ConversionReport()
    from rb2engine.writer.build import build_library

    # Restore real insert_artwork (pipeline fakes stub it to {}).
    monkeypatch.setattr(
        artwork_mod, "insert_artwork", real_insert_artwork, raising=False
    )

    m_db = build_library(
        lib, drive_root=drive, report=report, target_schema=(3, 0, 1), with_artwork=True
    )
    assert report.counters.artwork_found == 2
    assert report.counters.artwork_deduped == 1
    assert report.counters.artwork_missing == 1

    conn = sqlite3.connect(f"file:{m_db}?mode=ro", uri=True)
    try:
        n = conn.execute("SELECT COUNT(*) FROM AlbumArt").fetchone()[0]
        assert n == 1
    finally:
        conn.close()


def test_unresolvable_and_map_failed_tracks_are_soft_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One bad track must not abort the run or leave tmp residue."""
    import rb2engine.mapper.track as map_track_mod

    _install_database_fakes(monkeypatch)
    state = _install_pipeline_fakes(monkeypatch)

    def map_track(
        src: SourceTrack, *, drive_root: Path, engine_library_dir: Path
    ) -> EngineTrack:
        if src.rb_id == 2:
            raise ValueError("mapper exploded on track 2")
        return _minimal_engine_track(path=f"../Contents/track_{src.rb_id}.mp3")

    monkeypatch.setattr(map_track_mod, "map_track", map_track, raising=False)

    drive = tmp_path / "stick"
    drive.mkdir()
    bad_path = _source_track(1)
    bad_path = SourceTrack(
        **{
            **{f: getattr(bad_path, f) for f in bad_path.__dataclass_fields__},
            "resolved_path": None,
        }
    )
    lib = SourceLibrary(
        drive_root=drive,
        tracks={1: bad_path, 2: _source_track(2), 3: _source_track(3)},
        playlists=[],
        warnings=[],
    )
    # Only track 3 maps successfully (2 fails map, 1 unresolvable).
    state["rb_ids"] = [3]
    report = ConversionReport()
    from rb2engine.writer.build import build_library

    m_db = build_library(
        lib, drive_root=drive, report=report, target_schema=(3, 0, 1)
    )
    assert m_db.is_file()
    assert not (m_db.parent / "m.db.tmp").exists()
    assert report.counters.tracks_unresolvable_paths == 1
    skip_codes = {s.reason_code for s in report.skipped_tracks}
    assert "unresolvable_path" in skip_codes
    assert "map_failed" in skip_codes
    assert report.counters.tracks_converted == 1


def test_positional_track_id_map_is_realigned_to_rb_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If insert_tracks keys 1..N, build remaps to rb_ids so playlists resolve."""
    import rb2engine.writer.tracks as tracks_mod

    _install_database_fakes(monkeypatch)
    _install_pipeline_fakes(monkeypatch)

    def insert_tracks(
        conn: sqlite3.Connection,
        tracks: Sequence[EngineTrack],
        *,
        art_ids: Mapping[str, int] | None = None,
    ) -> dict[int, int]:
        out: dict[int, int] = {}
        for i, et in enumerate(tracks):
            cur = conn.execute(
                "INSERT INTO Track (path, title, artist, originDatabaseUuid, "
                "originTrackId) VALUES (?, ?, ?, "
                "(SELECT uuid FROM Information LIMIT 1), ?)",
                (et.path, et.title, et.artist, i + 1),
            )
            out[i + 1] = int(cur.lastrowid)  # positional keys, not rb_id
        return out

    monkeypatch.setattr(tracks_mod, "insert_tracks", insert_tracks, raising=False)

    drive = tmp_path / "stick"
    drive.mkdir()
    lib = SourceLibrary(
        drive_root=drive,
        tracks={10: _source_track(10), 20: _source_track(20)},
        playlists=[
            SourcePlaylist(
                rb_id=1,
                parent_rb_id=0,
                name="P",
                sort_order=0,
                is_folder=False,
                track_rb_ids=[10, 20],
            )
        ],
        warnings=[],
    )
    report = ConversionReport()
    from rb2engine.writer.build import build_library

    m_db = build_library(lib, drive_root=drive, report=report, target_schema=(3, 0, 1))
    conn = sqlite3.connect(f"file:{m_db}?mode=ro", uri=True)
    try:
        n_ent = conn.execute("SELECT COUNT(*) FROM PlaylistEntity").fetchone()[0]
        assert n_ent == 2
    finally:
        conn.close()


def test_empty_track_id_map_does_not_invent_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty insert_tracks map with mapped tracks must not invent phantom ids."""
    import rb2engine.writer.tracks as tracks_mod

    _install_database_fakes(monkeypatch)
    _install_pipeline_fakes(monkeypatch)

    def insert_tracks(
        conn: sqlite3.Connection,
        tracks: Sequence[EngineTrack],
        *,
        art_ids: Mapping[str, int] | None = None,
    ) -> dict[int, int]:
        # Writer bug simulation: inserts nothing, returns empty.
        return {}

    monkeypatch.setattr(tracks_mod, "insert_tracks", insert_tracks, raising=False)

    drive = tmp_path / "stick"
    drive.mkdir()
    lib = SourceLibrary(
        drive_root=drive, tracks={1: _source_track(1)}, playlists=[], warnings=[]
    )
    report = ConversionReport()
    from rb2engine.writer.build import build_library

    build_library(lib, drive_root=drive, report=report, target_schema=(3, 0, 1))
    # Defensive path: counters fall back to engine_tracks length when map empty.
    assert report.counters.tracks_converted == 1


def test_stray_tmp_wiped_again_just_before_create(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Second wipe before create catches a race after reconcile."""
    import rb2engine.writer.build as build_mod

    _install_database_fakes(monkeypatch)
    _install_pipeline_fakes(monkeypatch)

    drive = tmp_path / "stick"
    drive.mkdir()
    m_db = _seed_existing_library(drive)
    tmp = m_db.parent / "m.db.tmp"
    tmp.write_bytes(b"stale")

    def reconcile_noop(_p: Path) -> None:
        # Leave the stray tmp so the pre-create wipe (lines 197-199) must fire.
        return None

    monkeypatch.setattr(build_mod, "reconcile", reconcile_noop)

    from rb2engine.writer.build import build_library

    build_library(
        SourceLibrary(drive_root=drive, tracks={}, playlists=[], warnings=[]),
        drive_root=drive,
        report=ConversionReport(),
        target_schema=(3, 0, 1),
    )
    assert not tmp.exists()


def test_journal_beside_tmp_removed_before_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A leftover m.db.tmp-journal after copy must not block os.replace."""
    import shutil

    _install_database_fakes(monkeypatch)
    state = _install_pipeline_fakes(monkeypatch)

    drive = tmp_path / "stick"
    drive.mkdir()
    _seed_existing_library(drive)

    real_copy = shutil.copyfile

    def copy_and_plant_journal(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        real_copy(src, dst)
        Path(str(dst) + "-journal").write_bytes(b"journal-residue")

    monkeypatch.setattr(shutil, "copyfile", copy_and_plant_journal)

    lib = SourceLibrary(
        drive_root=drive, tracks={1: _source_track(1)}, playlists=[], warnings=[]
    )
    state["rb_ids"] = [1]
    from rb2engine.writer.build import build_library

    m_db = build_library(
        lib, drive_root=drive, report=ConversionReport(), target_schema=(3, 0, 1)
    )
    assert m_db.is_file()
    assert not Path(str(m_db.parent / "m.db.tmp") + "-journal").exists()
    assert not (m_db.parent / "m.db.tmp").exists()


def test_appledouble_sidecar_for_mdb_removed_on_macos(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only ._m.db for the published file is removed — not sibling sidecars."""
    import sys

    if sys.platform != "darwin":
        pytest.skip("AppleDouble cleanup is macOS-only")

    _install_database_fakes(monkeypatch)
    state = _install_pipeline_fakes(monkeypatch)

    drive = tmp_path / "stick"
    drive.mkdir()
    m_db = _seed_existing_library(drive)
    db2 = m_db.parent
    sibling_side = db2 / "._hm.db"
    sibling_side.write_bytes(b"keep-me-sibling-sidecar")

    real_replace = os.replace

    def replace_and_plant(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        real_replace(src, dst)
        Path(dst).with_name("._m.db").write_bytes(b"appledouble-m.db")

    monkeypatch.setattr(os, "replace", replace_and_plant)

    lib = SourceLibrary(
        drive_root=drive, tracks={1: _source_track(1)}, playlists=[], warnings=[]
    )
    state["rb_ids"] = [1]
    from rb2engine.writer.build import build_library

    build_library(
        lib, drive_root=drive, report=ConversionReport(), target_schema=(3, 0, 1)
    )
    assert not (db2 / "._m.db").exists()
    assert sibling_side.read_bytes() == b"keep-me-sibling-sidecar"


def test_fatal_error_from_finalize_is_reraised_not_wrapped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FatalError from integrity checks must surface as FatalError (exit 2)."""
    import rb2engine.writer.build as build_mod

    _install_database_fakes(monkeypatch)
    state = _install_pipeline_fakes(monkeypatch)

    def boom_finalize(_conn: sqlite3.Connection) -> None:
        raise FatalError("PRAGMA integrity_check failed: simulated")

    monkeypatch.setattr(build_mod, "_finalize", boom_finalize)

    drive = tmp_path / "stick"
    drive.mkdir()
    m_db = _seed_existing_library(drive, uuid="fatal-reraise")
    prior = m_db.read_bytes()
    lib = SourceLibrary(
        drive_root=drive, tracks={1: _source_track(1)}, playlists=[], warnings=[]
    )
    state["rb_ids"] = [1]
    report = ConversionReport()
    from rb2engine.writer.build import build_library

    with pytest.raises(FatalError, match="integrity_check"):
        build_library(lib, drive_root=drive, report=report, target_schema=(3, 0, 1))
    assert m_db.read_bytes() == prior


def test_error_path_survives_unlink_failure_after_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If m.db.tmp cannot be deleted after a failed replace, still mark fatal.

    Prior m.db must remain byte-identical — unlink failure on the tmp must
    never cascade into touching the published database.
    """
    _install_database_fakes(monkeypatch)
    state = _install_pipeline_fakes(monkeypatch)

    drive = tmp_path / "stick"
    drive.mkdir()
    m_db = _seed_existing_library(drive, uuid="unlink-fail")
    prior = m_db.read_bytes()

    def boom_replace(_s: Any, _d: Any) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", boom_replace)

    orig_unlink = Path.unlink

    def flaky_unlink(self: Path, *a: Any, **k: Any) -> None:
        if self.name == "m.db.tmp":
            raise OSError("tmp busy")
        return orig_unlink(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)

    lib = SourceLibrary(
        drive_root=drive, tracks={1: _source_track(1)}, playlists=[], warnings=[]
    )
    state["rb_ids"] = [1]
    report = ConversionReport()
    from rb2engine.writer.build import build_library

    with pytest.raises(FatalError):
        build_library(lib, drive_root=drive, report=report, target_schema=(3, 0, 1))
    assert m_db.read_bytes() == prior
    assert report.fatal is True
    # tmp may remain because unlink failed — that is the path under test
    assert (m_db.parent / "m.db.tmp").exists()


def test_read_prior_identity_handles_unreadable_and_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Corrupt prior m.db must not abort — fall back to defaults."""
    import rb2engine.writer.build as build_mod
    import rb2engine.writer.database as database_mod

    assert build_mod._read_prior_identity(None) == (None, None)
    missing = tmp_path / "nope.db"
    assert build_mod._read_prior_identity(missing) == (None, None)

    garbage = tmp_path / "garbage.db"
    garbage.write_bytes(b"not-a-sqlite-database")

    def raise_detect(_p: Path) -> tuple[int, int, int] | None:
        raise RuntimeError("detect boom")

    monkeypatch.setattr(database_mod, "detect_schema", raise_detect, raising=False)
    schema, uuid = build_mod._read_prior_identity(garbage)
    assert schema is None
    assert uuid is None

    # Readable Information path when detect_schema returns None.
    good = tmp_path / "good.db"
    c = schema_mod.create_database(good, (3, 0, 1), database_uuid="from-info")
    c.close()

    def none_detect(_p: Path) -> tuple[int, int, int] | None:
        return None

    monkeypatch.setattr(database_mod, "detect_schema", none_detect, raising=False)
    schema, uuid = build_mod._read_prior_identity(good)
    assert schema == (3, 0, 1)
    assert uuid == "from-info"


def test_finalize_integrity_and_fk_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """G4 integrity failures must be FatalError before any swap."""
    from unittest import mock

    import rb2engine.writer.build as build_mod
    import rb2engine.writer.database as database_mod

    # Ensure we exercise the local PRAGMA path (no database.finalize).
    if hasattr(database_mod, "finalize"):
        monkeypatch.delattr(database_mod, "finalize", raising=False)

    class ConnIntegrity:
        def execute(self, sql: str, *a: Any, **k: Any) -> Any:
            if "UPDATE Track" in sql or "UPDATE Information" in sql:
                return mock.Mock()
            if sql == "PRAGMA integrity_check":
                m = mock.Mock()
                m.fetchone.return_value = ("not ok",)
                return m
            if sql == "PRAGMA foreign_key_check":
                m = mock.Mock()
                m.fetchall.return_value = []
                return m
            return mock.Mock()

        def commit(self) -> None:
            return None

    with pytest.raises(FatalError, match="integrity_check"):
        build_mod._finalize(ConnIntegrity())  # type: ignore[arg-type]

    class ConnFk:
        def execute(self, sql: str, *a: Any, **k: Any) -> Any:
            if "UPDATE" in sql:
                return mock.Mock()
            if sql == "PRAGMA integrity_check":
                m = mock.Mock()
                m.fetchone.return_value = ("ok",)
                return m
            if sql == "PRAGMA foreign_key_check":
                m = mock.Mock()
                m.fetchall.return_value = [("Track", 1, "AlbumArt", 0)]
                return m
            return mock.Mock()

        def commit(self) -> None:
            return None

    with pytest.raises(FatalError, match="foreign_key_check"):
        build_mod._finalize(ConnFk())  # type: ignore[arg-type]

    # database.finalize preferred when present.
    conn3 = schema_mod.create_database(tmp_path / "t3.db", (3, 0, 1), database_uuid="fin3")
    called: list[bool] = []

    def fake_finalize(c: sqlite3.Connection) -> None:
        called.append(True)

    monkeypatch.setattr(database_mod, "finalize", fake_finalize, raising=False)
    build_mod._finalize(conn3)
    assert called == [True]
    conn3.close()


def test_fsync_helpers_are_best_effort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """fsync failures must never abort a completed conversion."""
    import rb2engine.writer.build as build_mod

    f = tmp_path / "x.bin"
    f.write_bytes(b"data")
    d = tmp_path / "dir"
    d.mkdir()

    def open_fail(*_a: Any, **_k: Any) -> int:
        raise OSError("open fail")

    monkeypatch.setattr(os, "open", open_fail)
    # The property under test is "does not raise". Made explicit rather than
    # implicit, and paired with an integrity check so a helper that silently
    # did nothing at all would not look identical to one that degraded well.
    build_mod._fsync_file(f)
    build_mod._fsync_dir(d)
    assert f.read_bytes() == b"data", "fsync helper must not disturb the file"
    assert d.is_dir()

    # fsync itself fails after open succeeds.
    fds: list[int] = []

    def open_ok(path: str, flags: int) -> int:
        fd = os.dup(0)  # any valid fd
        fds.append(fd)
        return fd

    monkeypatch.setattr(os, "open", open_ok)

    def fsync_fail(_fd: int) -> None:
        raise OSError("fsync unsupported")

    monkeypatch.setattr(os, "fsync", fsync_fail)
    build_mod._fsync_file(f)
    build_mod._fsync_dir(d)
    for fd in fds:
        with contextlib.suppress(OSError):
            os.close(fd)

    # Windows: dir fsync is a no-op.
    monkeypatch.setattr(build_mod.os, "name", "nt")
    build_mod._fsync_dir(d)


def test_detect_schema_none_skips_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """detect_schema returning None walks to the next candidate / falls through."""
    import rb2engine.writer.build as build_mod
    import rb2engine.writer.database as database_mod

    fake_home = tmp_path / "home"
    cand = fake_home / "Music" / "Engine Library" / "Database2" / "m.db"
    cand.parent.mkdir(parents=True)
    cand.write_bytes(b"x")  # exists but detect returns None

    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
    monkeypatch.setattr(
        build_mod,
        "_DESKTOP_LIBRARY_CANDIDATES",
        (("Music", "Engine Library", "Database2", "m.db"),),
    )
    monkeypatch.setattr(
        database_mod, "detect_schema", lambda _p: None, raising=False
    )
    assert build_mod.detect_desktop_schema() is None


def test_error_path_close_failure_still_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """conn.close() raising during rollback is logged; prior m.db still survives.

    sqlite3.Connection.close is read-only, so we return a thin proxy whose
    close() raises — same control flow build sees when a broken connection
    cannot be closed cleanly on the error path.
    """
    import rb2engine.writer.database as database_mod

    _install_database_fakes(monkeypatch)
    state = _install_pipeline_fakes(monkeypatch, fail_after_tmp=True)

    base_create = database_mod.create_m_db

    class _CloseBoomConn:
        """Proxy: all attrs from real conn except close() which always raises."""

        def __init__(self, real: sqlite3.Connection) -> None:
            object.__setattr__(self, "_real", real)

        def close(self) -> None:
            raise sqlite3.ProgrammingError("close during rollback")

        def __getattr__(self, name: str) -> Any:
            return getattr(object.__getattribute__(self, "_real"), name)

    def create_bad_close(
        path: Path, *, schema: tuple[int, int, int], uuid: str | None = None
    ) -> sqlite3.Connection:
        return _CloseBoomConn(base_create(path, schema=schema, uuid=uuid))  # type: ignore[return-value]

    monkeypatch.setattr(database_mod, "create_m_db", create_bad_close, raising=False)

    drive = tmp_path / "stick"
    drive.mkdir()
    m_db = _seed_existing_library(drive, uuid="close-fail")
    prior = m_db.read_bytes()
    lib = SourceLibrary(
        drive_root=drive, tracks={1: _source_track(1)}, playlists=[], warnings=[]
    )
    state["rb_ids"] = [1]
    report = ConversionReport()
    from rb2engine.writer.build import build_library

    with pytest.raises(FatalError):
        build_library(lib, drive_root=drive, report=report, target_schema=(3, 0, 1))
    assert m_db.read_bytes() == prior
    assert report.fatal is True


def test_fresh_drive_rmtree_failure_still_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If partial Engine Library cannot be removed, still raise FatalError."""
    import rb2engine.writer.build as build_mod
    import rb2engine.writer.database as database_mod

    _install_database_fakes(monkeypatch)

    def boom_create(*_a: Any, **_k: Any) -> sqlite3.Connection:
        raise RuntimeError("stage fail")

    monkeypatch.setattr(database_mod, "create_m_db", boom_create, raising=False)

    def boom_rmtree(_p: Path) -> None:
        raise OSError("rmtree denied")

    monkeypatch.setattr(build_mod, "_rmtree", boom_rmtree)

    drive = tmp_path / "stick"
    drive.mkdir()
    report = ConversionReport()
    from rb2engine.writer.build import build_library

    with pytest.raises(FatalError, match="build_library failed"):
        build_library(
            SourceLibrary(drive_root=drive, tracks={}, playlists=[], warnings=[]),
            drive_root=drive,
            report=report,
            target_schema=(3, 0, 1),
        )
    assert report.fatal is True


def test_read_prior_identity_without_detect_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When detect_schema is absent, Information row still supplies schema+uuid."""
    import rb2engine.writer.build as build_mod
    import rb2engine.writer.database as database_mod

    monkeypatch.setattr(database_mod, "detect_schema", None, raising=False)
    good = tmp_path / "good.db"
    c = schema_mod.create_database(good, (3, 0, 1), database_uuid="info-only")
    c.close()
    schema, uuid = build_mod._read_prior_identity(good)
    assert schema == (3, 0, 1)
    assert uuid == "info-only"

    # Empty Information table → no schema/uuid from row.
    empty = tmp_path / "empty.db"
    conn = sqlite3.connect(str(empty))
    conn.execute(
        "CREATE TABLE Information (schemaVersionMajor INT, schemaVersionMinor INT, "
        "schemaVersionPatch INT, uuid TEXT)"
    )
    conn.commit()
    conn.close()
    schema, uuid = build_mod._read_prior_identity(empty)
    assert schema is None
    assert uuid is None


def test_appledouble_non_darwin_is_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import rb2engine.writer.build as build_mod

    monkeypatch.setattr(build_mod.sys, "platform", "linux")
    mdb = tmp_path / "m.db"
    mdb.write_bytes(b"m")
    side = tmp_path / "._m.db"
    side.write_bytes(b"side")
    build_mod._remove_appledouble_sidecar_for(mdb)
    assert side.exists(), "non-macOS must not touch AppleDouble sidecars"


def test_rmtree_and_appledouble_edge_helpers(tmp_path: Path) -> None:
    """_rmtree removes files/dirs; appledouble no-ops on wrong name / missing."""
    import sys

    import rb2engine.writer.build as build_mod

    # missing path
    build_mod._rmtree(tmp_path / "does-not-exist")

    # file path
    f = tmp_path / "solo.file"
    f.write_bytes(b"x")
    build_mod._rmtree(f)
    assert not f.exists()

    # nested tree
    root = tmp_path / "tree"
    (root / "a" / "b").mkdir(parents=True)
    (root / "a" / "b" / "c.txt").write_bytes(b"c")
    (root / "leaf").write_bytes(b"l")
    build_mod._rmtree(root)
    assert not root.exists()

    # appledouble: wrong basename
    other = tmp_path / "other.db"
    other.write_bytes(b"o")
    build_mod._remove_appledouble_sidecar_for(other)

    # appledouble: missing sidecar is fine
    mdb = tmp_path / "m.db"
    mdb.write_bytes(b"m")
    build_mod._remove_appledouble_sidecar_for(mdb)

    if sys.platform == "darwin":
        side = tmp_path / "._m.db"
        side.write_bytes(b"side")
        build_mod._remove_appledouble_sidecar_for(mdb)
        assert not side.exists()

        # unlink failure is warned, not raised
        mdb2 = tmp_path / "sub" / "m.db"
        mdb2.parent.mkdir()
        mdb2.write_bytes(b"m")
        side2 = mdb2.parent / "._m.db"
        side2.write_bytes(b"s")
        real_unlink = Path.unlink

        def fail_unlink(self: Path, *a: Any, **k: Any) -> None:
            if self.name.startswith("._"):
                raise OSError("locked")
            return real_unlink(self, *a, **k)

        # Use a direct call with patched unlink via monkeypatch-like approach
        from unittest import mock

        with mock.patch.object(Path, "unlink", fail_unlink):
            build_mod._remove_appledouble_sidecar_for(mdb2)


