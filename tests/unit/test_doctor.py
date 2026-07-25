"""Tests for rb2engine doctor — pre-convert environment / schema diagnostics.

Why these cases exist: Engine DJ 4.3.0 already ran two schema versions on one
machine (desktop 3.0.1, stick 3.0.2). doctor is the user-facing early warning
for that drift. Unsupported triples must never be a bare failure — they must
point at capturing a new DDL. doctor must be strictly read-only so running it
over a live library cannot create journals or side files.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import rb2engine
from rb2engine.writer.schema import SUPPORTED_SCHEMAS

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "golden"
DESKTOP_301 = FIXTURES / "engine_desktop_3_0_1.db"
STICK_302 = FIXTURES / "engine_stick_3_0_2.db"

# Golden Information.uuid values (Engine-authored; used as identity checks).
DESKTOP_UUID = "157447f8-69b4-4dc7-af8a-20d973484461"
STICK_UUID = "102b661f-976b-4811-825c-6762d3a118bf"


def _tree_snapshot(root: Path) -> set[tuple[str, int]]:
    """Relative path + size for every file under root (detect any write)."""
    out: set[tuple[str, int]] = set()
    for p in sorted(root.rglob("*")):
        if p.is_file():
            out.add((str(p.relative_to(root)), p.stat().st_size))
    return out


def _make_minimal_stick(
    root: Path,
    *,
    export_ext: bool = True,
    engine_library: bool = False,
    m_db: Path | None = None,
    anlz_files: int = 2,
) -> Path:
    """Synthetic rekordbox stick layout for drive_root doctor reports."""
    root.mkdir(parents=True, exist_ok=True)
    pioneer = root / "PIONEER"
    rb = pioneer / "rekordbox"
    rb.mkdir(parents=True)
    (rb / "export.pdb").write_bytes(b"fake-pdb")
    if export_ext:
        (rb / "exportExt.pdb").write_bytes(b"fake-ext")
    usbanlz = pioneer / "USBANLZ"
    for i in range(anlz_files):
        d = usbanlz / f"P{i:03d}" / f"{i:08X}"
        d.mkdir(parents=True)
        (d / "ANLZ0000.DAT").write_bytes(b"dat")
        (d / "ANLZ0000.EXT").write_bytes(b"ext")
        if i == 0:
            (d / "ANLZ0000.2EX").write_bytes(b"2ex")
    contents = root / "Contents"
    contents.mkdir()
    (contents / "a.mp3").write_bytes(b"audio")
    if engine_library:
        eng = root / "Engine Library" / "Database2"
        eng.mkdir(parents=True)
        if m_db is not None:
            import shutil

            shutil.copy2(m_db, eng / "m.db")
        else:
            (eng / "m.db").write_bytes(b"not-sqlite")
    return root


def _make_unsupported_mdb(
    path: Path,
    *,
    triple: tuple[int, int, int] = (9, 9, 9),
    db_uuid: str = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
) -> Path:
    """Minimal Engine-shaped Information row with an unsupported triple.

    Must not use create_m_db — that correctly refuses unsupported versions.
    """
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            """
            CREATE TABLE Information (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                uuid TEXT,
                schemaVersionMajor INTEGER,
                schemaVersionMinor INTEGER,
                schemaVersionPatch INTEGER,
                currentPlayedIndiciator INTEGER,
                lastRekordBoxLibraryImportReadCounter INTEGER
            )
            """
        )
        maj, minor, patch = triple
        conn.execute(
            """
            INSERT INTO Information (
                uuid, schemaVersionMajor, schemaVersionMinor, schemaVersionPatch,
                currentPlayedIndiciator, lastRekordBoxLibraryImportReadCounter
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (db_uuid, maj, minor, patch, 0, None),
        )
        conn.commit()
    finally:
        conn.close()
    return path


# ---------------------------------------------------------------------------
# Environment: versions + bundled DDL (no paths)
# ---------------------------------------------------------------------------


def test_doctor_report_includes_rb2engine_and_python_versions() -> None:
    """Users need to know which tool/runtime produced the diagnosis.

    Support tickets and schema-drift reports are useless without versions.
    """
    from rb2engine.doctor import doctor_report

    result = doctor_report()
    text = result.render_text()

    assert rb2engine.__version__ in text
    assert f"{sys.version_info.major}.{sys.version_info.minor}" in text
    assert result.ok is True


def test_doctor_report_lists_bundled_schemas_from_supported_schemas() -> None:
    """Bundled DDL list must come from SUPPORTED_SCHEMAS, not a hardcoded copy.

    Hardcoding would drift the moment a new schema is registered — the whole
    point of doctor is to report what the code actually ships.
    """
    from rb2engine.doctor import doctor_report

    result = doctor_report()
    text = result.render_text()

    for maj, minor, patch in SUPPORTED_SCHEMAS:
        assert f"{maj}.{minor}.{patch}" in text
    # Guard: test fails if SUPPORTED_SCHEMAS is emptied without noticing.
    assert SUPPORTED_SCHEMAS
    assert len(SUPPORTED_SCHEMAS) >= 2


