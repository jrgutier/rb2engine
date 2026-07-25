"""Unit tests for rekordbox USB stick layout discovery (reader/scan.py).

Why these cases exist: a real stick is FAT32 with case-insensitive names, may
carry exportExt.pdb (MyTags, out of scope), may already have Engine Library/
from a prior Engine export (full-rebuild path), and associates ANLZ files via
the pdb path field — not directory enumeration. Missing export.pdb or USBANLZ
must abort with exit-2 style UnsupportedFormatError; missing .EXT is normal.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from rb2engine.errors import UnsupportedFormatError
from rb2engine.reader.scan import AnlzPaths, StickLayout, resolve_anlz_paths, scan_drive

REAL_STICK = Path("/Volumes/USB DISK")


def _make_minimal_stick(
    root: Path,
    *,
    pioneer: str = "PIONEER",
    contents: str | None = "Contents",
    export_ext: bool = False,
    engine_library: bool = False,
    usbanlz: bool = True,
    export_pdb: bool = True,
    anlz_ext: bool = True,
    anlz_2ex: bool = False,
) -> Path:
    """Build a synthetic stick under root with configurable casing/optional parts."""
    root.mkdir(parents=True, exist_ok=True)
    if export_pdb or usbanlz or export_ext:
        pioneer_dir = root / pioneer
        pioneer_dir.mkdir(parents=True, exist_ok=True)
        if export_pdb or export_ext:
            rb = pioneer_dir / "rekordbox"
            rb.mkdir(parents=True, exist_ok=True)
            if export_pdb:
                (rb / "export.pdb").write_bytes(b"fake-pdb")
            if export_ext:
                (rb / "exportExt.pdb").write_bytes(b"fake-ext-pdb")
        if usbanlz:
            anlz_dir = pioneer_dir / "USBANLZ" / "P02B" / "00003DA7"
            anlz_dir.mkdir(parents=True, exist_ok=True)
            (anlz_dir / "ANLZ0000.DAT").write_bytes(b"dat")
            if anlz_ext:
                (anlz_dir / "ANLZ0000.EXT").write_bytes(b"ext")
            if anlz_2ex:
                (anlz_dir / "ANLZ0000.2EX").write_bytes(b"2ex")
    if contents is not None:
        c = root / contents
        c.mkdir(parents=True, exist_ok=True)
        (c / "Artist" / "Album").mkdir(parents=True, exist_ok=True)
        (c / "Artist" / "Album" / "track.m4a").write_bytes(b"audio")
    if engine_library:
        eng = root / "Engine Library"
        (eng / "Database2").mkdir(parents=True, exist_ok=True)
        (eng / "Music").mkdir(parents=True, exist_ok=True)
        (eng / "Artwork").mkdir(parents=True, exist_ok=True)
        (eng / "Database2" / "m.db").write_bytes(b"fake-db")
    return root


# ---------------------------------------------------------------------------
# Full valid layout
# ---------------------------------------------------------------------------


def test_full_valid_layout_discovers_all_required_paths(tmp_path: Path) -> None:
    """A complete stick must yield export.pdb, USBANLZ/, and optional Contents/.

    Convert starts from this layout; wrong paths mean every track is skipped.
    """
    stick = _make_minimal_stick(tmp_path / "stick", export_ext=False)

    layout = scan_drive(stick)

    assert isinstance(layout, StickLayout)
    assert layout.drive_root == stick
    assert layout.export_pdb.is_file()
    assert layout.export_pdb.name == "export.pdb"
    assert layout.usbanlz_dir.is_dir()
    assert layout.usbanlz_dir.name == "USBANLZ"
    assert layout.contents_dir is not None
    assert layout.contents_dir.is_dir()
    assert layout.export_ext_pdb is None
    assert layout.exportext_present is False
    assert layout.engine_library_dir is None
    assert layout.is_full_rebuild is False


# ---------------------------------------------------------------------------
# Fatal missing pieces → UnsupportedFormatError (exit 2)
# ---------------------------------------------------------------------------


def test_missing_export_pdb_raises_unsupported_format(tmp_path: Path) -> None:
    """Without export.pdb there is no track metadata — fail loud, name the path.

    Exit 2 (UnsupportedFormatError) so the CLI does not pretend to convert.
    """
    stick = _make_minimal_stick(tmp_path / "stick", export_pdb=False)

    with pytest.raises(UnsupportedFormatError, match=r"export\.pdb") as exc_info:
        scan_drive(stick)

    msg = str(exc_info.value)
    assert "PIONEER" in msg or "pioneer" in msg.lower() or "rekordbox" in msg.lower()


def test_missing_usbanlz_raises_unsupported_format(tmp_path: Path) -> None:
    """Without USBANLZ/ there are no beatgrids/cues — fail loud with expected path.

    Audio alone is not enough; performance data lives under USBANLZ.
    """
    stick = _make_minimal_stick(tmp_path / "stick", usbanlz=False)

    with pytest.raises(UnsupportedFormatError, match=r"USBANLZ") as exc_info:
        scan_drive(stick)

    assert "expected" in str(exc_info.value).lower() or "PIONEER" in str(exc_info.value)


# ---------------------------------------------------------------------------
# G1d — exportExt.pdb present (MyTags out of scope)
# ---------------------------------------------------------------------------


def test_exportext_present_sets_g1d_counter_and_warns_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """exportExt.pdb is normal on modern sticks; warn once, never fatal.

    G1d fires on this user's first real run (73 KB exportExt.pdb). The message
    must be reassuring and once-per-scan so it is not repeated per track.
    """
    stick = _make_minimal_stick(tmp_path / "stick", export_ext=True)

    with caplog.at_level(logging.INFO):
        layout = scan_drive(stick)

    assert layout.exportext_present is True
    assert layout.export_ext_pdb is not None
    assert layout.export_ext_pdb.is_file()
    assert layout.export_ext_pdb.name.lower() == "exportext.pdb"

    # Exactly one G1d-related line (friendly MyTags note).
    g1d = [
        r
        for r in caplog.records
        if "mytags" in r.getMessage().lower()
        or "exportext" in r.getMessage().lower()
    ]
    assert len(g1d) == 1, f"expected one G1d line, got: {[r.getMessage() for r in g1d]}"
    msg = g1d[0].getMessage().lower()
    assert "not" in msg  # not converted / not an error


def test_exportext_absent_no_g1d_message(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Older sticks without exportExt must not emit a spurious MyTags warning."""
    stick = _make_minimal_stick(tmp_path / "stick", export_ext=False)

    with caplog.at_level(logging.INFO):
        layout = scan_drive(stick)

    assert layout.exportext_present is False
    g1d = [
        r
        for r in caplog.records
        if "mytags" in r.getMessage().lower()
        or "exportext" in r.getMessage().lower()
    ]
    assert g1d == []


