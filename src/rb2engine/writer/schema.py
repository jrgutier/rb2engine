"""DDL loader and version gate G3; SUPPORTED_SCHEMAS keyed by version triple."""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path

from rb2engine.errors import UnsupportedFormatError

# Directory of version-keyed DDL captures (Engine-authored, not libdjinterop).
DDL_DIR = Path(__file__).resolve().parent / "ddl"

# Map Engine Information schema triple → DDL filename under DDL_DIR.
# Both triples are produced by Engine DJ 4.3.0 (desktop = 3.0.1, stick = 3.0.2
# after in-place migration). App version does not select the schema — detect
# from the target m.db. Add new dumps via ddl/README.md.
SUPPORTED_SCHEMAS: dict[tuple[int, int, int], str] = {
    (3, 0, 1): "schema_3_0_1.sql",
    (3, 0, 2): "schema_3_0_2.sql",
}

# Opaque Information.currentPlayedIndiciator (sic). Engine accepts any signed
# int64 (real desktop value was negative); pin 0 so rebuilds stay deterministic
# once uuid is carried forward.
_DEFAULT_PLAYED_INDICATOR = 0


def resolve_schema(triple: tuple[int, int, int]) -> Path:
    """Return the path to the DDL file for *triple*, or raise (gate G3).

    Unknown schema versions must not produce a database Engine may silently
    misread. Callers map UnsupportedFormatError to process exit code 2.
    """
    filename = SUPPORTED_SCHEMAS.get(triple)
    if filename is None:
        supported = ", ".join(
            f"{maj}.{minor}.{patch}"
            for maj, minor, patch in sorted(SUPPORTED_SCHEMAS)
        )
        maj, minor, patch = triple
        raise UnsupportedFormatError(
            f"Unsupported Engine schema version {maj}.{minor}.{patch}. "
            f"Supported versions: {supported}. "
            f"To capture a new version from Engine's own m.db, see "
            f"src/rb2engine/writer/ddl/README.md."
        )
    path = DDL_DIR / filename
    if not path.is_file():
        raise UnsupportedFormatError(
            f"DDL file missing for schema {triple[0]}.{triple[1]}.{triple[2]}: "
            f"{path}. Re-capture per src/rb2engine/writer/ddl/README.md."
        )
    return path


def create_database(
    path: str | Path,
    schema: tuple[int, int, int],
    *,
    database_uuid: str | None = None,
) -> sqlite3.Connection:
    """Create an empty Engine-valid database at *path* for *schema*.

    Applies the captured DDL via ``executescript()`` (tables, indexes,
    triggers, views — never reimplemented in Python), sets
    ``PRAGMA journal_mode=DELETE`` (not WAL; matches Engine on FAT32), and
    inserts the single ``Information`` row.

    *database_uuid*: when provided, written unchanged so rebuilds do not orphan
    ``hm.db`` play-history origin links. When omitted, a random UUID is minted
    (first-run only path).

    Returns an open connection; the caller owns close/commit lifecycle after
    further writes. The Information insert is committed so a crash after return
    still leaves a valid empty Engine DB.
    """
    ddl_path = resolve_schema(schema)
    ddl = ddl_path.read_text(encoding="utf-8")

    db_uuid = database_uuid if database_uuid is not None else str(uuid.uuid4())
    maj, minor, patch = schema

    path = Path(path)
    if path.parent and str(path) not in {":memory:", ""}:
        path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(path))
    try:
        # Must be DELETE, not WAL — removable FAT32 volumes lack reliable shm.
        mode = conn.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
        if str(mode).lower() != "delete":
            conn.close()
            raise UnsupportedFormatError(
                f"Could not set PRAGMA journal_mode=DELETE (got {mode!r}). "
                "WAL is not safe on typical removable Engine library volumes."
            )

        conn.executescript(ddl)

        # Column name currentPlayedIndiciator is Engine's spelling — do not "fix".
        conn.execute(
            """
            INSERT INTO Information (
                uuid,
                schemaVersionMajor,
                schemaVersionMinor,
                schemaVersionPatch,
                currentPlayedIndiciator,
                lastRekordBoxLibraryImportReadCounter
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (db_uuid, maj, minor, patch, _DEFAULT_PLAYED_INDICATOR, None),
        )
        conn.commit()
    except Exception:
        conn.close()
        raise

    return conn
