# Engine DJ desktop acceptance checklist (M5)

Manual verification of a **converted** stick in **Engine DJ desktop 4.3.0**.
This covers GUI-only criteria that CI cannot fully prove. Each item names the
**mechanical test** that should already have caught a failure — any human
fail that CI still passes is a **test-gap bug** (reproduce into
`engine_ref.db` / fixture golden and extend the suite).

**When:** after `rb2engine convert` succeeds on Tier A (`mini_stick`) and/or
the real stick (Tier B).  
**Record results in:** `.omc/research/engine-acceptance-<YYYY-MM-DD>.md`  
(copy the results table from §Results).

> **Safety:** conversion must only write under `Engine Library/` (primarily
> `Database2/m.db`). Never hand-edit production libraries without a backup.
> If inspecting databases offline, **copy first** — never open the only copy.

---

## Preconditions

1. Engine DJ **4.3.0** installed (schema target **3.0.1**).
2. Conversion completed with exit code 0 or 1 (1 = soft track skips only).
3. Conversion report available:
   - default: `<drive>/Engine Library/rb2engine-report.json`
4. Operator has the rekordbox source view of the same material (or the
   `AUTHORING.md` notes) for ground truth on BPM, downbeat, cue names, etc.

---

## Checklist

### 1. Open the converted library

| | |
|---|---|
| **Steps** | Launch Engine DJ 4.3.0 → open / connect the converted drive so `Engine Library` loads without repair prompts or schema errors. |
| **Pass** | Library opens; track count matches conversion report (`tracks converted`). No modal about a corrupt or unsupported database. |
| **Mechanical proxy** | `test_write_readback.py` (schema/integrity); `writer/database.py` `PRAGMA integrity_check` / `foreign_key_check` (G4); `rb2engine doctor` schema triple check. |

### 2. Playlist tree, names, and track order

| | |
|---|---|
| **Steps** | Expand the playlist sidebar. Walk every folder and playlist from the source tree (e.g. `RB2 Fixture` / Folder A / List Alpha…). Compare **names**, **folder nesting**, and **track order** inside each list to rekordbox / `AUTHORING.md`. |
| **Pass** | Hierarchy identical; every playlist name matches; track order in each list matches source. |
| **Mechanical proxy** | `test_mapped_ir.py` (mapped tree pin); `test_write_readback.py::test_playlist_tree` reconstructing `Playlist.parentListId` / `nextListId` and `PlaylistEntity.nextEntityId` chains vs IR. |

### 3. No missing-file indicators — **load and play**

| | |
|---|---|
| **Steps** | For **each** of the seven Tier-A tracks (or a sample of Tier B): load onto a deck **and press play**. Listen for audio; watch for missing-file / broken-path badges. Do **not** pass merely because a badge is absent while the track never loads. |
| **Pass** | Every checked track loads, plays audible audio, and shows no missing-file UI. |
| **Mechanical proxy** | `test_write_readback.py` path field assertions; PM-1 path strategy tests in `writer/paths.py`; `test_nondestructive.py` (audio still present on stick). **Gap note:** only Engine proves the path base it actually resolves on removable media. |

### 4. Adjusted beatgrid — `t02_adjusted_grid` (criterion 4)

| | |
|---|---|
| **Steps** | Load `t02_adjusted_grid`. Display the beatgrid / waveform grid. Compare **downbeat** alignment and **BPM** readout to rekordbox for the same track (±1 ms / exact BPM). |
| **Pass** | Downbeat and BPM match the adjusted rekordbox grid, not the pre-edit automatic grid. |
| **Mechanical proxy** | Fixture `t02_adjusted_grid`; decode written `beatData` and assert marker `sample_offset` within **0 samples** of mapped ANLZ; first marker beat index **-4**; `adjusted_beat_grid` from PQT2 and `default_beat_grid` from PQTZ; M2 golden encode/decode round-trip on Engine-authored blobs. |

### 4b. Tempo-change grid — `t03_tempo_change` (R6)

| | |
|---|---|
| **Steps** | Load `t03_tempo_change`. Scrub or play through the **entire** track with the grid visible. Confirm the grid **follows the tempo change** (not a single constant BPM across the whole track). |
| **Pass** | Grid tracks both tempo regions end-to-end; no obvious drift after the change point. |
| **Mechanical proxy** | Unit coverage on `t03_tempo_change` multi-marker compression in `mapper/beatgrid.py`; Tier-C **variable-tempo** shape in `engine_ref.db` golden blob round-trip (M2). |

### 5. Hot cues — `t04_cues_full` (criterion 5)

| | |
|---|---|
| **Steps** | Load `t04_cues_full`. Open hot-cue pads. Check **pad numbers (1–8)**, **colors**, and **names**, including the **non-ASCII** name. |
| **Pass** | All 8 pads populated; colors and names match rekordbox (non-ASCII not mojibake); positions audibly correct when triggered. |
| **Mechanical proxy** | `test_write_readback.py` decode of `quickCues` (pad assignment, ARGB, labels); M2 golden quickCues encode/decode; fixture pin for non-ASCII label encoding. |

### 6. Memory cues / pad fill and overflow — `t05_overflow`

