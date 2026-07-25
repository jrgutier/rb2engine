# Engine DDL captures — schema-version runbook

Version-keyed SQL dumps used by `rb2engine.writer.schema` to create an empty,
Engine-valid `m.db`. Each file is **schema-only** (tables, indexes, triggers,
views) and is applied with a single `sqlite3.executescript()` call.

When Engine ships a schema rb2engine does not know, conversion **refuses to
write** (`UnsupportedFormatError`, process exit 2). That is intentional:
producing a database Engine may silently misread is worse than failing loud.
Adding a version is a ~10-minute capture, not a rewrite.

---

## Hard-won fact: app version ≠ schema version

**The Engine DJ application version does NOT determine the schema version.**

Always read `Information` from the **actual database** you are about to
replace (or from the Engine-authored library you are capturing). Do not guess
from the installer version string.

Observed on one machine with **Engine DJ 4.3.0.159ab27b8d**:

| Library | Path | Schema triple |
| --- | --- | --- |
| Desktop | `~/Music/Engine Library/Database2/m.db` | **3.0.1** |
| USB stick | `<stick>/Engine Library/Database2/m.db` | **3.0.2** (migrated in place from 3.0.1 by the same app build) |

Both triples are valid Engine 4.3.0 output. `SUPPORTED_SCHEMAS` is therefore
keyed by `(major, minor, patch)` from `Information`, not by a marketing
version number.

```sql
SELECT schemaVersionMajor, schemaVersionMinor, schemaVersionPatch, uuid
FROM Information;
```

---

## Why capture from Engine, not from libdjinterop

