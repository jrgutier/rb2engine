"""engine_track_path(): PM-1 path strategy; absolute is diagnostic-only.

Default base is engine-lib: Track.path is relative to the Engine Library/
folder so audio at <drive>/Contents/... becomes ../Contents/.... Engine's
desktop library stores ../-escaping paths this way; Engine's exporter instead
copies into Engine Library/Music/ — so this configuration is one Engine's own
tooling never produces (PM-1 open risk).

absolute is reachable only when explicitly requested and can never be default.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

# Compiled-in default for --path-base. Must never be "absolute" (C-MAJOR-2).
DEFAULT_PATH_BASE = "engine-lib"

_VALID_BASES = frozenset({"engine-lib", "drive-root", "absolute"})


def engine_track_path(
    music_abs: Path,
    *,
    drive_root: Path,
    engine_library_dir: Path,
    base: str = "engine-lib",
) -> str:
    """Return the string stored in Track.path (always forward-slash).

    base="engine-lib" (DEFAULT): relative to the Engine Library/ folder, so
      audio at <drive>/Contents/A/b.mp3 becomes "../Contents/A/b.mp3".
    base="drive-root": relative to the drive root ("Contents/A/b.mp3").
    base="absolute": DIAGNOSTIC ONLY. Must never be the default: an absolute
      path breaks the moment the stick mounts anywhere else, and no oracle in
      this project would catch it.

    Raises ValueError if *base* is unknown or *music_abs* is outside
    *drive_root* (refusing to emit a silently wrong relative path).
    """
    if base not in _VALID_BASES:
        raise ValueError(
            f"Unknown path base {base!r}; expected one of "
            f"{sorted(_VALID_BASES)}. "
            f"'absolute' is diagnostic-only and must never be the default."
        )

    music = Path(music_abs).resolve()
    root = Path(drive_root).resolve()

    try:
        music.relative_to(root)
    except ValueError as exc:
        raise ValueError(
            f"Track path {music} is outside drive root {root}. "
            "Refusing to emit a relative path that Engine would mark missing."
        ) from exc

    if base == "absolute":
        return music.as_posix()

    if base == "drive-root":
        rel = music.relative_to(root)
        return PurePosixPath(*rel.parts).as_posix()

    # engine-lib: relative to Engine Library/ (not Database2/).
    lib = Path(engine_library_dir).resolve()
    rel = music.relative_to(root)
    # music is under drive_root; express it relative to engine_library_dir.
    # Prefer Path.relative_to when music is under lib (rare: Music/ copy mode);
    # otherwise walk from lib up to root then down into Contents/.
    try:
        under_lib = music.relative_to(lib)
        return PurePosixPath(*under_lib.parts).as_posix()
    except ValueError:
        pass

    # lib should be under drive_root for the real stick geometry.
    try:
        lib_from_root = lib.relative_to(root)
    except ValueError as exc:
        raise ValueError(
            f"engine_library_dir {lib} is outside drive_root {root}."
        ) from exc

    ups = len(lib_from_root.parts)
    parts = ("..",) * ups + tuple(rel.parts)
    return PurePosixPath(*parts).as_posix() if parts else "."