# ---------------------------------------------------------------------------
# FAT32 case-insensitivity vs case-sensitive hosts
# ---------------------------------------------------------------------------


def test_lowercase_pioneer_and_contents_resolve_to_on_disk_casing(
    tmp_path: Path,
) -> None:
    """FAT32 may present pioneer/ and contents/; hosts are often case-sensitive.

    Returned Paths must use real on-disk names so open() works on APFS/Linux.
    """
    stick = _make_minimal_stick(
        tmp_path / "stick",
        pioneer="pioneer",
        contents="contents",
    )

    layout = scan_drive(stick)

    assert layout.export_pdb.exists()
    assert "pioneer" in layout.export_pdb.parts
    assert "PIONEER" not in layout.export_pdb.parts
    assert layout.usbanlz_dir.exists()
    assert layout.usbanlz_dir.name == "USBANLZ" or layout.usbanlz_dir.name == "usbanlz"
    assert layout.contents_dir is not None
    assert layout.contents_dir.name == "contents"
    assert "Contents" not in layout.contents_dir.parts


def test_contents_optional_when_audio_lives_elsewhere(tmp_path: Path) -> None:
    """Contents/ is optional — audio may live outside the rekordbox tree.

    Missing Contents must not abort scan; resolved_path handling is separate.
    """
    stick = _make_minimal_stick(tmp_path / "stick", contents=None)

    layout = scan_drive(stick)

    assert layout.export_pdb.is_file()
    assert layout.usbanlz_dir.is_dir()
    assert layout.contents_dir is None


# ---------------------------------------------------------------------------
# Pre-existing Engine Library/ → full-rebuild path
# ---------------------------------------------------------------------------


def test_existing_engine_library_flags_full_rebuild(tmp_path: Path) -> None:
    """Prior Engine export leaves Engine Library/; convert must full-rebuild m.db.

    Presence is informational for the writer (preserve hm.db etc.); scan only
    detects and flags it — never writes.
    """
    stick = _make_minimal_stick(tmp_path / "stick", engine_library=True)

    layout = scan_drive(stick)

    assert layout.engine_library_dir is not None
    assert layout.engine_library_dir.is_dir()
    assert layout.engine_library_dir.name == "Engine Library"
    assert layout.is_full_rebuild is True


def test_no_engine_library_is_clean_create_path(tmp_path: Path) -> None:
    """No Engine Library/ means clean-create, not rebuild."""
    stick = _make_minimal_stick(tmp_path / "stick", engine_library=False)

    layout = scan_drive(stick)

    assert layout.engine_library_dir is None
    assert layout.is_full_rebuild is False


