"""Two full conversions of the same source must yield byte-identical m.db.

WHY (criterion 8 / D5):
* Engine keys play history (hm.db) and OverviewData/<uuid>/ on Information.uuid
  — reusing the prior uuid is a hard requirement, not an optimisation.
* AlbumArt ids are AUTOINCREMENT; insertion order fixes every Track.albumArtId.
  Shuffling order would still "dedup" but break any canonical dump comparison.
* Wall-clock in any column makes re-runs flaky across second boundaries.

Mechanical proof: build twice, compare sqlite3-equivalent canonical dumps.
Also covers a real-world FAT32 artifact: macOS AppleDouble ``._m.db`` sidecars
must not clutter Engine Library (surgical removal of only our own sidecar).
"""

from __future__ import annotations

import os
import sqlite3
import sys
import time
from pathlib import Path

import pytest

from rb2engine.ir import SourceArtwork, SourceLibrary, SourcePlaylist, SourceTrack
from rb2engine.ir_engine import artwork_content_hash
from rb2engine.report import ConversionReport
from rb2engine.writer.build import build_library

# Minimal distinct 1×1 PNGs (hand-authored; not produced by our writer).
_PNG_A = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de"
    "0000000c49444154789c6360f8cfc0000000030001f9c4e2c50000000049454e44ae426082"
)
_PNG_B = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de"
    "0000000c49444154789c6368f8ffc0000000030001fbcdc28d0000000049454e44ae426082"
)


def _canonical_dump(db_path: Path) -> str:
    """sqlite3 .dump equivalent — blobs as X'…' hex, stable for identical DBs."""
    conn = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
    try:
        return "\n".join(conn.iterdump())
    finally:
        conn.close()


def _read_uuid(db_path: Path) -> str:
    conn = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
    try:
        return str(conn.execute("SELECT uuid FROM Information LIMIT 1").fetchone()[0])
    finally:
        conn.close()


def _source_track(
    rb_id: int,
    *,
    title: str,
    drive: Path,
    art: SourceArtwork | None = None,
) -> SourceTrack:
    audio = drive / "Contents" / f"{rb_id}.mp3"
    audio.parent.mkdir(parents=True, exist_ok=True)
    if not audio.is_file():
        audio.write_bytes(b"ID3fake-audio-" + str(rb_id).encode())
    return SourceTrack(
        rb_id=rb_id,
        title=title,
        artist="Artist",
        album="Album",
        genre="Genre",
        label="",
        comment="",
        composer="",
        remixer="",
        year=2020,
        track_number=1,
        disc_number=None,
        bpm=128.0,
        key_name="Am",
        rating=0,
        play_count=0,
        bitrate=320,
        file_size=audio.stat().st_size,
        file_type="mp3",
        sample_rate=44100,
        duration_s=60,
        total_samples=44100 * 60,
        raw_path=f"/Contents/{rb_id}.mp3",
        resolved_path=audio,
        beatgrid=None,
        cues=[],
        artwork=art,
    )


def _small_library(drive: Path) -> SourceLibrary:
    """Two tracks, two distinct covers, first-seen art order is load-bearing.

    Track rb_id order is deliberately not the same as content_key sort order so
    a writer that sorted artwork by hash would still fail the dump compare /
    albumArtId pin when insertion order is first-seen-by-track-id.
    """
    art_dir = drive / "art"
    art_dir.mkdir(parents=True, exist_ok=True)
    path_a = art_dir / "a.png"
    path_b = art_dir / "b.png"
    path_a.write_bytes(_PNG_A)
    path_b.write_bytes(_PNG_B)
    key_a = artwork_content_hash(_PNG_A)
    key_b = artwork_content_hash(_PNG_B)
    assert key_a != key_b

    # Insert into dict in reverse rb_id order; build must still process sorted ids.
    tracks = {
        2: _source_track(
            2,
            title="Second",
            drive=drive,
            art=SourceArtwork(content_key=key_b, path=path_b, source="pdb"),
        ),
        1: _source_track(
            1,
            title="First",
            drive=drive,
            art=SourceArtwork(content_key=key_a, path=path_a, source="pdb"),
        ),
    }
    return SourceLibrary(
        drive_root=drive,
        tracks=tracks,
        playlists=[
            SourcePlaylist(
                rb_id=10,
                parent_rb_id=0,
                name="Set",
                sort_order=0,
                is_folder=False,
                track_rb_ids=[1, 2],
            )
        ],
        warnings=[],
    )


