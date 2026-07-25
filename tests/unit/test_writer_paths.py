"""Tests for writer/paths.py — PM-1 Track.path strategy.

Why these cases exist:
- engine-lib is the DEFAULT because Engine's desktop library stores
  ../-escaping relative paths rooted at Engine Library/. Audio on this stick
  lives at <drive>/Contents/... while m.db is at
  <drive>/Engine Library/Database2/m.db, so stored paths become
  ../Contents/... . That geometry is the project's top open risk (PM-1):
  Engine's own exporter copies into Engine Library/Music/ instead, so this
  configuration is one Engine's tooling never produces.
- absolute must never be the default: a stick with absolute paths breaks the
  moment it mounts at a different path, and no oracle in this project would
  catch that regression.
- Forward slashes always: Engine and the sqlite file are cross-platform; a
  Windows backslash in Track.path is a latent missing-file bug on players.
- Paths outside drive_root must raise rather than emit a silently wrong
  relative string that would show as missing in Engine.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from rb2engine.writer.paths import engine_track_path


def _geometry(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Real stick layout: audio under Contents/, DB under Engine Library/."""
    drive_root = tmp_path / "USB DISK"
    engine_library_dir = drive_root / "Engine Library"
    music_abs = drive_root / "Contents" / "A" / "b.mp3"
    music_abs.parent.mkdir(parents=True)
    music_abs.write_bytes(b"fake-audio")
    engine_library_dir.mkdir(parents=True)
    return drive_root, engine_library_dir, music_abs


# ---------------------------------------------------------------------------
# Default and three bases
# ---------------------------------------------------------------------------


def test_default_base_is_engine_lib_not_absolute() -> None:
    """absolute must be unreachable as the default — C-MAJOR-2 / PM-1.

    If the default ever becomes absolute, every converted stick breaks when
    remounted under a different path, and no field test would catch it until
    the user is on stage.
    """
    sig = inspect.signature(engine_track_path)
    default = sig.parameters["base"].default
    assert default == "engine-lib"
    assert default != "absolute"


def test_engine_lib_base_escapes_to_contents(tmp_path: Path) -> None:
    """Default geometry: path relative to Engine Library/ reaches Contents/.

    m.db lives at <drive>/Engine Library/Database2/m.db; Engine resolves
    Track.path from the Engine Library/ folder (not Database2/). Audio at
    <drive>/Contents/A/b.mp3 must therefore store as ../Contents/A/b.mp3.
    """
    drive_root, engine_library_dir, music_abs = _geometry(tmp_path)

    got = engine_track_path(
        music_abs,
        drive_root=drive_root,
        engine_library_dir=engine_library_dir,
    )
    assert got == "../Contents/A/b.mp3"

    # Explicit base matches the default.
    explicit = engine_track_path(
        music_abs,
        drive_root=drive_root,
        engine_library_dir=engine_library_dir,
        base="engine-lib",
    )
    assert explicit == got


def test_drive_root_base_is_contents_relative(tmp_path: Path) -> None:
    """drive-root stores paths relative to the volume root (no ../ escape).

    Fallback candidate if M1 shows Engine rejects engine-lib on removable media.
    """
    drive_root, engine_library_dir, music_abs = _geometry(tmp_path)

    got = engine_track_path(
        music_abs,
        drive_root=drive_root,
        engine_library_dir=engine_library_dir,
        base="drive-root",
    )
    assert got == "Contents/A/b.mp3"


def test_absolute_base_is_diagnostic_only(tmp_path: Path) -> None:
    """absolute returns a full path string — only when explicitly requested."""
    drive_root, engine_library_dir, music_abs = _geometry(tmp_path)

    got = engine_track_path(
        music_abs,
        drive_root=drive_root,
        engine_library_dir=engine_library_dir,
        base="absolute",
    )
    assert got == music_abs.resolve().as_posix()
    assert got.startswith("/")
    assert "Contents/A/b.mp3" in got


# ---------------------------------------------------------------------------
# Cross-platform and safety
# ---------------------------------------------------------------------------


def test_forward_slashes_on_every_platform(tmp_path: Path) -> None:
    """Track.path must use / even when Path would use \\ on Windows.

    Engine and FAT32 players do not want host-native separators in sqlite.
    """
    drive_root, engine_library_dir, music_abs = _geometry(tmp_path)

    for base in ("engine-lib", "drive-root", "absolute"):
        got = engine_track_path(
            music_abs,
            drive_root=drive_root,
            engine_library_dir=engine_library_dir,
            base=base,
        )
        assert "\\" not in got
        assert "/" in got


def test_nested_contents_path_preserves_structure(tmp_path: Path) -> None:
    """Deep artist/album trees must not be collapsed or re-rooted incorrectly."""
    drive_root = tmp_path / "stick"
    engine_library_dir = drive_root / "Engine Library"
    music_abs = (
        drive_root
        / "Contents"
        / "Artist"
        / "Album"
        / "Track Name.mp3"
    )
    music_abs.parent.mkdir(parents=True)
    music_abs.write_bytes(b"x")
    engine_library_dir.mkdir(parents=True)

    assert (
        engine_track_path(
            music_abs,
            drive_root=drive_root,
            engine_library_dir=engine_library_dir,
            base="engine-lib",
        )
        == "../Contents/Artist/Album/Track Name.mp3"
    )
    assert (
        engine_track_path(
            music_abs,
            drive_root=drive_root,
            engine_library_dir=engine_library_dir,
            base="drive-root",
        )
        == "Contents/Artist/Album/Track Name.mp3"
    )


def test_path_outside_drive_root_raises(tmp_path: Path) -> None:
    """Outside the stick must not become a silently wrong relative path.

    Emitting ../../elsewhere/file.mp3 or an absolute host path without an
    explicit absolute base would produce missing-file ghosts in Engine.
    """
    drive_root = tmp_path / "stick"
    engine_library_dir = drive_root / "Engine Library"
    drive_root.mkdir()
    engine_library_dir.mkdir()

    outside = tmp_path / "elsewhere" / "track.mp3"
    outside.parent.mkdir()
    outside.write_bytes(b"x")

    for base in ("engine-lib", "drive-root", "absolute"):
        with pytest.raises((ValueError, OSError)):
            engine_track_path(
                outside,
                drive_root=drive_root,
                engine_library_dir=engine_library_dir,
                base=base,
            )


def test_unknown_base_raises(tmp_path: Path) -> None:
    """Typos in --path-base must fail loud, not fall through to a guess."""
    drive_root, engine_library_dir, music_abs = _geometry(tmp_path)

    with pytest.raises(ValueError):
        engine_track_path(
            music_abs,
            drive_root=drive_root,
            engine_library_dir=engine_library_dir,
            base="music-folder",
        )