# ---------------------------------------------------------------------------
# ANLZ path resolution (.DAT required target; .EXT/.2EX optional siblings)
# ---------------------------------------------------------------------------


def test_resolve_anlz_paths_with_ext_present(tmp_path: Path) -> None:
    """pdb ANLZ path points at .DAT; sibling .EXT holds PCO2 when present.

    Association is by the stored path, never by walking USBANLZ.
    """
    stick = _make_minimal_stick(tmp_path / "stick", anlz_ext=True, anlz_2ex=True)
    pdb_path = "/PIONEER/USBANLZ/P02B/00003DA7/ANLZ0000.DAT"

    paths = resolve_anlz_paths(stick, pdb_path)

    assert isinstance(paths, AnlzPaths)
    assert paths.dat is not None and paths.dat.is_file()
    assert paths.dat.name == "ANLZ0000.DAT"
    assert paths.ext is not None and paths.ext.is_file()
    assert paths.ext.suffix.upper() == ".EXT"
    assert paths.two_ex is not None and paths.two_ex.is_file()
    assert paths.two_ex.name.endswith("2EX") or paths.two_ex.suffix.upper() in {
        ".2EX",
        "2EX",
    }


def test_resolve_anlz_paths_missing_ext_is_not_error(tmp_path: Path) -> None:
    """Missing .EXT is normal for some tracks — not an error, just None.

    Count mismatch (.DAT vs .EXT) is real-world noise on the user's stick.
    """
    stick = _make_minimal_stick(tmp_path / "stick", anlz_ext=False, anlz_2ex=False)
    pdb_path = "/PIONEER/USBANLZ/P02B/00003DA7/ANLZ0000.DAT"

    paths = resolve_anlz_paths(stick, pdb_path)

    assert paths.dat is not None and paths.dat.is_file()
    assert paths.ext is None
    assert paths.two_ex is None


def test_resolve_anlz_paths_case_insensitive_pioneer(tmp_path: Path) -> None:
    """Stored path says /PIONEER/...; on-disk may be pioneer/ — still openable."""
    stick = _make_minimal_stick(
        tmp_path / "stick",
        pioneer="pioneer",
        anlz_ext=True,
    )
    pdb_path = "/PIONEER/USBANLZ/P02B/00003DA7/ANLZ0000.DAT"

    paths = resolve_anlz_paths(stick, pdb_path)

    assert paths.dat is not None
    assert paths.dat.exists()
    assert "pioneer" in paths.dat.parts


def test_resolve_anlz_paths_missing_dat_returns_none_dat(tmp_path: Path) -> None:
    """Missing .DAT → dat=None for the track skip path; do not raise at resolve.

    Track-level skip/report is the caller's job; scan only locates files.
    """
    stick = _make_minimal_stick(tmp_path / "stick")
    pdb_path = "/PIONEER/USBANLZ/P099/DEADBEEF/ANLZ0000.DAT"

    paths = resolve_anlz_paths(stick, pdb_path)

    assert paths.dat is None
    assert paths.ext is None


# ---------------------------------------------------------------------------
# Real stick (Tier B) — skipped when unmounted
# ---------------------------------------------------------------------------


@pytest.mark.real_stick
@pytest.mark.skipif(
    not REAL_STICK.is_dir(),
    reason="real stick not mounted at /Volumes/USB DISK",
)
def test_real_stick_layout_and_g1d_once(caplog: pytest.LogCaptureFixture) -> None:
    """Tier B: real FAT32 stick has export.pdb, exportExt, USBANLZ, Engine Library.

    READ-ONLY — never write to the stick. G1d must fire exactly once.
    """
    with caplog.at_level(logging.INFO):
        layout = scan_drive(REAL_STICK)

    assert layout.export_pdb.is_file()
    assert layout.export_pdb.name == "export.pdb"
    assert layout.usbanlz_dir.is_dir()
    assert layout.export_ext_pdb is not None
    assert layout.export_ext_pdb.is_file()
    assert layout.exportext_present is True
    assert layout.contents_dir is not None
    assert layout.engine_library_dir is not None
    assert layout.is_full_rebuild is True

    g1d = [
        r
        for r in caplog.records
        if "mytags" in r.getMessage().lower()
        or "exportext" in r.getMessage().lower()
    ]
    assert len(g1d) == 1

    # Verified real form of ANLZ path from research appendix
    anlz = resolve_anlz_paths(
        REAL_STICK,
        "/PIONEER/USBANLZ/P02B/00003DA7/ANLZ0000.DAT",
    )
    assert anlz.dat is not None and anlz.dat.is_file()
    assert anlz.ext is not None and anlz.ext.is_file()
    assert anlz.two_ex is not None and anlz.two_ex.is_file()