def _prepare_drive(root: Path) -> Path:
    drive = root / "stick"
    drive.mkdir()
    (drive / "Contents").mkdir()
    (drive / "PIONEER").mkdir()
    (drive / "PIONEER" / "export.pdb").write_bytes(b"export")
    return drive


def test_two_full_conversions_produce_identical_canonical_dumps(
    tmp_path: Path,
) -> None:
    """Re-run without --database-uuid: dumps match; uuid carried; art ids stable.

    Sleep across a second boundary between runs so any leaked wall-clock value
    (strftime('%s') trigger, datetime.now, etc.) would change the dump.
    """
    drive = _prepare_drive(tmp_path)
    lib = _small_library(drive)

    report1 = ConversionReport()
    m_db = build_library(
        lib,
        drive_root=drive,
        report=report1,
        target_schema=(3, 0, 1),
        with_artwork=True,
    )
    assert m_db.is_file()
    dump1 = _canonical_dump(m_db)
    uuid1 = _read_uuid(m_db)

    # Pin albumArtId assignment from first-seen track order (rb_id 1 then 2).
    conn = sqlite3.connect(f"file:{m_db.resolve()}?mode=ro", uri=True)
    try:
        art_rows = conn.execute(
            "SELECT id, hash FROM AlbumArt ORDER BY id"
        ).fetchall()
        assert len(art_rows) == 2
        # Track 1's art is first-seen → AlbumArt id 1; track 2 → id 2.
        t_art = {
            row[0]: row[1]
            for row in conn.execute(
                "SELECT title, albumArtId FROM Track ORDER BY id"
            )
        }
        assert t_art["First"] == 1
        assert t_art["Second"] == 2
        # No wall-clock residue in time columns after finalize pin.
        times = conn.execute(
            "SELECT dateCreated, dateAdded, lastEditTime, timeLastPlayed "
            "FROM Track"
        ).fetchall()
        for date_created, date_added, last_edit, time_last in times:
            assert date_created is None
            assert date_added is None
            # Pinned epoch (not strftime('%s') from the PerformanceData trigger).
            assert last_edit in (0, "0", None) or last_edit == 0
            assert time_last is None
        played = conn.execute(
            "SELECT currentPlayedIndiciator FROM Information"
        ).fetchone()[0]
        assert played == 0
    finally:
        conn.close()

    time.sleep(1.1)  # force a different wall-clock second

    report2 = ConversionReport()
    m_db2 = build_library(
        lib,
        drive_root=drive,
        report=report2,
        target_schema=(3, 0, 1),
        with_artwork=True,
    )
    assert m_db2 == m_db
    dump2 = _canonical_dump(m_db)
    uuid2 = _read_uuid(m_db)

    assert uuid2 == uuid1, (
        "Information.uuid must be reused from the existing m.db so hm.db "
        "history and OverviewData/<uuid>/ stay linked (D5)"
    )
    assert dump1 == dump2, (
        "canonical sqlite dumps differ across two conversions of the same "
        "source — non-determinism in uuid, AlbumArt order, or a wall-clock column"
    )


def test_album_art_insertion_order_stable_across_runs(tmp_path: Path) -> None:
    """AlbumArt AUTOINCREMENT ids must not shuffle between equivalent builds.

    If insertion sorted by content_key, albumArtId values on Track would change
    whenever a new image sorts earlier — dumps would diverge for the same library.
    """
    drive = _prepare_drive(tmp_path)
    lib = _small_library(drive)
    key_by_title: dict[str, str] = {}
    for t in lib.tracks.values():
        assert t.artwork is not None
        key_by_title[t.title] = t.artwork.content_key

    ids_per_run: list[dict[str, int]] = []
    for _ in range(2):
        report = ConversionReport()
        m_db = build_library(
            lib,
            drive_root=drive,
            report=report,
            target_schema=(3, 0, 1),
            database_uuid="stable-art-order-uuid",
            with_artwork=True,
        )
        conn = sqlite3.connect(f"file:{m_db.resolve()}?mode=ro", uri=True)
        try:
            title_to_art = {
                title: int(art_id)
                for title, art_id in conn.execute(
                    "SELECT title, albumArtId FROM Track"
                )
            }
            hash_by_id = {
                int(i): h
                for i, h in conn.execute("SELECT id, hash FROM AlbumArt")
            }
        finally:
            conn.close()
        ids_per_run.append(title_to_art)
        assert hash_by_id[title_to_art["First"]] == key_by_title["First"]
        assert hash_by_id[title_to_art["Second"]] == key_by_title["Second"]
        # First-seen by sorted rb_id: track 1 then track 2 → ids 1, 2.
        assert title_to_art["First"] == 1
        assert title_to_art["Second"] == 2

    assert ids_per_run[0] == ids_per_run[1]


