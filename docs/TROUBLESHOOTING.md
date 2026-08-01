# Troubleshooting

Real failure modes observed during development and the first full conversion of
a multi-thousand-track stick. Each entry has a **cause** and a **fix**.

## First steps (always)

1. **Read the JSON report** written after every convert:

   ```text
   <stick>/Engine Library/rb2engine-report.json
   ```

   It lists tracks converted vs skipped (with reasons), dropped cues/loops
   (itemised per track), path base used, and any fatal message. Override the
   path with `--report PATH` if needed.

2. **`rb2engine doctor`** — intended first stop for tool version, bundled DDL
   versions, and whether a given `--engine-db` schema triple is supported.
   If your installed build still stubs this command, use the manual checks
   under each section below (especially reading `Information` from a **copy**
   of `m.db`).

3. Re-run with more log detail if the report is ambiguous:

   ```bash
   rb2engine convert /path/to/stick -vv
   ```

**Safety:** never open a production `m.db` read-write for diagnosis. Copy it
first, then open the copy read-only.

```bash
cp "/path/to/Engine Library/Database2/m.db" /tmp/m-copy.db
sqlite3 'file:/tmp/m-copy.db?mode=ro' \
  "SELECT schemaVersionMajor, schemaVersionMinor, schemaVersionPatch, uuid FROM Information;"
```

---

## "Unsupported schema" / exit code 2

**Symptom:** convert aborts with a message like
`Unsupported Engine schema version X.Y.Z` and writes nothing usable.

**Cause:** rb2engine only writes schema triples it has captured DDL for
(currently **3.0.1** and **3.0.2**). Unknown triples fail loud (gate G3) so the
tool never invents a database Engine might silently misread.

**Important:** the Engine **app** version does not select the schema. Engine DJ
4.3.0 has been observed running **3.0.1** on the desktop library and **3.0.2**
on a stick after an in-place migration. Always read `Information` from the
actual database.

**Fix:**

1. Confirm the triple from a **copy** of the target `m.db` (SQL above).
2. If the triple is simply newer than this release supports, capture and
   register it — full runbook:
   [`src/rb2engine/writer/ddl/README.md`](../src/rb2engine/writer/ddl/README.md)
   (copy the Engine-authored DB, dump `sqlite_master` excluding `sqlite_%`,
   add the file, add one entry to `SUPPORTED_SCHEMAS`, run
   `uv run pytest tests/unit/test_schema.py -v`).
3. Force a known triple only when you understand the risk:

   ```bash
   rb2engine convert /path/to/stick --target-schema 3.0.2
   ```

   Prefer detecting from an existing library; forcing a wrong triple can make
   Engine reject or misread the DB.

---

## Tracks show as missing in Engine

**Symptom:** tracks appear in playlists or the library browser but Engine marks
them missing / cannot load audio.

**Cause:** `Track.path` does not resolve relative to how Engine looks up files
on that volume. Default path base is `engine-lib`: paths are relative to the
`Engine Library/` folder (e.g. `../Contents/Artist/track.mp3`). Wrong bases,
moved audio, or host-only absolute paths break resolution.

**Fix:**

1. Confirm audio still lives under the stick's `Contents/` (or wherever
   rekordbox exported it) and was not deleted.
2. Check the report's `path_base` field and re-run with an explicit base if
   diagnosing:

   ```bash
   rb2engine convert /path/to/stick --path-base engine-lib   # default
   rb2engine convert /path/to/stick --path-base drive-root  # Contents/... from volume root
   ```

3. **`--path-base absolute` is diagnostic-only.** It writes host absolute paths
   into the database. Those break as soon as the stick mounts at another path
   (another machine, another drive letter, another `/Volumes/...` name). Never
   leave a production library on `absolute`.
4. On case-sensitive hosts, FAT32 may have presented different casing than
   rekordbox stored; the reader matches case-insensitively, but if files were
   moved off-stick, paths cannot be recovered automatically.

---

## Playlists renamed with `(2)` suffixes

**Symptom:** a playlist named e.g. `Setlist Bigroom` appears as
`Setlist Bigroom (2)` or `(3)`.

**Cause:** rekordbox allows **duplicate playlist names in the same folder**.
Engine does not — schema constraint
`UNIQUE (title, parentListId)` (`C_NAME_UNIQUE_FOR_PARENT`). A real conversion
hit three `"Setlist Bigroom"` and two `"Setlist Classic"` under one folder and
would have aborted the write without disambiguation.

**Fix:** nothing required for a successful convert — renames are deterministic
(sibling order by sort key / id) so re-runs stay stable. Rename in rekordbox
before export if you want specific names in Engine. Check logs/report for which
titles were adjusted.

---

## `verify` reports extra playlists after you opened Engine DJ

**Symptom:** `convert` succeeds and `verify` is clean, then a later `verify`
reports something like:

```
track library: playlist_count: expected=45 actual=48
```

and names playlists that exist in Engine but in no rekordbox playlist.

**Cause:** opening Engine DJ with the drive attached can **merge Engine's own
desktop library onto the stick**. Those playlists are real and were added by
Engine on purpose — they are not corruption, and `rb2engine` did not write them.
`verify` is doing its job: it compares the database against the rekordbox
source, and after a desktop merge the database legitimately holds more than the
source describes.

Confirmed by experiment on a 3,673-track library. Identical conversion both
times; the only variable was Engine's desktop database:

| Engine desktop library | Playlists after opening Engine |
|---|---|
| Populated | 48 — three added, playlist ids running past our allocation |
| Cleared | 45 — none added, no row altered |

