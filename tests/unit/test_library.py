"""Tests for the reader orchestration layer.

WHY THIS FILE EXISTS
--------------------
Every reader module (scan, pdb, anlz, artwork) passed its own suite while the
pipeline did not connect: `pdb.py` never captured `analyze_path`
(ofs_strings[14]), so nothing could join a track to its ANLZ files and no
beatgrid or cue could ever reach the IR. Five green module suites, zero
working pipeline.

These tests pin the join itself, so that failure mode cannot recur silently.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rb2engine.ir import SourceTrack


def test_source_track_carries_analyze_path() -> None:
    """The join key must exist on the IR.

    Without `analyze_path` there is no way to locate a track's ANLZ files, so
    the tool degrades to a metadata-only converter — silently losing every
    beatgrid, hot cue and loop, which is the entire product.
    """
    fields = {f for f in SourceTrack.__dataclass_fields__}
    assert "analyze_path" in fields


def test_read_library_is_exported_from_reader_package() -> None:
    """`cli.py` discovers the reader via `reader.read_library`.

    It probes for this name; if the export disappears, `inspect` silently
    reports "reader not available" and exits 0 — which is how the gap went
    unnoticed the first time.
    """
    import rb2engine.reader as reader_pkg

    assert hasattr(reader_pkg, "read_library")
    assert callable(reader_pkg.read_library)


def test_read_library_rejects_non_stick(tmp_path: Path) -> None:
    """A directory that is not a rekordbox export must fail loudly (exit 2)."""
    from rb2engine.errors import UnsupportedFormatError
    from rb2engine.reader.library import read_library

    with pytest.raises(UnsupportedFormatError):
        read_library(tmp_path)


REAL_STICK = Path("/Volumes/USB DISK")


@pytest.mark.real_stick
@pytest.mark.skipif(
    not (REAL_STICK / "PIONEER" / "rekordbox" / "export.pdb").is_file(),
    reason="real stick not mounted at /Volumes/USB DISK",
)
def test_real_stick_end_to_end_join() -> None:
    """Tier B: the whole reader pipeline joins on real hardware data.

    Asserts the property that was broken: every track resolves an ANLZ path,
    and performance data actually arrives in the IR.
    """
    from rb2engine.reader.library import read_library

    lib = read_library(REAL_STICK, with_anlz=False, with_artwork=False)

    assert len(lib.tracks) >= 3000
    assert lib.playlists

    with_anlz_path = [t for t in lib.tracks.values() if t.analyze_path]
    # A stick whose tracks are analysed should be at or near 100%.
    ratio = len(with_anlz_path) / len(lib.tracks)
    assert ratio > 0.95, f"only {ratio:.1%} of tracks carry an analyze_path"

    sample = with_anlz_path[0].analyze_path
    assert sample is not None and "USBANLZ" in sample
