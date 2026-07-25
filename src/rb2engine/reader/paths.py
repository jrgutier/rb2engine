"""Contents/ re-rooting and drive-letter stripping for track paths.

export.pdb stores locations as drive-letter-prefixed paths through a top-level
``Contents/`` folder (e.g. ``D:/Contents/Artist/Track.mp3``). The drive letter
reflects the exporting machine and must never be trusted. Resolution finds the
``Contents`` path segment and re-roots everything from there onto the actual
mount point.

Type discipline (plan R4):
- Paths *stored* in Engine / pdb are conceptual POSIX forms (forward slashes).
- Paths *on the local filesystem* are ``pathlib.Path``.
Never mix the two.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath


def resolve_track_path(raw_path: str, drive_root: Path) -> Path | None:
    """Map a pdb-stored track path onto a real file under ``drive_root``.

    Parameters
    ----------
    raw_path:
        The string from export.pdb (may use ``/`` or ``\\``, may start with a
        Windows drive letter). Treated as a POSIX-style path after separator
        normalization — not opened as a host path.
    drive_root:
        Actual mount / stick root on this host (``pathlib.Path``).

    Returns
    -------
    Path | None
        The on-disk path with real casing if the file exists; ``None`` if there
        is no ``Contents`` segment or the file cannot be found (caller skips
        and reports — never raises for ordinary miss cases).
    """
    if not raw_path or not isinstance(drive_root, Path):
        return None

    # Stored form: normalize separators, then parse as PurePosixPath segments.
    # Do not construct a host Path from raw_path — that would trust drive letters.
    stored = PurePosixPath(raw_path.replace("\\", "/"))
    parts = stored.parts
    if not parts:
        return None

    # Drop empty segments and a lone root marker if present after normalization.
    segments = [p for p in parts if p and p != "/"]

    contents_idx: int | None = None
    for i, segment in enumerate(segments):
        # Full segment match only — "MyContentsMix" / "ContentsExtra" must not hit.
        if segment.lower() == "contents":
            contents_idx = i
            break

    if contents_idx is None:
        return None

    relative_segments = segments[contents_idx:]
    resolved = _resolve_case_insensitive(drive_root, relative_segments)
    if resolved is None or not resolved.is_file():
        return None
    return resolved


def _resolve_case_insensitive(root: Path, segments: list[str]) -> Path | None:
    """Walk ``segments`` under ``root``, matching each name case-insensitively.

    Returns the path using on-disk casing so open() works on case-sensitive
    hosts when the volume is FAT32 (case-insensitive) but the host is not.
    """
    current = root
    for segment in segments:
        if not current.is_dir():
            return None
        try:
            entries = list(current.iterdir())
        except OSError:
            return None

        match: Path | None = None
        target = segment.lower()
        for entry in entries:
            if entry.name.lower() == target:
                match = entry
                break
        if match is None:
            return None
        current = match
    return current
