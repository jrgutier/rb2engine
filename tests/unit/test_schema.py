"""Tests for writer/schema.py — DDL replay, G3 version gate, Information row.

Expected object counts and the Track column set come from the Engine-authored
golden fixture (engine_desktop_3_0_1.db), never from our own dump of the DDL
we just wrote. That is the only way these tests fail when the wrong schema
is bundled or a trigger is silently dropped.
"""

from __future__ import annotations

import re
import sqlite3
import uuid
from pathlib import Path

import pytest

from rb2engine.errors import UnsupportedFormatError
from rb2engine.writer import schema as schema_mod

GOLDEN_DB = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "golden"
    / "engine_desktop_3_0_1.db"
)


def _object_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Full sqlite_master counts including sqlite_sequence and autoindexes.

    Engine's empty-after-DDL shape is 10 tables / 24 indexes / 16 triggers /
    4 views (54 objects). Autoindexes from UNIQUE constraints and the
    sqlite_sequence table for AUTOINCREMENT are part of that total.
    """
    rows = conn.execute(
        "SELECT type, COUNT(*) FROM sqlite_master GROUP BY type"
    ).fetchall()
    return {typ: n for typ, n in rows}


def _golden_counts() -> dict[str, int]:
    conn = sqlite3.connect(f"file:{GOLDEN_DB}?mode=ro", uri=True)
    try:
        return _object_counts(conn)
    finally:
        conn.close()


def test_captured_ddl_object_counts_match_engine_golden(tmp_path: Path) -> None:
    """Replay must produce the same object inventory Engine authored.

    A missing CREATE TRIGGER or CREATE INDEX would change these counts and
    leave playlists / PerformanceData silently broken in ways Engine may not
    report — so we compare type counts to the golden Engine database itself.
    """
    db_path = tmp_path / "m.db"
    conn = schema_mod.create_database(db_path, (3, 0, 1))
    try:
        got = _object_counts(conn)
    finally:
        conn.close()

    expected = _golden_counts()
    assert got == expected
    # Pin the documented Engine 3.0.1 inventory so a wrong golden still fails.
    assert got.get("table") == 10
    assert got.get("index") == 24
    assert got.get("trigger") == 16
    assert got.get("view") == 4
    assert sum(got.values()) == 54


def test_track_table_is_schema_3_0_1_not_3_0_2(tmp_path: Path) -> None:
    """Track column set is why DDL is version-keyed, not a single hardcoded SQL.

    Schema 3.0.1 (Engine DJ 4.3.x) ends at lastEditTime and does NOT have
    albumArtSourceHash; that column exists only in 3.0.2 (Engine 4.5+/5.x).
    Bundling the wrong DDL would make Engine reject or silently misread m.db.
    Column list is taken from the Engine-authored golden, not from our DDL file.
    """
    golden = sqlite3.connect(f"file:{GOLDEN_DB}?mode=ro", uri=True)
    try:
        golden_cols = [row[1] for row in golden.execute("PRAGMA table_info(Track)")]
    finally:
        golden.close()

    assert golden_cols[-1] == "lastEditTime"
    assert "albumArtSourceHash" not in golden_cols
    # Actual Engine 3.0.1 Track width (research text said "41"; fixture has 42
    # including id — pin the golden truth).
    assert len(golden_cols) == 42

    db_path = tmp_path / "m.db"
    conn = schema_mod.create_database(db_path, (3, 0, 1))
    try:
        cols = [row[1] for row in conn.execute("PRAGMA table_info(Track)")]
    finally:
        conn.close()

    assert cols == golden_cols
    assert cols[-1] == "lastEditTime"
    assert "albumArtSourceHash" not in cols


def test_resolve_schema_supported_triple() -> None:
    """G3 allowlist: 3.0.1 is the seeded target for Engine DJ 4.3.0."""
    path = schema_mod.resolve_schema((3, 0, 1))
    assert path.is_file()
    assert path.name == schema_mod.SUPPORTED_SCHEMAS[(3, 0, 1)]
    assert path.parent == schema_mod.DDL_DIR


def test_resolve_schema_unsupported_raises() -> None:
    """G3: unknown schema must fail loud (exit 2 path), never write a guess.

    3.0.2 is a real Engine schema we deliberately do not support yet; 9.9.9
    is nonsense. Both must raise UnsupportedFormatError with supported
    versions and a pointer at the capture runbook.
    """
    for triple in ((3, 0, 2), (9, 9, 9)):
        with pytest.raises(UnsupportedFormatError) as exc_info:
            schema_mod.resolve_schema(triple)
        msg = str(exc_info.value)
        assert "3.0.1" in msg or "3, 0, 1" in msg or "(3, 0, 1)" in msg
        assert "README" in msg


def test_track_insert_auto_creates_performance_data(tmp_path: Path) -> None:
    """Captured AFTER INSERT trigger must fire — the highest-value DDL check.

    Engine relies on trigger_after_insert_Track_insert_performance_data to
    create the PerformanceData row. Reimplementing that in Python is forbidden;
    a silently missing trigger set would leave tracks without performance
    rows and corrupt playlists in ways Engine may not surface.
    """
    db_path = tmp_path / "m.db"
    conn = schema_mod.create_database(db_path, (3, 0, 1))
    try:
        before = conn.execute("SELECT COUNT(*) FROM PerformanceData").fetchone()[0]
        assert before == 0

        conn.execute(
            "INSERT INTO Track (path, filename, title) VALUES (?, ?, ?)",
            ("/tmp/rb2engine_trigger_probe.mp3", "probe.mp3", "probe"),
        )
        rows = conn.execute(
            "SELECT trackId FROM PerformanceData ORDER BY trackId"
        ).fetchall()
        track_ids = conn.execute("SELECT id FROM Track").fetchall()
        assert len(track_ids) == 1
        assert rows == [(track_ids[0][0],)]
    finally:
        conn.close()


def test_journal_mode_is_delete_not_wal(tmp_path: Path) -> None:
    """FAT32 removable volumes need rollback journal; WAL needs shared memory.

    Engine itself writes *.db-journal beside its databases on FAT32. WAL is
    not reliable on those volumes, so create_database must force DELETE.
    """
    db_path = tmp_path / "m.db"
    conn = schema_mod.create_database(db_path, (3, 0, 1))
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()
    assert mode.lower() == "delete"


def test_information_uuid_carried_when_supplied(tmp_path: Path) -> None:
    """hm.db play history keys cross-DB identity on Information.uuid.

    Regenerating uuid on every rebuild orphans Historylist origin links.
    A caller that already has a uuid (prior m.db or --database-uuid) must
    see it written unchanged.
    """
    carried = "157447f8-69b4-4dc7-af8a-20d973484461"
    db_path = tmp_path / "m.db"
    conn = schema_mod.create_database(db_path, (3, 0, 1), database_uuid=carried)
    try:
        row = conn.execute(
            "SELECT uuid, schemaVersionMajor, schemaVersionMinor, schemaVersionPatch, "
            "currentPlayedIndiciator, lastRekordBoxLibraryImportReadCounter "
            "FROM Information"
        ).fetchone()
    finally:
        conn.close()

    assert row is not None
    assert row[0] == carried
    assert row[1:4] == (3, 0, 1)
    # Typo column must exist and accept a signed integer (Engine uses opaque int64).
    assert isinstance(row[4], int)


def test_information_uuid_generated_when_omitted(tmp_path: Path) -> None:
    """First run only: mint a random uuid when none is supplied to carry forward."""
    db_path = tmp_path / "m.db"
    conn = schema_mod.create_database(db_path, (3, 0, 1))
    try:
        value = conn.execute("SELECT uuid FROM Information").fetchone()[0]
    finally:
        conn.close()

    assert isinstance(value, str)
    # RFC 4122 uuid string form
    uuid.UUID(value)
    assert re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        value,
        flags=re.IGNORECASE,
    )