[libdjinterop](https://github.com/xsco/libdjinterop) is **LGPL-3.0**. This
project uses it **only as documentation** (format notes, table intent).
Copying its literal `CREATE TABLE` / `CREATE TRIGGER` text would reproduce
licensed source.

These `.sql` files are captured from a database **Engine itself authored**,
via `sqlite_master`. The DDL originates from Engine's output, not from a
third-party transcription. Do not hand-edit the SQL body to "match" libdjinterop
or to invent columns.

---

## Shipped captures

| File | Triple | Source | Notes |
| --- | --- | --- | --- |
| `schema_3_0_1.sql` | 3.0.1 | Desktop library → `tests/fixtures/golden/engine_desktop_3_0_1.db` | 42 Track columns; no `albumArtSourceHash` |
| `schema_3_0_2.sql` | 3.0.2 | Stick library after Engine 4.3.0 in-place migration | 43 Track columns; adds `albumArtSourceHash` |

### Provenance of `schema_3_0_1.sql`

| Field | Value |
| --- | --- |
| Engine DJ | 4.3.0.159ab27b8d |
| Schema triple | 3.0.1 |
| Source database | `tests/fixtures/golden/engine_desktop_3_0_1.db` |
| Capture date | 2026-07-24 |
| Method | `sqlite_master` SQL text (`name NOT LIKE 'sqlite_%'`) |

Object inventory after replay (matches the golden Engine DB):

- **10** tables (9 user tables + `sqlite_sequence` created by AUTOINCREMENT)
- **24** indexes (16 explicit `CREATE INDEX` + 8 UNIQUE autoindexes)
- **16** triggers
- **4** views
- **54** objects total

`CREATE TABLE sqlite_sequence` is omitted from the file: SQLite treats that
name as internal and rejects a manual create; it is created automatically when
AUTOINCREMENT tables are defined / first used.

### Provenance of `schema_3_0_2.sql`

| Field | Value |
| --- | --- |
| Engine DJ | 4.3.0.159ab27b8d (migrated stick DB 3.0.1 → 3.0.2 in place) |
| Schema triple | 3.0.2 |
| Capture date | 2026-07-25 |
| Structural delta vs 3.0.1 | `Track.albumArtSourceHash` |

---

## How to capture a new schema version (10-minute path)

Someone who has never seen this repo should be able to follow these steps when
a user reports `Unsupported Engine schema version X.Y.Z`.

### 1. Find an Engine-authored `m.db`

Typical locations:

| Platform | Path |
| --- | --- |
| **macOS desktop** | `~/Music/Engine Library/Database2/m.db` |
| **Windows desktop** | `%USERPROFILE%\Music\Engine Library\Database2\m.db` (confirm in Engine's library settings if moved) |
| **USB / SD stick** | `<mount>/Engine Library/Database2/m.db` |

Prefer a small library so the file is easy to inspect and copy.

### 2. Copy first — never open the live database read-write

```bash
# macOS example
cp "$HOME/Music/Engine Library/Database2/m.db" /tmp/engine-capture-m.db
```

**Never** open the user's live library (desktop or production stick) with a
read-write SQLite connection for capture. Prefer SQLite URI read-only mode on
the **copy**:

```text
file:/tmp/engine-capture-m.db?mode=ro
```

The user's DJ library is irreplaceable. Treat every production `m.db` as
read-only reference data.

### 3. Record the schema triple (and app version for the header)

```bash
sqlite3 'file:/tmp/engine-capture-m.db?mode=ro' \
  "SELECT schemaVersionMajor, schemaVersionMinor, schemaVersionPatch, uuid FROM Information;"
```

Also note the Engine DJ version string from the app (About box), for the
file header only — it does **not** select which triple to register.

### 4. Dump schema objects only (exclude `sqlite_%` internals)

No row data. Preserve Engine's statement text from `sqlite_master`:

```bash
python - <<'PY'
import sqlite3
from pathlib import Path

src = "file:/tmp/engine-capture-m.db?mode=ro"
# Fill these from the SELECT in step 3 — never invent them.
maj, minor, patch = 3, 0, 3
engine_version = "4.x.y"  # from the About box
capture_date = "YYYY-MM-DD"
source_note = "desktop copy at ~/Music/Engine Library/Database2/m.db"

out = Path(f"schema_{maj}_{minor}_{patch}.sql")

conn = sqlite3.connect(src, uri=True)
rows = conn.execute(
    "SELECT sql FROM sqlite_master "
    "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' "
    "ORDER BY rowid"
).fetchall()
conn.close()

header = f"""-- Captured from Engine DJ {engine_version} (schema version {maj}.{minor}.{patch}).
-- Capture date: {capture_date}.
-- Source: Engine-authored m.db ({source_note}).
-- Captured from Engine's own output via sqlite_master — NOT transcribed from libdjinterop.
-- libdjinterop is LGPL-3.0 and is used only as documentation; copying its
-- literal SQL would reproduce licensed source text.
-- Do not hand-edit this file. Re-capture from a real Engine m.db when
-- supporting a new schema version (see README.md in this directory).
--
-- IMPORTANT: the Engine app version does NOT determine the schema version.
-- Always read Information from the actual database you capture or replace.

"""

parts = [header]
for (sql,) in rows:
    stmt = sql.strip()
    if not stmt.endswith(";"):
        stmt += ";"
    parts.append(stmt)
    parts.append("")
out.write_text("\n".join(parts) + "\n", encoding="utf-8")
print("wrote", out, "objects", len(rows))
PY
```

Equivalently with the `sqlite3` CLI (same exclusion rule):

```bash
sqlite3 'file:/tmp/engine-capture-m.db?mode=ro' \
  "SELECT sql || ';' FROM sqlite_master
   WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'
   ORDER BY rowid;" > /tmp/schema_body.sql
```

Then prepend the provenance header by hand.

### 5. What the provenance header must record

Every capture file's leading comments must include:

1. **Engine DJ version string** (from the app that wrote the DB)
2. **Schema triple** `major.minor.patch` from `Information`
3. **Capture date** (`YYYY-MM-DD`)
4. **Source** — which Engine-authored `m.db` (desktop path, stick, or path
   recorded in the commit message if the file itself is not committed)
5. Explicit statement that the dump is from **Engine's `sqlite_master`**, not
   transcribed from libdjinterop
6. Reminder that **app version ≠ schema version**

### 6. Place the file and register the triple

1. Move the dump next to this README as `schema_<maj>_<min>_<patch>.sql`.
2. Register it in `rb2engine.writer.schema.SUPPORTED_SCHEMAS`:

```python
SUPPORTED_SCHEMAS: dict[tuple[int, int, int], str] = {
    (3, 0, 1): "schema_3_0_1.sql",
    (3, 0, 2): "schema_3_0_2.sql",
    (maj, minor, patch): "schema_X_Y_Z.sql",  # new
}
```

3. **Do not hand-edit** the SQL body. Triggers maintain playlist linked-list
   ordering and auto-create `PerformanceData` rows; reimplementing that logic
   in Python is forbidden. If something looks odd, re-capture.

### 7. Run the schema pin tests

```bash
uv run pytest tests/unit/test_schema.py -v
```

These tests pin object inventory and column sets against Engine-authored
fixtures (or, for a brand-new triple, against expectations you must extend
from a golden copy of that Engine DB — never from replaying our own dump and
calling the result "expected").

Also extend tests so the new triple:

- resolves via `resolve_schema()`
- creates a DB whose Track column set (and other structural diffs) is pinned
  against a golden fixture for **that** version when available

### 8. Commit as one unit

Commit the new `.sql`, the `SUPPORTED_SCHEMAS` entry, any golden fixture, and
test updates together. Unsupported triples continue to raise
`UnsupportedFormatError` from `resolve_schema()` — gate **G3**.

---

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
