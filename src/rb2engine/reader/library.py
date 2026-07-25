"""Compose the reader modules into a complete SourceLibrary.

WHY THIS MODULE EXISTS
----------------------
`scan`, `pdb`, `anlz` and `artwork` were each built and tested in isolation.
Every one of them passed its own suite while the pipeline did not connect end
to end: nothing joined a track to its ANLZ files, so no beatgrid or cue ever
reached the IR. This module is that join, and it is deliberately thin — all
the real work lives in the modules it calls.

The join key is `SourceTrack.analyze_path` (pdb `ofs_strings[14]`), e.g.
``/PIONEER/USBANLZ/P02B/00003DA7/ANLZ0000.DAT``. Without it a track has
metadata but no performance data, which is the entire point of the tool.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

from rb2engine.ir import SourceLibrary, SourceTrack
from rb2engine.progress import ProgressCallback
from rb2engine.reader.anlz import read_anlz
from rb2engine.reader.artwork import extract_artwork
from rb2engine.reader.pdb import parse_export_pdb
from rb2engine.reader.scan import resolve_anlz_paths, scan_drive


def read_library(
    drive_root: Path,
    *,
    with_anlz: bool = True,
    with_artwork: bool = True,
    on_progress: ProgressCallback | None = None,
) -> SourceLibrary:
    """Read a rekordbox stick into a fully-populated `SourceLibrary`.

    Strictly read-only: the stick is never written, and `export.pdb` is opened
    read-only by the parser.

    Parameters
    ----------
    drive_root:
        Stick mount point, e.g. ``/Volumes/USB DISK``.
    with_anlz:
        Read beatgrids/cues. Disable to inspect metadata quickly.
    with_artwork:
        Extract embedded album art. Disable for speed — it opens every audio
        file, which dominates runtime on a large library.
    on_progress:
        Optional ``(phase, done, total)`` sink. The per-track loop below opens
        two or three files per track over USB, so on a real library this is
        the difference between a visible conversion and a silent one.
    """
    drive_root = Path(drive_root)
    layout = scan_drive(drive_root)

    if on_progress is not None:
        # Indeterminate: the pdb page count is not known until it is parsed.
        on_progress("scanning", 0, 0)

    lib = parse_export_pdb(layout.export_pdb, drive_root)
    warnings = list(lib.warnings)

    if layout.exportext_present:
        # G1d — once per run, never per track. MyTags are out of scope.
        warnings.append(
            "exportExt.pdb present (rekordbox MyTags): detected and skipped; "
            "this is expected and not an error."
        )

    if not (with_anlz or with_artwork):
        return dataclasses.replace(lib, warnings=warnings)

    tracks: dict[int, SourceTrack] = {}
    total = len(lib.tracks)
    if on_progress is not None:
        on_progress("reading tracks", 0, total)
    for done, (tid, track) in enumerate(lib.tracks.items(), start=1):
        beatgrid = track.beatgrid
        cues = track.cues
        artwork = track.artwork

        if with_anlz and track.analyze_path:
            try:
                paths = resolve_anlz_paths(drive_root, track.analyze_path)
            except Exception as exc:  # noqa: BLE001 - one bad track must not end the run
                warnings.append(f"track {tid}: ANLZ path unresolved: {exc}")
            else:
                if paths.dat is not None or paths.ext is not None:
                    try:
                        beatgrid, cues, anlz_warnings = read_anlz(
                            paths.dat, paths.ext, track.sample_rate
                        )
                        warnings.extend(f"track {tid}: {w}" for w in anlz_warnings)
                    except Exception as exc:  # noqa: BLE001 - skip+report, per policy
                        warnings.append(f"track {tid}: ANLZ unreadable: {exc}")

        if with_artwork and track.resolved_path is not None:
            artwork = extract_artwork(track.resolved_path)

        tracks[tid] = dataclasses.replace(
            track, beatgrid=beatgrid, cues=cues, artwork=artwork
        )
        if on_progress is not None:
            on_progress("reading tracks", done, total)

    return dataclasses.replace(lib, tracks=tracks, warnings=warnings)
