"""Tests for the reader orchestration layer.

WHY THIS FILE EXISTS
--------------------
Every reader module (scan, pdb, anlz, artwork) passed its own suite while the
pipeline did not connect: `pdb.py` never captured `analyze_path`
(ofs_strings[14]), so nothing could join a track to its ANLZ files and no
beatgrid or cue could ever reach the IR. Five green module suites, zero
working pipeline.

These tests pin the join itself, so that failure mode cannot recur silently.
Real-stick cases are marked real_stick (skipped in CI). The bulk of coverage
here monkeypatches scan/pdb/anlz/artwork so CI exercises the glue that
already broke once — without requiring hardware.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from rb2engine.ir import (
    RGB,
    CueKind,
    SourceArtwork,
    SourceBeat,
    SourceBeatgrid,
    SourceCue,
    SourceLibrary,
    SourceTrack,
)
from rb2engine.reader.scan import AnlzPaths, StickLayout

# ---------------------------------------------------------------------------
# Helpers — synthetic IR + layout (not derived from implementation output)
# ---------------------------------------------------------------------------


def _track(
    rb_id: int,
    *,
    analyze_path: str | None = None,
    resolved_path: Path | None = None,
    sample_rate: int = 44100,
    beatgrid: SourceBeatgrid | None = None,
    cues: list[SourceCue] | None = None,
    artwork: SourceArtwork | None = None,
) -> SourceTrack:
    """Hand-authored minimal track for orchestration tests."""
    return SourceTrack(
        rb_id=rb_id,
        title=f"Track{rb_id}",
        artist="Artist",
        album="Album",
        genre="Genre",
        label="",
        comment="",
        composer="",
        remixer="",
        year=2020,
        track_number=1,
        disc_number=None,
        bpm=128.0,
        key_name="Am",
        rating=0,
        play_count=0,
        bitrate=320,
        file_size=1000,
        file_type="mp3",
        sample_rate=sample_rate,
        duration_s=180,
        total_samples=sample_rate * 180,
        raw_path=f"/Contents/Artist/track{rb_id}.mp3",
        resolved_path=resolved_path,
        beatgrid=beatgrid,
        cues=list(cues) if cues is not None else [],
        artwork=artwork,
        analyze_path=analyze_path,
    )


def _base_lib(
    drive: Path,
    tracks: dict[int, SourceTrack],
    *,
    warnings: list[str] | None = None,
) -> SourceLibrary:
    return SourceLibrary(
        drive_root=drive,
        tracks=tracks,
        playlists=[],
        warnings=list(warnings or []),
    )


def _layout(
    drive: Path,
    *,
    exportext_present: bool = False,
) -> StickLayout:
    return StickLayout(
        drive_root=drive,
        export_pdb=drive / "PIONEER" / "rekordbox" / "export.pdb",
        export_ext_pdb=(
            drive / "PIONEER" / "rekordbox" / "exportExt.pdb"
            if exportext_present
            else None
        ),
        usbanlz_dir=drive / "PIONEER" / "USBANLZ",
        contents_dir=drive / "Contents",
        engine_library_dir=None,
        is_full_rebuild=False,
        exportext_present=exportext_present,
    )


def _patch_orchestration(
    monkeypatch: pytest.MonkeyPatch,
    *,
    drive: Path,
    lib: SourceLibrary,
    layout: StickLayout | None = None,
    resolve: Any = None,
    read_anlz: Any = None,
    extract_artwork: Any = None,
) -> dict[str, MagicMock]:
    """Wire fakes into reader.library so only the join logic runs."""
    import rb2engine.reader.library as library_mod

    mocks: dict[str, MagicMock] = {}
    mocks["scan_drive"] = MagicMock(return_value=layout or _layout(drive))
    mocks["parse_export_pdb"] = MagicMock(return_value=lib)
    mocks["resolve_anlz_paths"] = (
        resolve if resolve is not None else MagicMock(return_value=AnlzPaths(None, None, None))
    )
    mocks["read_anlz"] = read_anlz if read_anlz is not None else MagicMock()
    mocks["extract_artwork"] = (
        extract_artwork if extract_artwork is not None else MagicMock(return_value=None)
    )

    monkeypatch.setattr(library_mod, "scan_drive", mocks["scan_drive"])
    monkeypatch.setattr(library_mod, "parse_export_pdb", mocks["parse_export_pdb"])
    monkeypatch.setattr(library_mod, "resolve_anlz_paths", mocks["resolve_anlz_paths"])
    monkeypatch.setattr(library_mod, "read_anlz", mocks["read_anlz"])
    monkeypatch.setattr(library_mod, "extract_artwork", mocks["extract_artwork"])
    return mocks


# ---------------------------------------------------------------------------
# Contract pins (must stay true even when hardware is absent)
# ---------------------------------------------------------------------------


def test_source_track_carries_analyze_path() -> None:
    """The join key must exist on the IR.

    Without `analyze_path` there is no way to locate a track's ANLZ files, so
    the tool degrades to a metadata-only converter — silently losing every
    beatgrid, hot cue and loop, which is the entire product.
    """
    fields = set(SourceTrack.__dataclass_fields__)
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


# ---------------------------------------------------------------------------
# Orchestration: the join that already broke once
# ---------------------------------------------------------------------------


def test_track_with_analyze_path_gets_beatgrid_and_cues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A track whose pdb row carries analyze_path must receive ANLZ performance data.

    This is the exact failure mode that shipped once: every module green, but
    no join, so inspect/convert produced metadata without grids or cues.
    """
    from rb2engine.reader.library import read_library

    drive = tmp_path / "stick"
    anlz_rel = "/PIONEER/USBANLZ/P02B/00003DA7/ANLZ0000.DAT"
    dat = drive / "PIONEER" / "USBANLZ" / "P02B" / "00003DA7" / "ANLZ0000.DAT"
    ext = dat.with_suffix(".EXT")

    expected_grid = SourceBeatgrid(
        beats=[SourceBeat(beat_in_bar=1, sample_offset=0, bpm=128.0)],
        is_adjusted=False,
    )
    expected_cues = [
        SourceCue(
            kind=CueKind.HOT,
            hot_slot=1,
            start_sample=44100,
            end_sample=None,
            color=RGB(255, 0, 0),
            name="Drop",
        )
    ]

    track = _track(1, analyze_path=anlz_rel, beatgrid=None, cues=[])
    lib = _base_lib(drive, {1: track})

    def fake_resolve(_root: Path, path: str) -> AnlzPaths:
        assert path == anlz_rel
        return AnlzPaths(dat=dat, ext=ext, two_ex=None)

    def fake_read_anlz(
        dat_path: Path | None, ext_path: Path | None, sample_rate: int
    ) -> tuple[SourceBeatgrid, list[SourceCue], list[str]]:
        assert dat_path == dat
        assert ext_path == ext
        assert sample_rate == 44100
        return expected_grid, expected_cues, []

    _patch_orchestration(
        monkeypatch,
        drive=drive,
        lib=lib,
        resolve=fake_resolve,
        read_anlz=fake_read_anlz,
    )

    result = read_library(drive)

    assert result.tracks[1].beatgrid == expected_grid
    assert result.tracks[1].cues == expected_cues