| | |
|---|---|
| **Steps** | Load `t05_overflow`. Confirm Engine shows **at most 8** pad-assigned cues. Compare retained set to the conversion report’s dropped-cue list (4 hot + 8 memory → overflow). |
| **Pass** | Pads filled per merge policy; overflow cues absent from pads and **itemized** in the report; no silent loss without report entries. |
| **Mechanical proxy** | Pad merge policy unit tests in `mapper/cues.py`; `dropped_cues` in report schema tests; mapped IR golden for `t05_overflow`. |

### 7. Loops in loop slots — `t06_loops` (criterion 7)

| | |
|---|---|
| **Steps** | Load `t06_loops`. Open **loop** slots (not only hot-cue pads). Confirm the **3 saved loops** appear in loop slots with correct in/out. |
| **Pass** | Three loops present in loop UI; lengths/positions match rekordbox. |
| **Mechanical proxy** | Decode `loops` blob (uncompressed, LE count=8); `test_write_readback.py` loop offsets; M2 golden loops round-trip. |

### 7b. Hot-cue-that-is-a-loop frees its pad — `t06_loops` (U3)

| | |
|---|---|
| **Steps** | Still on `t06_loops`: identify the hot-cue-that-is-a-loop from authoring. Confirm it occupies a **loop slot** and that the corresponding **hot-cue pad is free/empty**. |
| **Pass** | Loop-capable entry is in loop storage only; pad is available (not double-booked). |
| **Mechanical proxy** | `mapper/cues.py` table-driven U3 cases; `golden_mapped_ir.json` pad/loop assignment for `t06_loops`. **Gap note:** pad-free is primarily GUI-visible. |

### 8. Loop-slot overflow — `t07_loop_overflow`

| | |
|---|---|
| **Steps** | Load `t07_loop_overflow`. Count loop slots in Engine. Open `rb2engine-report.json` → `dropped_loops`. |
| **Pass** | Engine shows **exactly 8** loops; they match the report’s **retained** set; the 2 dropped match authoring overflow (4 hot-cue loops + 6 saved = 10). |
| **Mechanical proxy** | Loop overflow policy unit tests; `dropped_loops` schema validation; `t07_loop_overflow` pin in `golden_mapped_ir.json`. |

### 9. Album artwork — resolution/quality, not mere presence (criterion 12 / U6)

| | |
|---|---|
| **Steps** | Browse/load `t01_plain`, `t02_adjusted_grid`, `t04_cues_full`, `t06_loops`. Inspect cover art in the library and deck UI. Compare to source images for **resolution and fidelity** (not just “a picture is there”). Load `t05_overflow` and confirm Engine’s **placeholder** (not a broken/corrupt image icon). |
| **Pass** | t01/t02/t04/t06 art is sharp enough to match source (reject blurry ~80×80 stand-ins if source was ~300×300); t01 and t02 show the **same** cover; t05 is placeholder-only. |
| **Mechanical proxy** | `test_write_readback.py` art assertions: **3** `AlbumArt` rows for 7 tracks; t01+t02 share `albumArtId`; t05 both art columns NULL; BLOB **byte-equal** to source image; `Track.albumArt` URI form; Tier-C shape 7 golden `AlbumArt` row; `--no-artwork` zero-row run. **Gap note:** mechanical tests cannot judge *perceptual* quality if we stored wrong-but-valid bytes — human checks resolution against source. |

---

## Results table

Copy into `.omc/research/engine-acceptance-<date>.md`.

| # | Item | Track / scope | Pass / Fail | Notes / screenshot |
|---|---|---|---|---|
| 1 | Library opens cleanly | whole stick | | |
| 2 | Playlist tree / names / order | playlists | | |
| 3 | Load **and play** (no missing files) | all 7 / sample | | |
| 4 | Adjusted downbeat + BPM | `t02_adjusted_grid` | | |
| 4b | Tempo-change grid full length | `t03_tempo_change` | | |
| 5 | Hot-cue pads / colors / names (incl. non-ASCII) | `t04_cues_full` | | |
| 6 | Pad fill + overflow vs report | `t05_overflow` | | |
| 7 | Loops in loop slots | `t06_loops` | | |
| 7b | Hot-cue-loop frees pad | `t06_loops` | | |
| 8 | Exactly 8 loops = report retained set | `t07_loop_overflow` | | |
| 9 | Art resolution/quality + t05 placeholder | t01/t02/t04/t06/t05 | | |

**Run metadata**

| Field | Value |
|---|---|
| Date | |
| Engine DJ version | 4.3.0 (confirm build) |
| rb2engine version | |
| Source | Tier A `mini_stick` / Tier B path |
| Convert exit code | |
| Report path | |
| Operator | |

---

## Failure protocol

1. **Do not** “accept with notes” without filing a gap.
2. If human fails and CI is green → open a test-gap bug: name the checklist
   item and the mechanical proxy that missed it.
3. Reproduce the correct Engine behavior into `tests/fixtures/golden/`
   (e.g. extend `engine_ref.db` shapes) so the failure becomes permanently
   CI-detectable.
4. Fix code; re-run mechanical suite; re-run this checklist for the failed
   items only.

---

## Out of scope for this checklist

- Performance timing on 3k+ track sticks (M3 timing note / M6).
- History playlists / `hm.db` contents (explicit non-goal E11).
- Engine’s built-in rekordbox importer (`rbm.db`) — independent product path.
