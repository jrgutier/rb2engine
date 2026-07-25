# rb2engine

Convert a **rekordbox** USB export into an **Engine DJ** library — on the same stick, in place, without duplicating a single audio file.

Plug the result into Denon/Engine OS gear and your tracks, playlists, beatgrids, hot cues, loops and artwork are there. The Pioneer side keeps working exactly as before, because nothing on it is touched.

```bash
rb2engine convert /Volumes/MY_USB
```

## Why this exists

Engine DJ can already import a rekordbox stick — but its export **copies your audio** into `Engine Library/Music/`. On a full stick that means a second copy of your whole library. A 44 GB library needs 44 GB free, and usually there isn't.

rb2engine writes only a database. Your music stays exactly where rekordbox put it, and both systems index the same files:

```
USB STICK
├── Contents/            ← your audio, untouched, referenced by BOTH
├── PIONEER/             ← rekordbox's export, never modified
└── Engine Library/
    └── Database2/m.db   ← the only thing rb2engine writes
```

Verified on a real 3,665-track library: **3,620 tracks, 47 playlists, ~500 MB of database** against a 44 GB library — instead of a second 44 GB.

## What transfers

| | |
|---|---|
| Tracks | title, artist, album, genre, label, comment, composer, remixer, year, track/disc number, BPM, key, rating, bitrate, length |
| Playlists | full folder hierarchy and track order |
| Beatgrids | including manually adjusted grids and tempo changes |
| Hot cues | same pad numbers (A–H), with custom colours and names |
| Memory cues | fill remaining pads in chronological order |
| Saved loops | in/out points, into Engine's separate loop slots |
| Album art | extracted from your files' embedded tags, deduplicated |

## Install

```bash
pip install rb2engine
```

Requires Python 3.11+. Works on macOS, Windows and Linux.

From source:

```bash
git clone https://github.com/jrgutier/rb2engine
cd rb2engine
uv sync
uv run rb2engine --help
```

## Usage

```bash
# Check your setup and what's on a stick — always safe to run first
rb2engine doctor /Volumes/MY_USB

# Look at what rekordbox recorded — reads only, writes nothing
rb2engine inspect /Volumes/MY_USB

# Convert
rb2engine convert /Volumes/MY_USB

# Confirm the conversion is faithful, track by track
rb2engine verify /Volumes/MY_USB



# See what would happen without writing
rb2engine convert /Volumes/MY_USB --dry-run

# Skip artwork (much faster — artwork reads every audio file)
rb2engine convert /Volumes/MY_USB --no-artwork
```

A conversion of a full stick takes minutes, so `convert` reports each phase as
it goes:

```
reading tracks ▰▰▰▰▰▰▰▰▰▰▰▰▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱  30%
```

Progress goes to stderr and switches itself off when stderr is redirected or
`--log-json` is set, so piping stays clean.

### Commands

| Command | Writes? | What it does |
|---|---|---|
| `convert` | **yes**, only inside `Engine Library/` | Builds the Engine library from the rekordbox export |
| `inspect` | no | Dumps what rekordbox recorded — tracks, playlists, cues, grids. `--json` for machine output |
| `verify` | no | Decodes the library you just wrote and diffs it against the source |
| `doctor` | no | Versions, supported schemas, and a read of the drive |

**Exit codes** (all commands): `0` all good · `1` finished with something worth
your attention — tracks skipped on `convert`, discrepancies on `verify` · `2`
fatal, nothing usable written.

Every `convert` writes `Engine Library/rb2engine-report.json` listing what
converted, what was skipped and why, and any dropped cues or loops.

### Checking a conversion worked

`verify` is the answer to "did that actually work?". Instead of opening Engine
and spot-checking a few tracks, it decodes the `m.db` that was written and
compares it against a fresh parse of the source — **at sample granularity**:

- beatgrid marker positions
- hot-cue pad number, position, ARGB colour and label
- loop in/out points
- track metadata and that each `path` resolves to a real file
- playlist order, reconstructed from Engine's linked list
- artwork counts

```bash
rb2engine verify /Volumes/MY_USB            # whole library
rb2engine verify /Volumes/MY_USB --sample 50  # first 50 tracks (much faster over USB)
```

Exit `0` if everything matches, `1` if it finds discrepancies (each one listed
with track, field, expected and actual), `2` if it could not verify at all.

### When something looks wrong

`doctor` first. It is read-only and tells you what rb2engine sees:

```bash
rb2engine doctor                      # versions and supported schemas
rb2engine doctor /Volumes/MY_USB      # + what's actually on the drive
rb2engine doctor --engine-db path/to/m.db   # is this schema supported?
```

It reports the tool and dependency versions, which Engine schemas are bundled,
the drive layout, and — if the drive already has a library — its schema and
UUID. An unsupported schema is named explicitly along with how to add it,
rather than failing with a bare error. See
[docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md).

### Options

