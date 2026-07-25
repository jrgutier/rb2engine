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

import unicodedata
from pathlib import Path, PurePosixPath


def fold_name(name: str) -> str:
    """Comparison key for one filename segment: case- and NFC-normalized.

    WHY NORMALIZATION IS REQUIRED
    -----------------------------
    export.pdb stores ``São Paulo - Single`` composed (NFC: U+00E3), while the
    same directory read back off the stick with ``iterdir()`` comes out
    decomposed (NFD: ``a`` + U+0303). They are the same name to every human and
    to the filesystem, but different ``str`` objects, so a bare ``.lower()``
    comparison misses.

    This is not a corner case: it silently dropped 46 of 3,666 tracks on a real
    library — every track whose path contained an accented character. The
    filesystem hides it, because macOS resolves an NFC path against NFD
    on-disk names inside the syscall; only our own iterdir()-and-compare walk
    sees the difference, which is exactly why it went unnoticed.
    """
    return unicodedata.normalize("NFC", name).lower()


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
        if fold_name(segment) == "contents":
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

    Returns the path using on-disk casing **and on-disk Unicode normalization**,
    so the caller opens the name the filesystem actually holds. See
    :func:`fold_name` for why the comparison cannot be a bare ``.lower()``.
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
        target = fold_name(segment)
        for entry in entries:
            if fold_name(entry.name) == target:
                match = entry
                break
        if match is None:
            return None
        current = match
    return current
