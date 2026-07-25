"""Fresh-stick schema selection.

WHY THIS EXISTS
---------------
A stick that already has an Engine library keeps its own schema triple — that
case is unambiguous and covered elsewhere. A *fresh* stick carries no signal at
all about which Engine the user runs, and picking wrong is not symmetric:

* Engine migrates an older schema UPWARD. Observed on real hardware: a 3.0.1
  stick was migrated in place to 3.0.2 by Engine DJ 4.3.0.
* There is no downgrade path. Handing an older Engine a newer schema can leave
  it unable to open the library at all.

So we look for evidence first (the user's own desktop Engine library) and fall
back to the conservative version only when there is none.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from rb2engine.writer import build as build_mod
from rb2engine.writer.build import _DEFAULT_SCHEMA, detect_desktop_schema


def _make_engine_db(path: Path, triple: tuple[int, int, int]) -> Path:
    """Minimal m.db carrying just the Information row detect_schema reads."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE Information (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "uuid TEXT, schemaVersionMajor INTEGER, schemaVersionMinor INTEGER, "
            "schemaVersionPatch INTEGER, currentPlayedIndiciator INTEGER, "
            "lastRekordBoxLibraryImportReadCounter INTEGER)"
        )
        conn.execute(
            "INSERT INTO Information (uuid, schemaVersionMajor, schemaVersionMinor,"
            " schemaVersionPatch, currentPlayedIndiciator,"
            " lastRekordBoxLibraryImportReadCounter) VALUES (?,?,?,?,?,?)",
            ("test-uuid", *triple, 0, 0),
        )
        conn.commit()
    finally:
        conn.close()
    return path


def test_adopts_supported_schema_from_desktop_library(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A readable desktop library at a supported version is the best evidence.

    Writing the schema the user's own Engine already runs avoids handing them a
    database that must be migrated before it is usable.
    """
    home = tmp_path / "home"
    _make_engine_db(home / "Music" / "Engine Library" / "Database2" / "m.db", (3, 0, 2))
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))

    assert detect_desktop_schema() == (3, 0, 2)


def test_no_desktop_library_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing to read -> no opinion, so the caller uses the safe fallback."""
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path / "empty"))
    assert detect_desktop_schema() is None


def test_unsupported_desktop_schema_falls_back_not_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A desktop library we have no DDL for must NOT abort a fresh conversion.

    We cannot write 9.9.9 — we have no captured schema for it. Writing a
    supported older version and letting Engine migrate upward is strictly
    better than refusing to convert a stick that is otherwise fine.
    """
    home = tmp_path / "home"
    _make_engine_db(home / "Music" / "Engine Library" / "Database2" / "m.db", (9, 9, 9))
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))

    assert detect_desktop_schema() is None


def test_corrupt_desktop_library_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Detection is diagnostics, never a failure mode for the conversion."""
    home = tmp_path / "home"
    db = home / "Music" / "Engine Library" / "Database2" / "m.db"
    db.parent.mkdir(parents=True)
    db.write_bytes(b"this is not a database")
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))

    assert detect_desktop_schema() is None


def test_fallback_is_the_conservative_version() -> None:
    """The fallback must be the version Engine can migrate UP from.

    Pinned deliberately: raising this to the newest supported schema would
    silently change what every fresh stick receives, and an older Engine has no
    way to downgrade.
    """
    from rb2engine.writer.schema import SUPPORTED_SCHEMAS

    assert _DEFAULT_SCHEMA == (3, 0, 1)
    assert _DEFAULT_SCHEMA in SUPPORTED_SCHEMAS
    assert min(SUPPORTED_SCHEMAS) == _DEFAULT_SCHEMA


def test_existing_drive_library_wins_over_desktop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A drive that already has a library keeps ITS triple, not the desktop's.

    Otherwise re-converting a 3.0.2 stick on a 3.0.1 machine would silently
    rewrite it at the older version. Asserted through the real build path:
    desktop detection is made to explode, so any consultation fails the test.
    """
    from rb2engine.ir import SourceLibrary, SourceTrack
    from rb2engine.report import ConversionReport
    from rb2engine.writer.build import build_library

    def _explode() -> tuple[int, int, int] | None:
        raise AssertionError("desktop detection must not run when the drive has a library")

    monkeypatch.setattr(build_mod, "detect_desktop_schema", _explode)

    drive = tmp_path / "stick"
    (drive / "Contents").mkdir(parents=True)
    audio = drive / "Contents" / "t.mp3"
    audio.write_bytes(b"audio")

    track = SourceTrack(
        rb_id=1, title="T", artist="A", album="Al", genre="G", label="", comment="",
        composer="", remixer="", year=2024, track_number=1, disc_number=None,
        bpm=128.0, key_name="Am", rating=0, play_count=0, bitrate=320, file_size=5,
        file_type="mp3", sample_rate=44100, duration_s=180, total_samples=7938000,
        raw_path="/Contents/t.mp3", resolved_path=audio,
        beatgrid=None, cues=[], artwork=None,
    )
    lib = SourceLibrary(drive_root=drive, tracks={1: track}, playlists=[], warnings=[])

    # First run pins the drive at 3.0.2.
    build_library(lib, drive_root=drive, report=ConversionReport(), target_schema=(3, 0, 2))
    # Second run must carry that forward without ever asking the desktop.
    m_db = build_library(lib, drive_root=drive, report=ConversionReport())

    conn = sqlite3.connect(f"file:{m_db}?mode=ro", uri=True)
    try:
        triple = conn.execute(
            "SELECT schemaVersionMajor, schemaVersionMinor, schemaVersionPatch "
            "FROM Information"
        ).fetchone()
    finally:
        conn.close()
    assert tuple(triple) == (3, 0, 2)
