"""Tests for writer/database.py — detect_schema and create_m_db.

Why these cases exist:
- Engine DJ 4.3.0 runs schema 3.0.1 on the desktop library and 3.0.2 on the
  stick. The app version does not determine the schema; only reading the
  target m.db does. The two committed goldens are the empirical proof.
- uuid reuse is a hard requirement: hm.db play history and on-disk
  OverviewData/<uuid>/ directories key on Information.uuid. Minting a fresh
  uuid each run orphans both.
- create_m_db must replay captured DDL (including triggers). A Track insert
  that does not auto-create PerformanceData means triggers were not applied,
  and the rest of the writer would then wrongly INSERT PerformanceData.
"""

from __future__ import annotations

import re
import sqlite3
import uuid
from pathlib import Path

import pytest

from rb2engine.errors import UnsupportedFormatError
from rb2engine.writer.database import create_m_db, detect_schema

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "golden"
DESKTOP_301 = FIXTURES / "engine_desktop_3_0_1.db"
STICK_302 = FIXTURES / "engine_stick_3_0_2.db"


# ---------------------------------------------------------------------------
# detect_schema — read the target, never invent from app version
# ---------------------------------------------------------------------------


def test_detect_schema_desktop_golden_is_3_0_1() -> None:
    """Desktop Engine library from 4.3.0 is schema 3.0.1.

    If this ever reports 3.0.2 or None, either the golden was replaced or
    detect_schema stopped reading Information correctly — both break the
    version-keyed DDL design that exists because one Engine build has two
    schemas.
    """
    assert detect_schema(DESKTOP_301) == (3, 0, 1)


def test_detect_schema_stick_golden_is_3_0_2() -> None:
    """Stick m.db migrated in place by the same Engine 4.3.0 is schema 3.0.2.

    Paired with the desktop golden: proof that schema must be detected from
    the target database, not derived from the Engine app version string.
    """
    assert detect_schema(STICK_302) == (3, 0, 2)


def test_detect_schema_missing_file_returns_none(tmp_path: Path) -> None:
    """Absent m.db is a first-run signal, not a crash — caller mints uuid."""
    assert detect_schema(tmp_path / "no_such_m.db") is None


def test_detect_schema_unreadable_or_empty_returns_none(tmp_path: Path) -> None:
    """Corrupt / empty files must not raise into the convert path.

    Returning None lets build treat the target as first-run rather than
    aborting with an opaque sqlite error mid-pipeline.
    """
    empty = tmp_path / "empty.db"
    empty.write_bytes(b"")
    assert detect_schema(empty) is None

    not_sqlite = tmp_path / "junk.db"
    not_sqlite.write_bytes(b"not a sqlite database at all")
    assert detect_schema(not_sqlite) is None


def test_detect_schema_db_without_information_returns_none(tmp_path: Path) -> None:
    """A sqlite file that is not an Engine library has no Information row."""
    path = tmp_path / "other.db"
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE foo (id INTEGER)")
        conn.commit()
    finally:
        conn.close()
    assert detect_schema(path) is None


# ---------------------------------------------------------------------------
# create_m_db — DDL replay, Information, journal_mode, uuid policy
# ---------------------------------------------------------------------------


def test_create_m_db_refuses_unsupported_schema(tmp_path: Path) -> None:
    """G3 still applies: unknown triples must not produce a guess DB.

    detect_schema may report any triple found on disk; create_m_db must still
    refuse versions not in SUPPORTED_SCHEMAS so we never write DDL we cannot
    stand behind.
    """
    with pytest.raises(UnsupportedFormatError):
        create_m_db(tmp_path / "m.db", schema=(9, 9, 9))


def test_create_m_db_301_track_insert_auto_creates_performance_data(
    tmp_path: Path,
) -> None:
    """Triggers must replay: Track INSERT creates PerformanceData.

    writer/tracks.py UPDATEs PerformanceData and must never INSERT it. If
    this auto-create fails, every track write path is wrong by construction.
    """
    db_path = tmp_path / "m.db"
    conn = create_m_db(db_path, schema=(3, 0, 1))
    try:
        assert conn.execute("SELECT COUNT(*) FROM PerformanceData").fetchone()[0] == 0
        conn.execute(
            "INSERT INTO Track (path, filename, title) VALUES (?, ?, ?)",
            ("../Contents/probe.mp3", "probe.mp3", "probe"),
        )
        track_id = conn.execute("SELECT id FROM Track").fetchone()[0]
        rows = conn.execute(
            "SELECT trackId FROM PerformanceData WHERE trackId = ?",
            (track_id,),
        ).fetchall()
        assert rows == [(track_id,)]
    finally:
        conn.close()


