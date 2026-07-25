"""Unit tests for rekordbox path re-rooting (reader/paths.py).

Why these cases exist: export.pdb stores drive-letter-prefixed paths from the
exporting machine. That letter is meaningless on the current host. DJ sticks
are FAT32 (case-insensitive) but macOS/Linux hosts are often case-sensitive,
so a stored ``Contents/`` may live on disk as ``contents/``. A missed match
means a silent library hole on a real stick (~3,665 tracks), not a unit-test
nit. None means "skip and itemize in the report" — never crash, never guess.
"""

from __future__ import annotations

from pathlib import Path

from rb2engine.reader.paths import resolve_track_path


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fake-audio")
    return path


# ---------------------------------------------------------------------------
# Happy paths: re-root under the real mount, discard the stored drive letter
# ---------------------------------------------------------------------------


def test_macos_volume_form_resolves_under_mount_point(tmp_path: Path) -> None:
    """macOS mounts sticks at /Volumes/<name>; pdb still says D:/Contents/...

    If we trusted the drive letter we would look under D: and miss every track.
    """
    drive_root = tmp_path / "Volumes" / "USB DISK"
    real = _touch(drive_root / "Contents" / "A" / "B.mp3")

    result = resolve_track_path("D:/Contents/A/B.mp3", drive_root)

    assert result == real
    assert result is not None and result.exists()


def test_windows_backslash_stored_path_resolves(tmp_path: Path) -> None:
    """Some exports / tooling emit backslash-separated paths with a drive letter.

    Separators must not leave us unable to find Contents/ on a POSIX host.
    """
    drive_root = tmp_path / "E_drive"
    real = _touch(drive_root / "Contents" / "Artist" / "Track.mp3")

    result = resolve_track_path(r"E:\Contents\Artist\Track.mp3", drive_root)

    assert result == real


def test_linux_mount_point_resolves(tmp_path: Path) -> None:
    """Linux typically mounts removable media under /media/<user>/<label>.

    Same re-root logic; only the host mount string changes.
    """
    drive_root = tmp_path / "media" / "user" / "USB"
    real = _touch(drive_root / "Contents" / "Binum" / "UnknownAlbum" / "Chapter.mp3")

    result = resolve_track_path(
        "D:/Contents/Binum/UnknownAlbum/Chapter.mp3",
        drive_root,
    )

    assert result == real


def test_stored_drive_letter_is_ignored_entirely(tmp_path: Path) -> None:
    """The letter is whatever machine exported the stick — never the current host.

    A path claiming Z: must still resolve under the given drive_root, or every
    track fails when the stick is read on a different OS/machine.
    """
    drive_root = tmp_path / "actual_mount"
    real = _touch(drive_root / "Contents" / "X" / "y.wav")

    result = resolve_track_path("Z:/Contents/X/y.wav", drive_root)

    assert result == real
    assert "Z" not in result.parts


# ---------------------------------------------------------------------------
# FAT32 case-insensitivity vs case-sensitive host filesystems
# ---------------------------------------------------------------------------


def test_case_variant_contents_resolves_to_on_disk_casing(tmp_path: Path) -> None:
    """FAT32 is case-insensitive; macOS (case-sensitive APFS) and Linux are not.

    A real stick can expose ``contents/`` while pdb still says ``Contents/``.
    Matching must be case-insensitive, but the returned Path must use the real
    on-disk casing so open() succeeds on a case-sensitive host.
    """
    drive_root = tmp_path / "stick"
    # On-disk: lowercase "contents", mixed artist casing
    real = _touch(drive_root / "contents" / "SomeArtist" / "track.mp3")

    result = resolve_track_path("D:/Contents/SomeArtist/track.mp3", drive_root)

    assert result is not None
    assert result.exists()
    assert result == real
    # Must not invent Title-case Contents if the disk has lowercase
    assert "contents" in result.parts
    assert "Contents" not in result.parts


def test_case_variant_deeper_segments_resolve_to_on_disk_casing(
    tmp_path: Path,
) -> None:
    """Artist/album folders on FAT32 may also disagree in case with pdb strings.

    Re-rooting only the Contents segment is not enough — every segment must
    be matched case-insensitively and remapped to the real name.
    """
    drive_root = tmp_path / "stick"
    real = _touch(drive_root / "Contents" / "binum" / "unknownalbum" / "song.mp3")

    result = resolve_track_path(
        "D:/Contents/Binum/UnknownAlbum/song.mp3",
        drive_root,
    )

    assert result == real
    assert result is not None and result.exists()