**How to tell them apart from a real defect.** `rb2engine` pins `lastEditTime`
to `1970-01-01 00:00:00` on every playlist row it writes, so anything with a
real timestamp came from Engine:

```bash
sqlite3 "/Volumes/MY_USB/Engine Library/Database2/m.db" \
  "SELECT id, title, lastEditTime FROM Playlist
    WHERE lastEditTime <> '1970-01-01 00:00:00';"
```

Rows listed there were written by Engine, not by this tool. A genuine
conversion defect would carry the pinned epoch.

**Fix:** nothing is broken. **Run `verify` immediately after `convert`, before
launching Engine DJ** — that is the only moment the database is guaranteed to
contain exactly what the conversion produced. If you want the stick to match
rekordbox exactly, re-run `convert`; it rebuilds the database from the source.

Note that Engine also rewrites `m.db` harmlessly on open (the file's timestamp
and size change by a page or two) even when it merges nothing. A changed
timestamp alone does not mean your library was modified.

---

## A track appears once when it was in a playlist twice

**Symptom:** a track that was listed twice in one rekordbox playlist appears
only once in Engine.

**Cause:** Engine forbids the same track twice in one playlist:
`UNIQUE (listId, databaseUuid, trackId)` (`C_NAME_UNIQUE_FOR_LIST`). rekordbox
allows repeats. The writer keeps the **first** occurrence (the position the set
was built around) and drops later duplicates, counting them in logs.

**Fix:** intentional compatibility behaviour. Rebuild the playlist in rekordbox
without repeats if you need a different first occurrence. Duplicates across
*different* playlists are unaffected.

---

## More than 8 cues

**Symptom:** some hot cues or memory cues from rekordbox do not appear on
Engine pads.

**Cause:** Engine has **8 pads**. Mapping policy:

- Hot cues keep their original pad numbers (A–H).
- Memory cues fill remaining pads in chronological order.
- Anything beyond 8 pads is **dropped**.
- Saved loops go to Engine's **separate** 8 loop slots (a looped hot cue frees
  its pad rather than consuming both).

**Fix:** open the report and inspect `dropped_cues` / `dropped_loops` (and the
per-track lists). Overflow is itemised, not silent. Reduce cue count in
rekordbox or accept the pad cap — there is no ninth pad to map into.

---

## Artwork missing

**Symptom:** some tracks have no cover art in Engine after conversion.

**Cause:**

- Not every file has **embedded** artwork; rb2engine extracts from tags, it
  does not scrape web databases or copy rekordbox's internal cache as primary
  source.
- Conversion was run with artwork disabled.
- Corrupt or unsupported tag frames are skipped (track still converts).

**Fix:**

- Inspect the report counters for artwork missing / skips.
- Embed art in the audio files and re-run if you need it in Engine.
- Skip extraction deliberately when speed matters:

  ```bash
  rb2engine convert /path/to/stick --no-artwork
  ```

---

## Conversion is slow

**Symptom:** convert takes a long time on a large stick, especially over USB.

**Cause:** with artwork enabled, extraction **opens every audio file** on the
volume (often USB/FAT32). That dominates runtime far more than writing SQLite
rows. The database itself is built on **local** storage and copied into place
to avoid thousands of small journaled writes over USB.

**Fix:**

```bash
rb2engine convert /path/to/stick --no-artwork
```

Re-run later with artwork if needed. Prefer a direct USB port / avoid hubs for
full-art runs. Dry-run still parses the library but writes nothing:

```bash
rb2engine convert /path/to/stick --dry-run
```

---

## "attempt to write a readonly database" on FAT32

**Symptom:** SQLite error mentioning a readonly database while the volume is
clearly mounted writable; historically seen with large DDL scripts on macOS
FAT32 (fskit).

**Cause:** building `m.db` **directly on** some removable FAT32 drivers fails
during `executescript()` of Engine's full DDL even though plain
`CREATE TABLE` + commit can succeed. That is a driver/journal interaction, not
an intentional read-only mount.

**Fix:** current rb2engine **builds the database on local disk and copies it
into** `Engine Library/Database2/`, then installs with an atomic replace. You
should not see this on a normal convert.

If it **recurs** on a current release:

1. Confirm the volume is not literally mounted read-only (`mount` / disk
   utility).
2. Confirm free space for both the local temp build and the stick copy
   (~hundreds of MB for a large library DB, not another full audio library).
3. Collect `-vv` logs, OS version, how the volume is formatted, and open a
   GitHub issue — include whether `Engine Library/` was created and whether a
   stray `m.db.tmp` remains (safe to delete; never adopt it as the library).

---

## Partial success (exit code 1)

**Symptom:** convert finishes but the process exits `1`.

**Cause:** at least one track was **skipped** (unreadable path, missing file,
parse failure, etc.). The library that *did* convert was still written.

**Fix:** open `rb2engine-report.json` → `skipped_tracks` and address those
entries. Exit codes: `0` clean · `1` converted with skips · `2` fatal (nothing
usable written).

---

## Still stuck?

- GitHub issues: https://github.com/jrgutier/rb2engine/issues  
- Schema capture runbook: [`src/rb2engine/writer/ddl/README.md`](../src/rb2engine/writer/ddl/README.md)  
- Contributing / test expectations: [`CONTRIBUTING.md`](../CONTRIBUTING.md)

When filing a bug, attach a redacted report JSON, the schema triple from a
**copy** of `m.db`, Engine DJ version, OS, and whether the failure is on first
run or re-run. Do not upload an entire production library.
