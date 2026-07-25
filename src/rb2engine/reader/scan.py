"""Locate PIONEER/, export.pdb, exportExt.pdb, USBANLZ/, Contents/ on a drive root.

Discovers the layout of a rekordbox-exported USB stick (or folder tree that
mirrors one). FAT32 is case-insensitive but macOS/Linux hosts often are not:
directory names may appear as ``PIONEER`` or ``pioneer``. Matching is
case-insensitive; returned paths use the **real on-disk casing** so open()
works on case-sensitive filesystems.

Gate G1d: if ``exportExt.pdb`` is present (MyTags), emit one friendly line
per scan and set ``exportext_present`` — not fatal, never per-track.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from rb2engine.errors import UnsupportedFormatError
from rb2engine.reader.paths import fold_name

logger = logging.getLogger(__name__)

# One-line G1d message — reassuring, once per run (this user's stick has
# exportExt.pdb, so they will see this on every conversion).
_G1D_MESSAGE = (
    "MyTags (exportExt.pdb) were found on this stick and are not converted. "
    "This is expected and not an error — only the main library export is used."
)


@dataclass(frozen=True, slots=True)
class StickLayout:
    """Validated layout of a rekordbox export under ``drive_root``."""

    drive_root: Path
    export_pdb: Path
    export_ext_pdb: Path | None
    usbanlz_dir: Path
    contents_dir: Path | None
    engine_library_dir: Path | None
    is_full_rebuild: bool
    """True when ``Engine Library/`` already exists (prior Engine export)."""
    exportext_present: bool
    """G1d report counter: True when exportExt.pdb was detected."""


@dataclass(frozen=True, slots=True)
class AnlzPaths:
    """Resolved ANLZ file trio for one track (siblings share the stem)."""

    dat: Path | None
    ext: Path | None
    two_ex: Path | None
    """``.2EX`` sibling when present (3-band waveform; not consumed by v1)."""


def scan_drive(drive_root: Path) -> StickLayout:
    """Locate and validate a rekordbox stick layout under ``drive_root``.

    Required: ``PIONEER/rekordbox/export.pdb``, ``PIONEER/USBANLZ/``.
    Optional: ``exportExt.pdb``, ``Contents/``, ``Engine Library/``.

    Raises
    ------
    UnsupportedFormatError
        Missing ``export.pdb`` or ``USBANLZ/`` (CLI exit 2). Message names
        what was expected and where.
    """
    root = Path(drive_root)
    if not root.is_dir():
        raise UnsupportedFormatError(
            f"Drive root is not a directory: {root}. "
            "Pass the mount point of the rekordbox USB stick."
        )

    pioneer = _find_child(root, "PIONEER")
    if pioneer is None or not pioneer.is_dir():
        raise UnsupportedFormatError(
            f"Expected PIONEER/ under {root}, but it was not found. "
            "Is this a rekordbox-exported USB stick?"
        )

    rekordbox_dir = _find_child(pioneer, "rekordbox")
    export_pdb = (
        _find_child(rekordbox_dir, "export.pdb") if rekordbox_dir is not None else None
    )
    if export_pdb is None or not export_pdb.is_file():
        expected = pioneer / "rekordbox" / "export.pdb"
        raise UnsupportedFormatError(
            f"Expected export.pdb at {expected} (case-insensitive), but it was "
            f"not found under {root}. A rekordbox USB export must include "
            "PIONEER/rekordbox/export.pdb."
        )

    usbanlz = _find_child(pioneer, "USBANLZ")
    if usbanlz is None or not usbanlz.is_dir():
        expected = pioneer / "USBANLZ"
        raise UnsupportedFormatError(
            f"Expected USBANLZ/ at {expected} (case-insensitive), but it was "
            f"not found under {root}. Beatgrids and cues live under "
            "PIONEER/USBANLZ/."
        )

    export_ext: Path | None = None
    if rekordbox_dir is not None:
        candidate = _find_child(rekordbox_dir, "exportExt.pdb")
        if candidate is not None and candidate.is_file():
            export_ext = candidate

    exportext_present = export_ext is not None
    if exportext_present:
        # G1d: one friendly line per run, never per track.
        logger.info(_G1D_MESSAGE)

    contents = _find_child(root, "Contents")
    if contents is not None and not contents.is_dir():
        contents = None

    engine_lib = _find_child(root, "Engine Library")
    if engine_lib is not None and not engine_lib.is_dir():
        engine_lib = None

    return StickLayout(
        drive_root=root,
        export_pdb=export_pdb,
        export_ext_pdb=export_ext,
        usbanlz_dir=usbanlz,
        contents_dir=contents,
        engine_library_dir=engine_lib,
        is_full_rebuild=engine_lib is not None,
        exportext_present=exportext_present,
    )


def resolve_anlz_paths(drive_root: Path, anlz_path: str) -> AnlzPaths:
    """Resolve a pdb-stored ANLZ path onto the real filesystem.

    Parameters
    ----------
    drive_root:
        Stick mount / folder root (same as for :func:`scan_drive`).
    anlz_path:
        Path from export.pdb, e.g.
        ``/PIONEER/USBANLZ/P02B/00003DA7/ANLZ0000.DAT`` (verified real form).

    Returns
    -------
    AnlzPaths
        ``dat`` is the ``.DAT`` when present; ``ext`` / ``two_ex`` are
        sibling ``.EXT`` / ``.2EX`` when present. Missing ``.EXT`` is
        normal — not an error. Missing ``.DAT`` yields ``dat=None``
        (caller skips the track).
    """
    if not anlz_path:
        return AnlzPaths(dat=None, ext=None, two_ex=None)

    stored = PurePosixPath(anlz_path.replace("\\", "/"))
    segments = [p for p in stored.parts if p and p != "/"]
    if not segments:
        return AnlzPaths(dat=None, ext=None, two_ex=None)

    # Resolve the directory containing the ANLZ file, then pick siblings.
    dir_segments = segments[:-1]
    basename = segments[-1]
    stem = PurePosixPath(basename).stem  # ANLZ0000

    parent = (
        _resolve_case_insensitive(Path(drive_root), dir_segments)
        if dir_segments
        else Path(drive_root)
    )
    if parent is None or not parent.is_dir():
        return AnlzPaths(dat=None, ext=None, two_ex=None)

    dat = _find_named_file(parent, basename) or _find_named_file(
        parent, f"{stem}.DAT"
    )
    ext = _find_named_file(parent, f"{stem}.EXT")
    two_ex = _find_named_file(parent, f"{stem}.2EX")

    return AnlzPaths(dat=dat, ext=ext, two_ex=two_ex)


def _find_child(parent: Path, name: str) -> Path | None:
    """Return the child of ``parent`` whose name matches ``name`` case-insensitively.

    Matching is also insensitive to Unicode normalization form — see
    :func:`rb2engine.reader.paths.fold_name`.
    """
    if not parent.is_dir():
        return None
    target = fold_name(name)
    try:
        for entry in parent.iterdir():
            if fold_name(entry.name) == target:
                return entry
    except OSError:
        return None
    return None


def _find_named_file(parent: Path, name: str) -> Path | None:
    """Like :func:`_find_child` but only if the match is a file."""
    found = _find_child(parent, name)
    if found is not None and found.is_file():
        return found
    return None


def _resolve_case_insensitive(root: Path, segments: list[str]) -> Path | None:
    """Walk ``segments`` under ``root``, matching each name case-insensitively.

    Returns the path using on-disk casing so open() works on case-sensitive
    hosts when the volume is FAT32.
    """
    current = root
    for segment in segments:
        if not current.is_dir():
            return None
        match = _find_child(current, segment)
        if match is None:
            return None
        current = match
    return current