# ---------------------------------------------------------------------------
# Failure modes: None = skip + report (never exception, never guess)
# ---------------------------------------------------------------------------


def test_no_contents_segment_returns_none(tmp_path: Path) -> None:
    """Paths without a Contents/ directory segment cannot be re-rooted safely.

    Guessing (e.g. stripping only the drive letter) would open the wrong file
    or invent a location. Caller must skip and itemize in the report.
    """
    drive_root = tmp_path / "stick"
    _touch(drive_root / "Music" / "A" / "B.mp3")

    result = resolve_track_path("D:/Music/A/B.mp3", drive_root)

    assert result is None


def test_missing_file_returns_none(tmp_path: Path) -> None:
    """Well-formed pdb path whose audio is missing on the stick (deleted, moved).

    Must not raise; the convert run continues and the track is listed as skipped.
    """
    drive_root = tmp_path / "stick"
    (drive_root / "Contents" / "A").mkdir(parents=True)

    result = resolve_track_path("D:/Contents/A/Missing.mp3", drive_root)

    assert result is None


def test_missing_contents_directory_returns_none(tmp_path: Path) -> None:
    """Stick has no Contents/ at all (wrong root, empty drive, wrong mount)."""
    drive_root = tmp_path / "empty_mount"
    drive_root.mkdir()

    result = resolve_track_path("D:/Contents/A/B.mp3", drive_root)

    assert result is None


# ---------------------------------------------------------------------------
# Non-ASCII + false-positive Contents substring
# ---------------------------------------------------------------------------


def test_non_ascii_path_segments_resolve(tmp_path: Path) -> None:
    """Real libraries include non-ASCII artist/title folders (accents, CJK, etc.).

    Path handling must not mangle UTF-8 segments when re-rooting.
    """
    drive_root = tmp_path / "stick"
    real = _touch(
        drive_root / "Contents" / "Björk" / "Homogenic" / "Jóga.mp3",
    )

    result = resolve_track_path(
        "D:/Contents/Björk/Homogenic/Jóga.mp3",
        drive_root,
    )

    assert result == real
    assert result is not None and result.exists()


def test_contents_substring_in_filename_does_not_false_match(tmp_path: Path) -> None:
    """``Contents`` must match only as a full path segment, not a substring.

    A filename or folder like ``MyContentsMix`` must not be treated as the
    Contents/ re-root point, or we re-root at the wrong depth and miss files.
    """
    drive_root = tmp_path / "stick"
    # File whose name contains the substring "Contents" but is not the dir
    _touch(drive_root / "Music" / "MyContentsMix.mp3")
    # Also a folder that contains the substring
    _touch(drive_root / "ContentsExtra" / "track.mp3")

    assert resolve_track_path("D:/Music/MyContentsMix.mp3", drive_root) is None
    assert resolve_track_path("D:/ContentsExtra/track.mp3", drive_root) is None


def test_contents_as_filename_not_directory_segment_does_not_match(
    tmp_path: Path,
) -> None:
    """A bare file named like Contents.mp3 is not the Contents/ music root."""
    drive_root = tmp_path / "stick"
    _touch(drive_root / "exports" / "Contents.mp3")

    result = resolve_track_path("D:/exports/Contents.mp3", drive_root)

    # Segment is "Contents.mp3", not "Contents" — must not re-root here
    assert result is None


def test_returns_concrete_filesystem_path(tmp_path: Path) -> None:
    """Filesystem results are pathlib.Path (PosixPath/WindowsPath); not Pure*.

    Stored pdb paths are parsed as PurePosixPath segments only. The return value
    must support exists()/open() on the host — PurePosixPath would be wrong.
    """
    drive_root = tmp_path / "stick"
    _touch(drive_root / "Contents" / "t.mp3")

    result = resolve_track_path("D:/Contents/t.mp3", drive_root)

    assert result is not None
    assert isinstance(result, Path)
    assert result.exists()
    assert result.is_file()
