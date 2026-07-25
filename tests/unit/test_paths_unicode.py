"""Unicode normalization in filename matching.

WHY THIS EXISTS
---------------
On a real 3,666-track stick, 46 tracks were reported "skipped: resolved_path is
None" while every one of the files was present and playable. The common factor
was an accented character somewhere in the path — São, Tiësto, RÜFÜS, Sinéad,
Chlär, Sébastien.

rekordbox writes the path into export.pdb composed (NFC: ``ã`` = U+00E3). The
same name read back off the stick with ``iterdir()`` arrives decomposed
(NFD: ``a`` + U+0303). Identical to a human and to the filesystem, different as
``str``, so the resolver's ``entry.name.lower() == segment.lower()`` missed.

The filesystem is what hid this: macOS resolves an NFC path against NFD on-disk
names inside the syscall, so ``Path(...).exists()`` on the very same path
returned True. Only our own iterdir-and-compare walk could see the mismatch —
which is why no ``exists()``-based check would ever have caught it, and why
these tests compare the two spellings explicitly rather than trusting open().

The assertion in every test below is the invariant, not the mechanism: **a name
spelled in either normalization form must find the one file on disk.**
"""

from __future__ import annotations

import unicodedata
from pathlib import Path

import pytest

from rb2engine.reader.paths import resolve_track_path
from rb2engine.reader.scan import scan_drive

# The exact directory name that failed on the real stick.
_NFC = unicodedata.normalize("NFC", "S\u00e3o Paulo - Single")
_NFD = unicodedata.normalize("NFD", "S\u00e3o Paulo - Single")


def test_the_two_spellings_really_are_different_strings() -> None:
    """Guard the premise: if these were equal, every test below is vacuous."""
    assert _NFC != _NFD
    assert len(_NFD) > len(_NFC)  # decomposed form carries a combining mark


def test_fold_name_equates_the_two_normalization_forms() -> None:
    from rb2engine.reader.paths import fold_name

    assert fold_name(_NFC) == fold_name(_NFD)


def test_fold_name_still_distinguishes_genuinely_different_names() -> None:
    """Normalizing must not collapse names that denote different files.

    Without this, a fold that "fixes" matching by being lossy would pass the
    test above while silently pointing two tracks at one file.
    """
    from rb2engine.reader.paths import fold_name

    assert fold_name("São Paulo") != fold_name("Sao Paulo")
    assert fold_name("Track A") != fold_name("Track B")


def test_fold_name_remains_case_insensitive() -> None:
    """The pre-existing FAT32 case-insensitivity must survive the change."""
    from rb2engine.reader.paths import fold_name

    assert fold_name("PIONEER") == fold_name("pioneer")


def _make_stick(root: Path, dir_name: str) -> Path:
    """Build a minimal stick whose Contents/ holds one album directory."""
    album = root / "Contents" / dir_name
    album.mkdir(parents=True)
    audio = album / "01 Track.m4a"
    audio.write_bytes(b"audio")
    return audio


@pytest.mark.parametrize("on_disk", [_NFC, _NFD], ids=["disk-nfc", "disk-nfd"])
@pytest.mark.parametrize("in_pdb", [_NFC, _NFD], ids=["pdb-nfc", "pdb-nfd"])
def test_resolves_across_every_normalization_pairing(
    tmp_path: Path, on_disk: str, in_pdb: str
) -> None:
    """All four spellings of the same album name must find the same file.

    Parameterised over both axes deliberately: which form ends up on disk is
    decided by the filesystem that created it (APFS stores what it is given,
    HFS+ normalizes to NFD, exFAT written by a Mac carries NFD), and which form
    is in the pdb is decided by rekordbox. Neither is under our control, so the
    resolver must not depend on either.
    """
    audio = _make_stick(tmp_path, on_disk)
    # Confirm the filesystem preserved the spelling we asked for; if it silently
    # normalized, this pairing cannot exercise what it claims to.
    stored = next((tmp_path / "Contents").iterdir()).name
    if stored != on_disk:
        pytest.skip(f"filesystem normalized {on_disk!r} to {stored!r}")

    raw = f"D:/Contents/{in_pdb}/01 Track.m4a"
    resolved = resolve_track_path(raw, tmp_path)

    assert resolved is not None, f"pdb spelling {in_pdb!r} did not find {stored!r}"
    # Same file, whatever spelling the resolver returned.
    assert resolved.read_bytes() == b"audio"
    assert resolved.samefile(audio)


def test_returned_path_uses_the_on_disk_spelling(tmp_path: Path) -> None:
    """Callers open this path and hash it — it must be the name disk holds.

    Handing back the pdb's spelling would work on a normalization-insensitive
    filesystem and fail on a byte-exact one (ext4, and NTFS), turning this into
    a bug that only reproduces on someone else's machine.
    """
    _make_stick(tmp_path, _NFD)
    stored = next((tmp_path / "Contents").iterdir()).name
    if stored != _NFD:
        pytest.skip("filesystem normalized the NFD directory name")

    resolved = resolve_track_path(f"D:/Contents/{_NFC}/01 Track.m4a", tmp_path)

    assert resolved is not None
    assert resolved.parent.name == _NFD


def test_missing_file_still_returns_none(tmp_path: Path) -> None:
    """Normalization must not turn a genuine miss into a false hit.

    The fix widens what counts as a match; this pins that it did not widen it
    to "anything under Contents/".
    """
    _make_stick(tmp_path, _NFC)
    assert resolve_track_path("D:/Contents/Other Album/01 Track.m4a", tmp_path) is None
    assert resolve_track_path(f"D:/Contents/{_NFC}/02 Nope.m4a", tmp_path) is None


def test_scan_drive_finds_a_decomposed_contents_directory(tmp_path: Path) -> None:
    """scan_drive carries its own matcher, so it needs its own regression test.

    ``Contents`` is ASCII, but scan_drive's ``_find_child`` is the same
    algorithm; a stick whose layout directories were created on a Mac can carry
    decomposed names too. Fixing only reader/paths.py would leave this half
    broken.
    """
    (tmp_path / "PIONEER" / "rekordbox").mkdir(parents=True)
    (tmp_path / "PIONEER" / "rekordbox" / "export.pdb").write_bytes(b"stub")
    (tmp_path / "PIONEER" / "USBANLZ").mkdir()
    (tmp_path / "Contents").mkdir()

    layout = scan_drive(tmp_path)

    assert layout.export_pdb.is_file()
    assert layout.contents_dir is not None