def test_create_m_db_302_track_insert_auto_creates_performance_data(
    tmp_path: Path,
) -> None:
    """3.0.2 DDL must also fire the PerformanceData trigger.

    Stick targets use 3.0.2; shipping only 3.0.1 trigger coverage would leave
    the real deployment path untested.
    """
    db_path = tmp_path / "m.db"
    conn = create_m_db(db_path, schema=(3, 0, 2))
    try:
        info = conn.execute(
            "SELECT schemaVersionMajor, schemaVersionMinor, schemaVersionPatch "
            "FROM Information"
        ).fetchone()
        assert info == (3, 0, 2)

        cols = [r[1] for r in conn.execute("PRAGMA table_info(Track)")]
        assert "albumArtSourceHash" in cols

        conn.execute(
            "INSERT INTO Track (path, filename, title) VALUES (?, ?, ?)",
            ("../Contents/probe.mp3", "probe.mp3", "probe"),
        )
        track_id = conn.execute("SELECT id FROM Track").fetchone()[0]
        assert conn.execute(
            "SELECT trackId FROM PerformanceData WHERE trackId = ?",
            (track_id,),
        ).fetchone() == (track_id,)
    finally:
        conn.close()


def test_create_m_db_information_schema_triple_matches(tmp_path: Path) -> None:
    """Engine rejects a DB whose Information triple does not match reality.

    The triple written must be exactly the schema argument used to select DDL.
    """
    for triple in ((3, 0, 1), (3, 0, 2)):
        db_path = tmp_path / f"m_{triple[0]}_{triple[1]}_{triple[2]}.db"
        conn = create_m_db(db_path, schema=triple)
        try:
            row = conn.execute(
                "SELECT schemaVersionMajor, schemaVersionMinor, schemaVersionPatch "
                "FROM Information"
            ).fetchone()
        finally:
            conn.close()
        assert row == triple


def test_create_m_db_journal_mode_is_delete(tmp_path: Path) -> None:
    """FAT32 sticks need DELETE journal mode; WAL is not reliable on them."""
    conn = create_m_db(tmp_path / "m.db", schema=(3, 0, 1))
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()
    assert str(mode).lower() == "delete"


def test_create_m_db_uuid_carried_when_supplied(tmp_path: Path) -> None:
    """Reuse uuid when given — hard requirement, not an optimisation.

    hm.db Historylist keys originDatabaseUuid, and OverviewData/<uuid>/
    directories on the stick are named after this value. A fresh uuid each
    run orphans history and strands a new directory every conversion.
    """
    carried = "157447f8-69b4-4dc7-af8a-20d973484461"
    conn = create_m_db(tmp_path / "m.db", schema=(3, 0, 1), uuid=carried)
    try:
        row = conn.execute(
            "SELECT uuid, currentPlayedIndiciator FROM Information"
        ).fetchone()
    finally:
        conn.close()

    assert row[0] == carried
    # Typo column name is real; value is a signed int64 (Engine uses negatives).
    assert isinstance(row[1], int)
    assert -(2**63) <= row[1] <= (2**63) - 1


def test_create_m_db_uuid_minted_when_omitted(tmp_path: Path) -> None:
    """First run only: mint a random uuid when none is supplied to carry."""
    conn = create_m_db(tmp_path / "m.db", schema=(3, 0, 1))
    try:
        value = conn.execute("SELECT uuid FROM Information").fetchone()[0]
    finally:
        conn.close()

    assert isinstance(value, str)
    uuid.UUID(value)
    assert re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        value,
        flags=re.IGNORECASE,
    )


def test_create_m_db_current_played_indicator_is_signed_int64(tmp_path: Path) -> None:
    """currentPlayedIndiciator is an opaque signed int64 (Engine typo spelling).

    Observed Engine values are large negatives; the column must accept the full
    signed int64 range, not a small positive counter.
    """
    conn = create_m_db(tmp_path / "m.db", schema=(3, 0, 1))
    try:
        # Column must exist under Engine's misspelled name.
        cols = [r[1] for r in conn.execute("PRAGMA table_info(Information)")]
        assert "currentPlayedIndiciator" in cols
        assert "currentPlayedIndicator" not in cols

        value = conn.execute(
            "SELECT currentPlayedIndiciator FROM Information"
        ).fetchone()[0]
    finally:
        conn.close()

    assert isinstance(value, int)
    assert -(2**63) <= value <= (2**63) - 1
