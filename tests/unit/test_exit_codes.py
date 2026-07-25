"""Process exit codes 0 / 1 / 2 — the codes users actually get from the CLI.

WHY these tests exist
---------------------
Criterion 9 of the plan is the operator contract: clean → 0, soft skips → 1
with machine-stable reason codes, fatals → 2 with no partial library left
behind. "No partial library" means two different things (U7):

* **Fresh stick** (no ``Engine Library/``): fatal leaves *no* ``Engine Library/``.
* **Pre-existing library** (the common re-export path): fatal leaves the prior
  ``m.db`` byte-identical and leaves **no** ``m.db.tmp`` residue.

These are driven through ``click.testing.CliRunner`` so we pin the exit codes
the click app returns, not an internal helper that the CLI might ignore.
All drives are ``tmp_path`` fakes — never the real USB stick.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest
from click.testing import CliRunner

from rb2engine.cli import main
from rb2engine.errors import UnsupportedFormatError
from rb2engine.ir import SourceLibrary, SourcePlaylist, SourceTrack

# Reuse the hand-authored DeviceSQL builders from test_pdb (no committed binaries).
from tests.unit.test_pdb import (
    PAGE_HEADER,
    _artist_row,
    _build_data_page,
    _build_nondata_page,
    _file_header,
    _pack_row_index,
    _page_header,
    _playlist_entry_row,
    _playlist_tree_row,
    _track_row,
    _write_pdb,
)

# ---------------------------------------------------------------------------
# Stick layout helpers (tmp_path only)
# ---------------------------------------------------------------------------


def _source_track(
    rb_id: int,
    *,
    drive: Path,
    title: str = "Song",
    resolved: Path | object | None = "auto",
) -> SourceTrack:
    if resolved == "auto":
        path = drive / "Contents" / f"{rb_id}.mp3"
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.is_file():
            path.write_bytes(b"fake-audio")
        resolved = path
    assert resolved is None or isinstance(resolved, Path)
    return SourceTrack(
        rb_id=rb_id,
        title=title,
        artist="Artist",
        album="",
        genre="",
        label="",
        comment="",
        composer="",
        remixer="",
        year=2024,
        track_number=1,
        disc_number=None,
        bpm=128.0,
        key_name=None,
        rating=0,
        play_count=0,
        bitrate=320,
        file_size=1000,
        file_type="mp3",
        sample_rate=44100,
        duration_s=60,
        total_samples=44100 * 60,
        raw_path=f"/Contents/{rb_id}.mp3",
        resolved_path=resolved,
        analyze_path=None,
        beatgrid=None,
        cues=[],
        artwork=None,
    )


def _seed_prior_mdb(drive: Path, *, payload: bytes = b"PRIOR-MDB-BYTES-v1") -> Path:
    """Create a pre-existing Engine Library/Database2/m.db with known bytes."""
    m_db = drive / "Engine Library" / "Database2" / "m.db"
    m_db.parent.mkdir(parents=True, exist_ok=True)
    m_db.write_bytes(payload)
    # Sibling that must never be touched by a fatal path that never reaches swap.
    (m_db.parent / "hm.db").write_bytes(b"hm-sibling")
    return m_db


def _write_mini_export_pdb(
    drive: Path,
    *,
    file_path: str = "/Contents/Artist/track.mp3",
    title: str = "Labirinto",
    include_artists: bool = True,
    corrupt_artists: bool = False,
    bad_len_page: bool = False,
) -> Path:
    """Write PIONEER/rekordbox/export.pdb + USBANLZ on a fake stick."""
    len_page = 1024
    pioneer = drive / "PIONEER"
    rb_dir = pioneer / "rekordbox"
    rb_dir.mkdir(parents=True, exist_ok=True)
    (pioneer / "USBANLZ").mkdir(parents=True, exist_ok=True)

    tables: list[tuple[int, int, int]] = [
        (0, 1, 2),  # tracks: nondata → data
        (7, 7, 7),  # playlist_tree
        (8, 8, 8),  # playlist_entries
    ]
    pages: dict[int, bytes] = {
        1: _build_nondata_page(
            len_page=len_page, page_index=1, page_type=0, next_page=2
        ),
        2: _build_data_page(
            len_page=len_page,
            page_index=2,
            page_type=0,
            next_page=99,
            row_blobs=[
                _track_row(
                    tid=42,
                    title=title,
                    file_path=file_path,
                    artist_id=7 if include_artists else 0,
                    sample_rate=44100,
                    tempo=12800,
                    duration=180,
                )
            ],
        ),
        7: _build_data_page(
            len_page=len_page,
            page_index=7,
            page_type=7,
            next_page=99,
            row_blobs=[
                _playlist_tree_row(
                    parent_id=0,
                    sort_order=0,
                    pl_id=1,
                    is_folder=False,
                    name="Main",
                )
            ],
        ),
        8: _build_data_page(
            len_page=len_page,
            page_index=8,
            page_type=8,
            next_page=99,
            row_blobs=[_playlist_entry_row(0, 42, 1)],
        ),
    }
    if include_artists:
        tables.insert(1, (2, 4, 4))
        if corrupt_artists:
            junk = bytearray(len_page)
            junk[0:PAGE_HEADER] = _page_header(
                page_index=4,
                page_type=2,
                next_page=99,
                num_row_offsets=1,
                num_rows=1,
                page_flags=0x24,
            )
            junk[PAGE_HEADER : PAGE_HEADER + 2] = b"\xff\xff"
            _pack_row_index(junk, len_page, [0])
            pages[4] = bytes(junk)
        else:
            pages[4] = _build_data_page(
                len_page=len_page,
                page_index=4,
                page_type=2,
                next_page=99,
                row_blobs=[_artist_row(7, "Cour T_")],
            )

    pages[0] = _file_header(len_page=len_page, tables=tables)
    pdb_path = rb_dir / "export.pdb"
    _write_pdb(pdb_path, len_page, pages)

    if bad_len_page:
        blob = bytearray(pdb_path.read_bytes())
        struct.pack_into("<I", blob, 4, 3000)  # not a power of two → G1a
        pdb_path.write_bytes(blob)

    return pdb_path


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


# ---------------------------------------------------------------------------
# Exit 0 — clean conversion
# ---------------------------------------------------------------------------


def test_clean_conversion_exits_zero(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fully resolvable library must exit 0 (criterion 9 clean path).

    Monkeypatches ``read_library`` so this pins the CLI exit mapping even if
    the concurrent reader workers' stick layout differs; ``build_library``
    still runs for real.
    """
    drive = tmp_path / "stick"
    drive.mkdir()
    track = _source_track(1, drive=drive, title="Clean")
    lib = SourceLibrary(
        drive_root=drive,
        tracks={1: track},
        playlists=[
            SourcePlaylist(
                rb_id=1,
                parent_rb_id=0,
                name="Set",
                sort_order=0,
                is_folder=False,
                track_rb_ids=[1],
            )
        ],
        warnings=[],
    )
    monkeypatch.setattr(
        "rb2engine.reader.library.read_library",
        lambda *_a, **_k: lib,
    )

    result = runner.invoke(main, ["convert", str(drive), "--no-artwork"])
    assert result.exit_code == 0, result.output + (result.stderr or "")
    assert (drive / "Engine Library" / "Database2" / "m.db").is_file()