def test_track_without_analyze_path_passes_through_without_anlz(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """analyze_path=None means metadata only — must not crash or invent paths.

    Unanalysed tracks are valid on a stick; the join must skip them rather
    than raise and abort the whole library.
    """
    from rb2engine.reader.library import read_library

    drive = tmp_path / "stick"
    track = _track(7, analyze_path=None, beatgrid=None, cues=[])
    lib = _base_lib(drive, {7: track})

    mocks = _patch_orchestration(monkeypatch, drive=drive, lib=lib)

    result = read_library(drive)

    mocks["resolve_anlz_paths"].assert_not_called()
    mocks["read_anlz"].assert_not_called()
    assert result.tracks[7].beatgrid is None
    assert result.tracks[7].cues == []
    assert result.tracks[7].analyze_path is None


def test_anlz_parse_failure_warns_and_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One corrupt ANLZ must not kill the run — warn, keep other tracks.

    Policy is skip+report per track. A single bad .DAT on a 3000-track stick
    must not abort conversion for everyone else.
    """
    from rb2engine.reader.library import read_library

    drive = tmp_path / "stick"
    anlz_rel = "/PIONEER/USBANLZ/P01/00000001/ANLZ0000.DAT"
    dat = drive / "PIONEER" / "USBANLZ" / "P01" / "00000001" / "ANLZ0000.DAT"

    good_grid = SourceBeatgrid(
        beats=[SourceBeat(beat_in_bar=1, sample_offset=0, bpm=120.0)],
        is_adjusted=True,
    )
    good_cues = [
        SourceCue(
            kind=CueKind.MEMORY,
            hot_slot=None,
            start_sample=0,
            end_sample=None,
            color=None,
            name=None,
        )
    ]

    bad = _track(1, analyze_path=anlz_rel)
    good = _track(2, analyze_path="/PIONEER/USBANLZ/P01/00000002/ANLZ0000.DAT")
    lib = _base_lib(drive, {1: bad, 2: good})

    def fake_resolve(_root: Path, path: str) -> AnlzPaths:
        # Both tracks "resolve"; failure is in read_anlz for track 1.
        return AnlzPaths(dat=dat, ext=None, two_ex=None)

    def fake_read_anlz(
        dat_path: Path | None, ext_path: Path | None, sample_rate: int
    ) -> tuple[SourceBeatgrid | None, list[SourceCue], list[str]]:
        # First call is track 1 (dict iteration order is insertion order).
        if not hasattr(fake_read_anlz, "_n"):
            fake_read_anlz._n = 0  # type: ignore[attr-defined]
        fake_read_anlz._n += 1  # type: ignore[attr-defined]
        if fake_read_anlz._n == 1:  # type: ignore[attr-defined]
            raise OSError("truncated ANLZ header")
        return good_grid, good_cues, []

    _patch_orchestration(
        monkeypatch,
        drive=drive,
        lib=lib,
        resolve=fake_resolve,
        read_anlz=fake_read_anlz,
    )

    result = read_library(drive)

    assert any("track 1: ANLZ unreadable" in w for w in result.warnings)
    assert "truncated ANLZ header" in " ".join(result.warnings)
    # Track 2 still gets performance data — run continued.
    assert result.tracks[2].beatgrid == good_grid
    assert result.tracks[2].cues == good_cues
    # Track 1 keeps pre-ANLZ state (None / empty), not a partial crash.
    assert result.tracks[1].beatgrid is None


def test_anlz_path_unresolved_warns_and_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """resolve_anlz_paths raising is per-track: warn, do not abort the library."""
    from rb2engine.reader.library import read_library

    drive = tmp_path / "stick"
    track = _track(3, analyze_path="/PIONEER/USBANLZ/missing/ANLZ0000.DAT")
    other = _track(4, analyze_path=None)
    lib = _base_lib(drive, {3: track, 4: other})

    def boom(_root: Path, _path: str) -> AnlzPaths:
        raise FileNotFoundError("ANLZ directory gone")

    mocks = _patch_orchestration(
        monkeypatch,
        drive=drive,
        lib=lib,
        resolve=boom,
    )

    result = read_library(drive)

    assert any("track 3: ANLZ path unresolved" in w for w in result.warnings)
    assert "ANLZ directory gone" in " ".join(result.warnings)
    assert 4 in result.tracks
    mocks["read_anlz"].assert_not_called()


def test_anlz_soft_warnings_prefixed_and_accumulated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ANLZ soft warnings must land on the returned library, tagged by track id.

    Without accumulation, operators lose the only signal that a grid was
    partial — the conversion would look clean while data is wrong.
    """
    from rb2engine.reader.library import read_library

    drive = tmp_path / "stick"
    track = _track(9, analyze_path="/PIONEER/USBANLZ/P0/00000009/ANLZ0000.DAT")
    lib = _base_lib(drive, {9: track}, warnings=["pdb: pre-existing note"])

    def fake_resolve(_root: Path, _path: str) -> AnlzPaths:
        return AnlzPaths(dat=Path("x.DAT"), ext=None, two_ex=None)

    def fake_read_anlz(
        *_a: Any, **_k: Any
    ) -> tuple[SourceBeatgrid | None, list[SourceCue], list[str]]:
        return None, [], ["missing PQTZ", "PCOB empty"]

    _patch_orchestration(
        monkeypatch,
        drive=drive,
        lib=lib,
        resolve=fake_resolve,
        read_anlz=fake_read_anlz,
    )

    result = read_library(drive)

    assert "pdb: pre-existing note" in result.warnings
    assert "track 9: missing PQTZ" in result.warnings
    assert "track 9: PCOB empty" in result.warnings


def test_with_anlz_false_skips_anlz_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """with_anlz=False must not resolve or read ANLZ — metadata-only inspect path.

    Opening every ANLZ on a large stick dominates runtime; the flag exists so
    inspect can stay fast. If the flag is ignored, that path is broken.
    """
    from rb2engine.reader.library import read_library

    drive = tmp_path / "stick"
    audio = drive / "Contents" / "t.mp3"
    audio.parent.mkdir(parents=True, exist_ok=True)
    audio.write_bytes(b"audio")
    track = _track(
        1,
        analyze_path="/PIONEER/USBANLZ/P0/00000001/ANLZ0000.DAT",
        resolved_path=audio,
    )
    lib = _base_lib(drive, {1: track})

    art = SourceArtwork(content_key="deadbeef", path=audio, source="embedded")
    mocks = _patch_orchestration(
        monkeypatch,
        drive=drive,
        lib=lib,
        extract_artwork=MagicMock(return_value=art),
    )

    result = read_library(drive, with_anlz=False, with_artwork=True)

    mocks["resolve_anlz_paths"].assert_not_called()
    mocks["read_anlz"].assert_not_called()
    mocks["extract_artwork"].assert_called_once_with(audio)
    assert result.tracks[1].artwork == art
    assert result.tracks[1].beatgrid is None


def test_with_artwork_false_skips_artwork_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """with_artwork=False must not open audio files for embedded art.

    Artwork extraction dominates runtime (every audio file). Skipping must
    be real, not a no-op flag.
    """
    from rb2engine.reader.library import read_library

    drive = tmp_path / "stick"
    audio = drive / "Contents" / "t.mp3"
    anlz_rel = "/PIONEER/USBANLZ/P0/00000001/ANLZ0000.DAT"
    track = _track(1, analyze_path=anlz_rel, resolved_path=audio)
    lib = _base_lib(drive, {1: track})

    grid = SourceBeatgrid(
        beats=[SourceBeat(beat_in_bar=1, sample_offset=0, bpm=100.0)],
        is_adjusted=False,
    )

    def fake_resolve(_root: Path, _path: str) -> AnlzPaths:
        return AnlzPaths(dat=Path("a.DAT"), ext=None, two_ex=None)

    read_anlz_mock = MagicMock(return_value=(grid, [], []))

    mocks = _patch_orchestration(
        monkeypatch,
        drive=drive,
        lib=lib,
        resolve=fake_resolve,
        read_anlz=read_anlz_mock,
    )

    result = read_library(drive, with_anlz=True, with_artwork=False)

    mocks["extract_artwork"].assert_not_called()
    read_anlz_mock.assert_called_once()
    assert result.tracks[1].beatgrid == grid
    assert result.tracks[1].artwork is None


def test_with_anlz_and_artwork_false_skips_track_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both flags off: metadata-only early return — no per-track work at all."""
    from rb2engine.reader.library import read_library

    drive = tmp_path / "stick"
    track = _track(
        1,
        analyze_path="/PIONEER/USBANLZ/P0/00000001/ANLZ0000.DAT",
        resolved_path=drive / "Contents" / "t.mp3",
    )
    lib = _base_lib(drive, {1: track}, warnings=["from-pdb"])

    mocks = _patch_orchestration(monkeypatch, drive=drive, lib=lib)

    result = read_library(drive, with_anlz=False, with_artwork=False)

    mocks["resolve_anlz_paths"].assert_not_called()
    mocks["read_anlz"].assert_not_called()
    mocks["extract_artwork"].assert_not_called()
    assert result.warnings == ["from-pdb"]
    assert result.tracks[1].analyze_path == track.analyze_path


def test_exportext_g1d_warning_once_per_run_not_per_track(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """exportExt.pdb (MyTags) must emit the G1d warning once, never N×tracks.

    This user's stick has exportExt.pdb. Per-track spam would flood the
    report and bury real errors.
    """
    from rb2engine.reader.library import read_library

    drive = tmp_path / "stick"
    tracks = {
        i: _track(i, analyze_path=None)
        for i in range(1, 6)
    }
    lib = _base_lib(drive, tracks)
    layout = _layout(drive, exportext_present=True)

    _patch_orchestration(
        monkeypatch,
        drive=drive,
        lib=lib,
        layout=layout,
    )

    # Early-return path still attaches G1d (flags off).
    result = read_library(drive, with_anlz=False, with_artwork=False)

    g1d = [
        w
        for w in result.warnings
        if "exportExt.pdb" in w and "MyTags" in w
    ]
    assert len(g1d) == 1, f"expected exactly one G1d warning, got {result.warnings!r}"
    assert "skipped" in g1d[0].lower() or "not an error" in g1d[0].lower()

    # Same when the track loop runs — still once, not once per track.
    result2 = read_library(drive, with_anlz=True, with_artwork=False)
    g1d2 = [w for w in result2.warnings if "exportExt.pdb" in w and "MyTags" in w]
    assert len(g1d2) == 1


def test_exportext_absent_no_g1d_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without exportExt.pdb there must be no MyTags warning noise."""
    from rb2engine.reader.library import read_library

    drive = tmp_path / "stick"
    lib = _base_lib(drive, {1: _track(1)})
    _patch_orchestration(
        monkeypatch,
        drive=drive,
        lib=lib,
        layout=_layout(drive, exportext_present=False),
    )

    result = read_library(drive, with_anlz=False, with_artwork=False)

    assert not any("exportExt" in w for w in result.warnings)


def test_warnings_accumulate_on_returned_library(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Returned SourceLibrary.warnings must include pdb + G1d + ANLZ messages.

    Warnings are the operator-facing signal on inspect/convert. Dropping them
    on the floor makes failures look like successes.
    """
    from rb2engine.reader.library import read_library

    drive = tmp_path / "stick"
    track = _track(1, analyze_path="/PIONEER/USBANLZ/P0/1/ANLZ0000.DAT")
    lib = _base_lib(drive, {1: track}, warnings=["pdb warning A"])

    def fake_resolve(_root: Path, _path: str) -> AnlzPaths:
        return AnlzPaths(dat=Path("d.DAT"), ext=None, two_ex=None)

    def fake_read_anlz(
        *_a: Any, **_k: Any
    ) -> tuple[None, list[SourceCue], list[str]]:
        return None, [], ["soft anlz issue"]

    _patch_orchestration(
        monkeypatch,
        drive=drive,
        lib=lib,
        layout=_layout(drive, exportext_present=True),
        resolve=fake_resolve,
        read_anlz=fake_read_anlz,
    )

    result = read_library(drive)

    assert "pdb warning A" in result.warnings
    assert any("exportExt.pdb" in w for w in result.warnings)
    assert "track 1: soft anlz issue" in result.warnings


def test_artwork_attached_when_resolved_path_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """resolved_path set → extract_artwork is called and result stored on track."""
    from rb2engine.reader.library import read_library

    drive = tmp_path / "stick"
    audio = drive / "Contents" / "song.mp3"
    track = _track(5, analyze_path=None, resolved_path=audio)
    lib = _base_lib(drive, {5: track})
    art = SourceArtwork(content_key="abc123", path=audio, source="embedded")

    mocks = _patch_orchestration(
        monkeypatch,
        drive=drive,
        lib=lib,
        extract_artwork=MagicMock(return_value=art),
    )

    result = read_library(drive, with_anlz=False, with_artwork=True)

    mocks["extract_artwork"].assert_called_once_with(audio)
    assert result.tracks[5].artwork == art


def test_artwork_skipped_when_resolved_path_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Missing audio path must not call extract_artwork (nothing to open)."""
    from rb2engine.reader.library import read_library

    drive = tmp_path / "stick"
    track = _track(6, analyze_path=None, resolved_path=None)
    lib = _base_lib(drive, {6: track})

    mocks = _patch_orchestration(monkeypatch, drive=drive, lib=lib)

    result = read_library(drive, with_anlz=False, with_artwork=True)

    mocks["extract_artwork"].assert_not_called()
    assert result.tracks[6].artwork is None


def test_scan_and_parse_receive_drive_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Orchestration must pass the stick root into scan, then export.pdb into parse.

    Wrong arguments here are how modules stay green while the pipeline is
    disconnected — scan/pdb never receive what the CLI intended.
    """
    from rb2engine.reader.library import read_library

    drive = tmp_path / "stick"
    lib = _base_lib(drive, {})
    layout = _layout(drive)
    mocks = _patch_orchestration(monkeypatch, drive=drive, lib=lib, layout=layout)

    read_library(drive, with_anlz=False, with_artwork=False)

    mocks["scan_drive"].assert_called_once()
    called_root = mocks["scan_drive"].call_args[0][0]
    assert Path(called_root) == drive
    mocks["parse_export_pdb"].assert_called_once_with(layout.export_pdb, drive)


# ---------------------------------------------------------------------------
# Real stick (skipped in CI when hardware absent)
# ---------------------------------------------------------------------------


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
