# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.0] - 2026-07-25

### Added
- Fresh sticks now adopt the schema from your own Engine desktop library when
  one is readable, instead of always falling back to a fixed default. A drive
  that already has a library still keeps its own version.
- `verify` now compares album-art **bytes**, not just AlbumArt row counts.
  Swapped, truncated or re-encoded images keep the count identical while the
  DJ sees the wrong cover.
- Coverage measurement with an 85% floor, enforced in CI and on release.

### Changed
- README documents `verify` and `doctor`, and the schema precedence rules.

### Testing
- 640 tests, 87% branch coverage (was 476 / 81% statement).
- `reader/tags.py` 52% -> 98%, `reader/library.py` 32% -> 98%,
  `writer/build.py` 77% -> 99%, `writer/artwork.py` 70% -> 100%.
  `library.py` mattered most: it is the orchestration join whose absence broke
  the pipeline once, and its only real tests were hardware-gated and skipped
  in CI.

## [0.2.0] - 2026-07-25

### Added
- `rb2engine verify` — decodes the written `m.db` and diffs it against a fresh
  parse of the source at sample granularity: beatgrid markers, cue pad index,
  ARGB colour, label, loop points, playlist order and artwork counts. Turns
  spot-checking a few tracks in the GUI into a mechanical check across the
  whole library. Exits 1 on any discrepancy.
- `rb2engine doctor` — reports tool and dependency versions, bundled schema
  support, and what is actually on a drive. Given an unsupported Engine
  schema it names the version and points at the capture runbook rather than
  failing bare. Strictly read-only.
- `CONTRIBUTING.md`, `docs/TROUBLESHOOTING.md`, and a schema-capture runbook
  in `writer/ddl/README.md`.

### Fixed
- Gate G1c's malformed *optional consumed* table branch (e.g. a corrupt
  `artists` table) had no pinning test — silently losing every artist name is
  exactly the plausible-looking-wrong-output failure the design guards
  against. Now byte-patched and tested.
- G1a: a pdb header listing the same consumed `page_type` twice was previously
  accidental behaviour. Now an explicit first-wins-with-warning policy.
- `test_no_wallclock` only grepped `writer/`, but time-derived values are
  wired through `mapper/track.py` too — half the surface was unchecked, which
  would have made determinism flaky across a second boundary.
- Converting on macOS left a `._m.db` AppleDouble sidecar in the user's
  Engine Library. Removed surgically (only our own file's sidecar, never a
  blanket `._*` sweep), and a no-op off macOS.

### Changed
- The stale "unimplemented command" test is replaced by one asserting the CLI
  has no stubs at all — it had gone stale three times as commands landed.

## [0.1.0] — 2026-07-25

First public release. Converts a rekordbox USB export into an Engine DJ library
on the same stick, referencing audio in place rather than copying it.

**Verified end-to-end** on a real **3,665-track** library: **3,620** tracks,
**47** playlists, beatgrids, hot cues, loops and album art, with playback
confirmed in **Engine DJ 4.3.0** from the stick. Nothing outside
`Engine Library/` is written.

### Added

- **Reader pipeline** for rekordbox USB exports:
  - Hand-written `export.pdb` parser (`construct`) with DeviceSQL strings,
    paged table walk, and path resolution suitable for FAT32 sticks
  - ANLZ analysis reader (beatgrids, hot cues, memory cues, loops) via a
    per-tag allowlist walk that survives tags pyrekordbox cannot parse
  - Drive layout scan with case-insensitive matching; artwork extraction from
    embedded tags (MP4 / ID3 / FLAC) without a GPL runtime dependency
- **Intermediate representation** (`ir.py` / `ir_engine.py`) that fully
  decouples rekordbox types from the Engine writer
- **Mappers** for keys (Camelot / Open Key / enharmonics), cue-to-pad policy
  (hot cues keep pads; memory cues fill remaining slots; loops free pads),
  and beatgrid compression
- **Writer pipeline** for Engine Library `Database2/m.db`:
  - PerformanceData blob codecs (`trackData`, `beatData`, `quickCues`,
    `loops`) validated by **byte-identity against Engine's own output**
    (`encode(decode(blob)) == blob`)
  - Schema DDL **captured from Engine-authored databases** (not transcribed
    from libdjinterop): triples **3.0.1** and **3.0.2**
  - Atomic local-stage build + `os.replace` install; sibling DBs (`hm.db`,
    `sm.db`, `stm.db`) and `Music/` / `Artwork/` / `OverviewData/` preserved
  - Playlist tree with deterministic rename of duplicate sibling names and
    de-duplication of repeated track membership (Engine schema constraints)
- **CLI** (`rb2engine`):
  - `convert` — full conversion onto the stick
  - `inspect` — read-only library dump
  - Flags: `--dry-run`, `--no-artwork`, `--path-base`, `--target-schema`,
    `--database-uuid`, `--report`, logging controls
  - JSON conversion report under `Engine Library/rb2engine-report.json`
- **Packaging**: MIT-licensed runtime deps only; installable from PyPI as
  `rb2engine`; CI matrix macOS / Windows / Ubuntu × Python 3.11–3.13

### Notes

- The same Engine DJ build can run different schema versions on desktop vs
  stick; the target triple is read from the existing `m.db` (or
  `--target-schema`), never assumed from the app version string.
- Waveforms are not generated — Engine analyses them on load. Beatgrids, cues
  and loops are what must survive conversion.
- `verify` and `doctor` commands are reserved stubs in this release.

[0.1.0]: https://github.com/jrgutier/rb2engine/releases/tag/v0.1.0