# ---------------------------------------------------------------------------
# Exit 1 — soft skip with machine-stable reason_code
# ---------------------------------------------------------------------------


def test_skipped_track_exits_one_with_reason_code(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One unresolvable track → exit 1 and JSON itemises a stable reason_code.

    WHY: automation keys on ``reason_code``, not free-text messages. A skip
    that only prints a human line fails criterion 9's machine half.
    """
    drive = tmp_path / "stick"
    drive.mkdir()
    # resolved_path=None is the build_library skip path → unresolvable_path
    bad = _source_track(7, drive=drive, title="Ghost", resolved=None)
    good = _source_track(8, drive=drive, title="Alive")
    lib = SourceLibrary(
        drive_root=drive,
        tracks={7: bad, 8: good},
        playlists=[],
        warnings=[],
    )
    monkeypatch.setattr(
        "rb2engine.reader.library.read_library",
        lambda *_a, **_k: lib,
    )

    report_path = tmp_path / "out-report.json"
    result = runner.invoke(
        main,
        ["convert", str(drive), "--no-artwork", "--report", str(report_path)],
    )
    assert result.exit_code == 1, result.output + (result.stderr or "")

    assert report_path.is_file(), "JSON report must be written on the skip path"
    data = json.loads(report_path.read_text(encoding="utf-8"))
    skips = data["skipped_tracks"]
    assert skips, "skipped track must be itemised in the JSON report"
    codes = {s["reason_code"] for s in skips}
    assert "unresolvable_path" in codes, (
        f"expected machine-stable reason_code unresolvable_path, got {codes!r}"
    )
    assert any(s["track_id"] == 7 for s in skips)


# ---------------------------------------------------------------------------
# Exit 2 — no PIONEER / fresh stick leaves nothing behind
# ---------------------------------------------------------------------------


def test_no_pioneer_exits_two_no_engine_library_left(
    runner: CliRunner, tmp_path: Path
) -> None:
    """Empty dir (no PIONEER/) → exit 2 and no Engine Library/ on a fresh drive.

    WHY: a fatal must not leave a half-created library that Engine might open
    as "valid but empty". Fresh-stick cleanup is half of U7 criterion 9.
    """
    drive = tmp_path / "empty_stick"
    drive.mkdir()
    assert not (drive / "PIONEER").exists()

    result = runner.invoke(main, ["convert", str(drive)])
    assert result.exit_code == 2, result.output + (result.stderr or "")
    assert not (drive / "Engine Library").exists(), (
        "fresh fatal must not leave Engine Library/ behind"
    )


# ---------------------------------------------------------------------------
# Exit 2 — unreadable / unsupported schema (G1a header insanity)
# ---------------------------------------------------------------------------


def test_unreadable_schema_exits_two(runner: CliRunner, tmp_path: Path) -> None:
    """G1a header insanity on export.pdb → UnsupportedFormatError → exit 2.

    Uses a real mini stick layout so scan + pdb both run; only the header is
    byte-patched (no committed corrupt binary).
    """
    drive = tmp_path / "stick"
    drive.mkdir()
    _write_mini_export_pdb(drive, bad_len_page=True)
    (drive / "Contents" / "Artist").mkdir(parents=True)
    (drive / "Contents" / "Artist" / "track.mp3").write_bytes(b"audio")

    result = runner.invoke(main, ["convert", str(drive), "--no-artwork"])
    assert result.exit_code == 2, result.output + (result.stderr or "")
    combined = (result.output + (result.stderr or "")).lower()
    assert "unsupported" in combined or "len_page" in combined


def test_corrupt_artists_table_exits_two(runner: CliRunner, tmp_path: Path) -> None:
    """G1c present-but-unparseable optional-consumed table → exit 2 via CLI.

    Complements the unit parse test: the operator-visible exit code must be 2,
    not a silent convert with empty artist names.
    """
    drive = tmp_path / "stick"
    drive.mkdir()
    _write_mini_export_pdb(drive, include_artists=True, corrupt_artists=True)
    (drive / "Contents" / "Artist").mkdir(parents=True)
    (drive / "Contents" / "Artist" / "track.mp3").write_bytes(b"audio")

    result = runner.invoke(main, ["convert", str(drive), "--no-artwork"])
    assert result.exit_code == 2, result.output + (result.stderr or "")
    # Must not have published a library from a refused parse.
    m_db = drive / "Engine Library" / "Database2" / "m.db"
    assert not m_db.is_file()


# ---------------------------------------------------------------------------
# Exit 2 — pre-existing library survives fatals byte-identical
# ---------------------------------------------------------------------------


def test_fatal_preserves_preexisting_mdb_byte_identical(
    runner: CliRunner, tmp_path: Path
) -> None:
    """Fatal on a stick that already has Engine Library/ keeps prior m.db.

    WHY: the common workflow is re-export-then-re-convert. A fatal mid-run
    must leave the previous conversion usable — prior m.db byte-identical,
    no m.db.tmp residue (U7 / criterion 9 pre-existing branch).
    """
    drive = tmp_path / "stick"
    drive.mkdir()
    prior_payload = b"PRIOR-MDB-MUST-SURVIVE-BYTE-IDENTICAL"
    m_db = _seed_prior_mdb(drive, payload=prior_payload)
    prior_hash = m_db.read_bytes()

    # Fatal before any write: PIONEER present layout fails G1a on export.pdb.
    _write_mini_export_pdb(drive, bad_len_page=True)

    result = runner.invoke(main, ["convert", str(drive), "--no-artwork"])
    assert result.exit_code == 2, result.output + (result.stderr or "")

    assert m_db.is_file()
    assert m_db.read_bytes() == prior_hash
    assert m_db.read_bytes() == prior_payload
    assert not (m_db.parent / "m.db.tmp").exists(), "no m.db.tmp residue after fatal"
    # Sibling left alone
    assert (m_db.parent / "hm.db").read_bytes() == b"hm-sibling"


def test_fatal_unsupported_on_preexisting_via_corrupt_artists(
    runner: CliRunner, tmp_path: Path
) -> None:
    """G1c artists corruption on a pre-existing library: exit 2, m.db untouched."""
    drive = tmp_path / "stick"
    drive.mkdir()
    prior = b"ENGINE-AUTHORED-MDB-V2"
    m_db = _seed_prior_mdb(drive, payload=prior)

    _write_mini_export_pdb(drive, include_artists=True, corrupt_artists=True)
    (drive / "Contents" / "Artist").mkdir(parents=True)
    (drive / "Contents" / "Artist" / "track.mp3").write_bytes(b"x")

    result = runner.invoke(main, ["convert", str(drive), "--no-artwork"])
    assert result.exit_code == 2, result.output + (result.stderr or "")
    assert m_db.read_bytes() == prior
    assert not (m_db.parent / "m.db.tmp").exists()


# ---------------------------------------------------------------------------
# Parser-level pin: corrupt artists is UnsupportedFormatError (not crash)
# ---------------------------------------------------------------------------


def test_corrupt_artists_raises_unsupported_not_traceback(tmp_path: Path) -> None:
    """Direct parse path: typed UnsupportedFormatError, no bare Exception leak."""
    from rb2engine.reader.pdb import parse_export_pdb

    drive = tmp_path / "stick"
    drive.mkdir()
    pdb = _write_mini_export_pdb(drive, include_artists=True, corrupt_artists=True)

    with pytest.raises(UnsupportedFormatError, match="artists"):
        parse_export_pdb(pdb, drive)
