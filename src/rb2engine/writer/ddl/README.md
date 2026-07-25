# Engine DDL captures

Version-keyed SQL dumps used by `rb2engine.writer.schema` to create an empty,
Engine-valid `m.db`. Each file is **schema-only** (tables, indexes, triggers,
views) and is applied with a single `sqlite3.executescript()` call.

## Why capture from Engine, not from libdjinterop

libdjinterop is LGPL-3.0 and is consumed **only as documentation**. Copying its
literal `CREATE TABLE` / `CREATE TRIGGER` text would reproduce licensed source.
These files are captured from a database **Engine itself authored**, so the DDL
originates from Engine.

## Provenance of `schema_3_0_1.sql`

| Field | Value |
| --- | --- |
| Engine DJ | 4.3.0.159ab27b8d |
| Schema triple | 3.0.1 (`Information.schemaVersionMajor/Minor/Patch`) |
| Source database | `tests/fixtures/golden/engine_desktop_3_0_1.db` |
| Capture date | 2026-07-24 |
| Method | `sqlite_master` SQL text (`name NOT LIKE 'sqlite_%'`) |

Object inventory after replay (matches the golden Engine DB):

- **10** tables (9 user tables + `sqlite_sequence` created by AUTOINCREMENT)
- **24** indexes (16 explicit `CREATE INDEX` + 8 UNIQUE autoindexes)
- **16** triggers
- **4** views
- **54** objects total

`CREATE TABLE sqlite_sequence` is omitted from the file: SQLite treats that name
as internal and rejects a manual create; it is created automatically when
AUTOINCREMENT tables are defined / first used.

## How to capture a new schema version

When Engine DJ upgrades and writes a new `Information` schema triple:

1. **Obtain an Engine-authored `m.db`** for the new version (desktop library or
   a stick export). Prefer a small library so the file is easy to inspect.
2. **Copy it off the medium first.** Never open the live library read-write for
   capture. Prefer SQLite URI read-only mode:
   `file:/path/to/m.db?mode=ro`.
3. **Record the triple:**
   ```sql
   SELECT schemaVersionMajor, schemaVersionMinor, schemaVersionPatch, uuid
   FROM Information;
   ```
4. **Dump schema objects only** (no row data), excluding internal `sqlite_*`
   names, preserving Engine’s statement text:
   ```bash
   python - <<'PY'
   import sqlite3
   from pathlib import Path

   src = "file:/ABS/PATH/m.db?mode=ro"
   maj, minor, patch = 3, 0, 2  # from Information
   out = Path(f"schema_{maj}_{minor}_{patch}.sql")

   conn = sqlite3.connect(src, uri=True)
   rows = conn.execute(
       "SELECT sql FROM sqlite_master "
       "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' "
       "ORDER BY rowid"
   ).fetchall()
   conn.close()

   header = f"""-- Captured from Engine DJ <VERSION> (schema version {maj}.{minor}.{patch}).
-- Capture date: <YYYY-MM-DD>.
-- Source: Engine-authored m.db (path recorded in commit message).
-- Captured from Engine's own output via sqlite_master — NOT transcribed from libdjinterop.
-- Do not hand-edit. Re-capture from a real Engine m.db for new versions.

"""
   parts = [header]
   for (sql,) in rows:
       stmt = sql.strip()
       if not stmt.endswith(";"):
           stmt += ";"
       parts.append(stmt)
       parts.append("")
   out.write_text("\n".join(parts) + "\n")
   print("wrote", out, "objects", len(rows))
   PY
   ```
5. **Place the file** next to this README as `schema_<maj>_<min>_<patch>.sql`.
6. **Register it** in `rb2engine.writer.schema.SUPPORTED_SCHEMAS`:
   ```python
   SUPPORTED_SCHEMAS[(maj, minor, patch)] = "schema_X_Y_Z.sql"
   ```
7. **Do not hand-edit** the SQL body. Triggers maintain playlist linked-list
   ordering and auto-create `PerformanceData` rows; reimplementing that logic in
   Python is forbidden. If something looks odd, re-capture.
8. **Extend tests** so the new triple resolves and the Track column set (or other
   structural diffs) is pinned against a golden fixture for that version.
9. **Commit** the new `.sql`, the `SUPPORTED_SCHEMAS` entry, golden fixture if
   any, and test updates together.

Unsupported triples raise `UnsupportedFormatError` (process exit 2) from
`resolve_schema()` — gate **G3**. That is intentional: writing a schema Engine
may silently misread is worse than failing loud.

## Date/time columns and wall-clock audit (schema 3.0.1)

Engine’s DDL is replayed verbatim. Our Python must not call wall-clock APIs,
but **triggers in the captured DDL do stamp time** via `strftime('%s')`. A
post-write normalization pass in `finalize()` (not DDL edits) is the remedy if
byte-identical re-runs are required.

### DATETIME / time-related columns

| Table | Column | Policy (determinism) |
| --- | --- | --- |
| `Track` | `timeLastPlayed` | Write NULL / fixed sentinel unless mapping real play data |
| `Track` | `dateCreated` | Pin from source or fixed epoch; do not use `now` |
| `Track` | `dateAdded` | Pin from source or fixed epoch; do not use `now` |
| `Track` | `lastEditTime` | **Trigger-stamped** on some Track/PerformanceData updates (`strftime('%s')`) — normalize in `finalize()` if determinism requires it |
| `Playlist` | `lastEditTime` | Pin or normalize; playlist triggers do not appear to stamp it on insert |
| `Smartlist` | `lastEditTime` | Pin or normalize |
| `Pack` | `lastPackTime` | **Trigger-stamped** on insert when NULL (`strftime('%s')`) — avoid inserting Pack rows during convert, or normalize |

### Wall-clock constructs in captured DDL

| Location | Construct | Effect |
| --- | --- | --- |
| `trigger_after_insert_Pack_timestamp` | `strftime('%s')` | Sets `Pack.lastPackTime` when NULL |
| `trigger_after_update_only_Track_timestamp` | `strftime('%s')` | Sets `Track.lastEditTime` on metadata column updates |
| `trigger_PerformanceData_after_update_Track_timestamp` | `strftime('%s')` | Sets `Track.lastEditTime` when performance blobs change |
| Column defaults | *(none)* | No `CURRENT_TIMESTAMP` / `datetime('now')` defaults found |

No `CURRENT_TIMESTAMP` or `datetime('now')` defaults are present in 3.0.1.

## Information row (written by `create_database`, not by DDL)

| Column | Notes |
| --- | --- |
| `uuid` | **Reusable.** Carry forward from prior `m.db` / `--database-uuid`; mint only on first run. `hm.db` history keys on this value. |
| `schemaVersionMajor/Minor/Patch` | Must exactly match the target triple or Engine rejects the DB |
| `currentPlayedIndiciator` | **Spelling is intentional** (Engine typo). Opaque signed int64 |
| `lastRekordBoxLibraryImportReadCounter` | Engine importer counter; NULL/0 is fine for our writer |