| Flag | Purpose |
|---|---|
| `--dry-run` | Parse and map, write nothing |
| `--no-artwork` | Skip cover-art extraction |
| `--target-schema 3.0.2` | Force an Engine schema version (see *Which schema gets written* below) |
| `--database-uuid UUID` | Override the library UUID (default: reuse the existing one) |
| `--report PATH` | Write the JSON report elsewhere |
| `-v` / `-vv` | More logging · `--log-json` for machine-readable logs |

## Safety

rb2engine is built to be non-destructive, and the guarantee is tested rather than asserted:

- **Only `Engine Library/` is ever written.** A test walks the entire drive before and after a conversion and fails if anything else changed. On the real 3,665-track stick, all 24,247 files outside `Engine Library/` were byte-identical afterwards, with both `.pdb` checksums unchanged.
- **Your audio is never opened for writing.** Artwork extraction is read-only, and a test hashes source files before and after to prove it.
- **The database swap is atomic** against process failure: the library is built elsewhere and moved into place with `os.replace`, so a crash leaves your previous `m.db` intact rather than a half-written one.
- **Engine's own files survive.** `hm.db` (your play history), `sm.db`, `stm.db`, `Music/`, `Artwork/` and `OverviewData/` are preserved. The library UUID is carried forward so Engine's history stays linked.

Still: **it's a DJ library. Back it up before you point a new tool at it.**

## Things worth knowing

**Re-running rebuilds from scratch.** The Engine library is derived data. Edits made *in Engine* to the converted library are not preserved — re-export from rekordbox, re-run, and you get a clean library.

**Two rekordbox habits Engine can't represent.** Both are handled rather than fatal, and both appear in the report:

- *Duplicate playlist names in one folder.* rekordbox allows it; Engine's schema doesn't. Duplicates get a numeric suffix (`Setlist (2)`), assigned deterministically so re-runs are stable.
- *The same track twice in one playlist.* rekordbox allows it; Engine doesn't. The first occurrence is kept.

**More than 8 cues on a track.** Engine has 8 pads. Hot cues keep their original pad; memory cues fill what's left chronologically; anything beyond 8 is dropped and itemised per-track in the report. Loops go to Engine's separate 8 loop slots, so a looped hot cue frees its pad.

**Waveforms aren't generated.** Engine builds those itself when it analyses a track. Beatgrids, cues and loops are what has to survive the conversion, and they do.

## Supported versions

- **Engine DJ**: schema `3.0.1` and `3.0.2` (Engine DJ 4.x).
- **rekordbox**: exports from rekordbox 5, 6 and 7.

### Which schema gets written

Never guessed from the app version — the same Engine build was observed running
`3.0.1` on the desktop library and `3.0.2` on a USB stick. In precedence order:

1. `--target-schema`, if you pass it
2. **the schema already on the drive** — a `3.0.2` stick stays `3.0.2`
3. your own Engine desktop library, if one is readable on this machine
4. `3.0.1` as a conservative fallback

The fallback is the *oldest* supported version on purpose. Engine migrates an
older schema upward — a `3.0.1` stick was migrated in place to `3.0.2` by
Engine DJ 4.3.0 — but there is no downgrade path, so writing the newest version
to a fresh stick could hand an older Engine something it cannot open.

If your stick uses a schema rb2engine doesn't know, it **refuses to write** and tells you, rather than producing a database Engine might silently misread. Adding a version is a schema capture plus one line — see `src/rb2engine/writer/ddl/README.md`.

## How it works

```
export.pdb ────┐
               ├─→ Source IR ─→ mapper ─→ Engine IR ─→ Engine Library/Database2/m.db
ANLZ .DAT/.EXT ┘
```

- `reader/` parses `export.pdb` (DeviceSQL, via `construct`) and the ANLZ analysis files (beatgrids, cues, loops)
- `ir.py` / `ir_engine.py` decouple the two sides — no rekordbox type reaches the writer, no Engine type reaches the reader
- `mapper/` applies the semantics: key ordinals, cue-to-pad policy, beatgrid compression
- `writer/` builds the SQLite database from schema DDL captured from Engine's own output

Positions are integer sample counts throughout; the millisecond conversion happens exactly once, at the reader boundary.

## Development

```bash
uv sync
uv run pytest          # ~680 tests
uv run ruff check src/ tests/
uv run mypy src/
```

Tests marked `real_stick` need an actual rekordbox USB mounted and are skipped otherwise:

```bash
uv run pytest -m real_stick
```

The blob codecs are validated by **byte-identity against Engine's own output**: `encode(decode(blob)) == blob` for `beatData`, `quickCues`, `loops` and `trackData`, using a database Engine itself wrote. That check is what makes the encoders trustworthy.

## License

MIT — see [LICENSE](LICENSE).

All runtime dependencies are permissively licensed (MIT / BSD). See [NOTICE](NOTICE) for attribution to [crate-digger](https://github.com/Deep-Symmetry/crate-digger) and [libdjinterop](https://github.com/xsco/libdjinterop), whose format documentation made this possible — neither is included or linked, and the Engine schema is captured from Engine's own databases.

rb2engine is an independent interoperability tool, unaffiliated with AlphaTheta/Pioneer DJ or inMusic/Denon DJ.