def test_doctor_report_includes_runtime_dependency_versions() -> None:
    """construct/pyrekordbox/click/rich versions matter when parsing breaks.

    pdb/ANLZ bugs are often dependency-version specific; doctor surfaces them
    without requiring the user to run pip freeze.
    """
    from rb2engine.doctor import doctor_report

    text = doctor_report().render_text().lower()
    for name in ("construct", "pyrekordbox", "click", "rich"):
        assert name in text


# ---------------------------------------------------------------------------
# engine_db: golden supported triples + unsupported with fix path
# ---------------------------------------------------------------------------


def test_doctor_engine_desktop_3_0_1_supported() -> None:
    """User desktop library is schema 3.0.1 and must be reported supported.

    This is the primary convert target for Engine DJ 4.3.0 desktop libraries.
    """
    from rb2engine.doctor import doctor_report

    result = doctor_report(engine_db=DESKTOP_301)
    text = result.render_text()

    assert result.ok is True
    assert "3.0.1" in text
    assert DESKTOP_UUID in text
    assert "supported" in text.lower()
    # Must not claim unsupported for a bundled golden.
    assert "unsupported" not in text.lower()


def test_doctor_engine_stick_3_0_2_supported() -> None:
    """Stick m.db after Engine's in-place migration is 3.0.2 and supported.

    Same app build, different triple than desktop — doctor must accept both
    goldens so users are not told their stick is broken when it is not.
    """
    from rb2engine.doctor import doctor_report

    result = doctor_report(engine_db=STICK_302)
    text = result.render_text()

    assert result.ok is True
    assert "3.0.2" in text
    assert STICK_UUID in text
    assert "supported" in text.lower()
    assert "unsupported" not in text.lower()


def test_doctor_unsupported_schema_is_actionable_and_not_ok(tmp_path: Path) -> None:
    """Unsupported triple must fail loudly with a capture-new-DDL fix path.

    A bare 'unsupported' without README guidance leaves users stuck; the fix
    is always to capture Engine's own m.db per writer/ddl/README.md.
    """
    from rb2engine.doctor import doctor_report

    bad = _make_unsupported_mdb(tmp_path / "future.db", triple=(9, 9, 9))
    result = doctor_report(engine_db=bad)
    text = result.render_text()

    assert result.ok is False
    assert "9.9.9" in text
    assert "unsupported" in text.lower()
    assert "README.md" in text or "ddl/README" in text
    # Point at the capture runbook, not a vague "update rb2engine".
    assert "capture" in text.lower() or "ddl" in text.lower()


def test_doctor_is_strictly_read_only(tmp_path: Path) -> None:
    """doctor must never create journals, sidecars, or any new files.

    Users will run this over live libraries and sticks; a single write would
    violate the read-only source principle and risk Engine locking issues.
    """
    from rb2engine.doctor import doctor_report

    stick = _make_minimal_stick(
        tmp_path / "stick",
        engine_library=True,
        m_db=DESKTOP_301,
    )
    # Copy goldens into a workspace we fully own so we can snapshot everything.
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    import shutil

    eng_db = workspace / "engine.db"
    shutil.copy2(DESKTOP_301, eng_db)
    stick_copy = workspace / "stick"
    shutil.copytree(stick, stick_copy)

    before = _tree_snapshot(workspace)
    result = doctor_report(engine_db=eng_db, drive_root=stick_copy)
    after = _tree_snapshot(workspace)

    assert before == after
    # Sanity: the run itself still produced a useful report.
    assert "3.0.1" in result.render_text()


# ---------------------------------------------------------------------------
# drive_root: scan findings
# ---------------------------------------------------------------------------


def test_doctor_drive_root_reports_layout_and_full_rebuild(tmp_path: Path) -> None:
    """drive_root path must surface export.pdb, G1d, USBANLZ, Contents, rebuild.

    Convert decisions (full-rebuild vs first-run, MyTags present) depend on
    these facts; doctor is where users see them before writing.
    """
    from rb2engine.doctor import doctor_report

    stick = _make_minimal_stick(
        tmp_path / "stick",
        export_ext=True,
        engine_library=True,
        m_db=STICK_302,
        anlz_files=2,
    )
    result = doctor_report(drive_root=stick)
    text = result.render_text()

    assert result.ok is True
    assert "export.pdb" in text
    # G1d: MyTags / exportExt presence must be visible.
    assert "exportExt" in text or "exportExt.pdb" in text
    assert "USBANLZ" in text
    # Two tracks → 2 DAT, 2 EXT, 1 2EX in the fixture builder.
    assert "2" in text  # counts appear somewhere
    assert "Contents" in text
    assert "full-rebuild" in text.lower() or "full rebuild" in text.lower()
    # Existing m.db schema/uuid from the stick golden.
    assert "3.0.2" in text
    assert STICK_UUID in text


def test_doctor_drive_root_missing_export_not_ok(tmp_path: Path) -> None:
    """A folder that is not a rekordbox stick must not claim ok.

    doctor answers 'will convert work here'; missing export.pdb means no.
    """
    from rb2engine.doctor import doctor_report

    empty = tmp_path / "not_a_stick"
    empty.mkdir()
    result = doctor_report(drive_root=empty)
    text = result.render_text()

    assert result.ok is False
    assert "export.pdb" in text.lower() or "PIONEER" in text or "not" in text.lower()
