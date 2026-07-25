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
# Look at what's on a stick — reads only, writes nothing
rb2engine inspect /Volumes/MY_USB

# Convert
rb2engine convert /Volumes/MY_USB

# See what would happen without writing
rb2engine convert /Volumes/MY_USB --dry-run

# Skip artwork (much faster — artwork reads every audio file)
rb2engine convert /Volumes/MY_USB --no-artwork
```

**Exit codes:** `0` clean · `1` converted, but some tracks were skipped · `2` fatal, nothing usable written.

Every run writes `Engine Library/rb2engine-report.json` listing what converted, what was skipped and why, and any dropped cues or loops.

### Options

| Flag | Purpose |
|---|---|
| `--dry-run` | Parse and map, write nothing |
| `--no-artwork` | Skip cover-art extraction |
| `--target-schema 3.0.2` | Force an Engine schema version (default: read from the existing database) |
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

- **Engine DJ**: schema `3.0.1` and `3.0.2` (Engine DJ 4.x). The target version is read from your existing database rather than guessed — the same Engine build can run different schema versions on the desktop and on a stick.
- **rekordbox**: exports from rekordbox 5, 6 and 7.

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
uv run pytest          # ~430 tests
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