def test_appledouble_sidecar_for_mdb_removed_on_macos(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After swap, remove only ``._m.db`` we created — never a blanket ``._*`` sweep.

    Real FAT32 runs on macOS left an AppleDouble sidecar beside m.db from the
    copy step. That is clutter in Engine Library; deleting the user's other
    ``._*`` metadata would be destructive.
    """
    drive = _prepare_drive(tmp_path)
    lib = SourceLibrary(
        drive_root=drive,
        tracks={1: _source_track(1, title="One", drive=drive)},
        playlists=[],
        warnings=[],
    )

    real_replace = os.replace

    def replace_planting_appledouble(src: str | Path, dst: str | Path, *a, **k):
        real_replace(src, dst, *a, **k)
        # Plant the sidecar for the *published* name (what users saw on FAT32).
        dst_path = Path(dst)
        sidecar = dst_path.with_name(f"._{dst_path.name}")
        sidecar.write_bytes(b"\x00\x05\x16\x07" + b"fake-appledouble")

    monkeypatch.setattr(os, "replace", replace_planting_appledouble)
    monkeypatch.setattr(sys, "platform", "darwin")

    # Pre-existing user AppleDouble metadata that must survive.
    eng_lib = drive / "Engine Library"
    eng_lib.mkdir(parents=True)
    db2_pre = eng_lib / "Database2"
    db2_pre.mkdir(parents=True, exist_ok=True)
    user_sidecar = eng_lib / "._Music"
    user_sidecar.write_bytes(b"user-metadata-keep")
    foreign = eng_lib / "._foreign.keep"
    foreign.write_bytes(b"also-keep")
    # Another AppleDouble next to m.db that is NOT ._m.db — must survive.
    other_db_meta = db2_pre / "._hm.db"
    other_db_meta.write_bytes(b"sibling-meta")

    report = ConversionReport()
    m_db = build_library(
        lib,
        drive_root=drive,
        report=report,
        target_schema=(3, 0, 1),
        with_artwork=False,
    )

    db2 = m_db.parent
    assert m_db.is_file()
    assert not (db2 / "._m.db").exists(), "._m.db sidecar for our database must be removed"
    # Never a blanket sweep.
    assert user_sidecar.is_file()
    assert user_sidecar.read_bytes() == b"user-metadata-keep"
    assert foreign.is_file()
    assert other_db_meta.is_file()
    assert other_db_meta.read_bytes() == b"sibling-meta"


def test_appledouble_removal_is_noop_on_non_macos(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Guard: do not touch AppleDouble paths when not on darwin."""
    drive = _prepare_drive(tmp_path)
    lib = SourceLibrary(
        drive_root=drive,
        tracks={1: _source_track(1, title="One", drive=drive)},
        playlists=[],
        warnings=[],
    )

    real_replace = os.replace

    def replace_and_plant_sidecar(src: str | Path, dst: str | Path, *a, **k):
        real_replace(src, dst, *a, **k)
        Path(dst).with_name(f"._{Path(dst).name}").write_bytes(b"sidecar")

    monkeypatch.setattr(os, "replace", replace_and_plant_sidecar)
    monkeypatch.setattr(sys, "platform", "linux")

    report = ConversionReport()
    m_db = build_library(
        lib,
        drive_root=drive,
        report=report,
        target_schema=(3, 0, 1),
        with_artwork=False,
    )
    # On non-macOS the guard is a no-op — we neither create nor require removal.
    # The planted sidecar (simulating something else) must not be deleted by us.
    assert (m_db.parent / "._m.db").is_file()
