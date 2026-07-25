"""Fixtures for the progress tests: a minimal stick and a TTY-controlled run.

Kept out of ``test_progress.py`` so the tests there read as assertions about
behaviour rather than as scaffolding.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner


def make_fake_stick(root: Path, *, n_tracks: int = 3) -> Path:
    """Build the smallest tree ``scan_drive`` + ``parse_export_pdb`` accept.

    The pdb is a stub, so the reader is monkeypatched by the caller when a real
    parse is not the thing under test.
    """
    (root / "PIONEER" / "rekordbox").mkdir(parents=True, exist_ok=True)
    (root / "PIONEER" / "rekordbox" / "export.pdb").write_bytes(b"stub")
    (root / "PIONEER" / "USBANLZ").mkdir(exist_ok=True)
    contents = root / "Contents"
    contents.mkdir(exist_ok=True)
    for i in range(n_tracks):
        (contents / f"track{i}.mp3").write_bytes(b"audio")
    return root


def _stub_library(root: Path, n_tracks: int) -> Any:
    from rb2engine.ir import SourceLibrary, SourceTrack

    tracks = {}
    for i in range(n_tracks):
        audio = root / "Contents" / f"track{i}.mp3"
        tracks[i + 1] = SourceTrack(
            rb_id=i + 1,
            title=f"T{i}",
            artist="A",
            album="Al",
            genre="G",
            label="",
            comment="",
            composer="",
            remixer="",
            year=2024,
            track_number=1,
            disc_number=None,
            bpm=128.0,
            key_name="Am",
            rating=0,
            play_count=0,
            bitrate=320,
            file_size=5,
            file_type="mp3",
            sample_rate=44100,
            duration_s=180,
            total_samples=7938000,
            raw_path=f"/Contents/track{i}.mp3",
            resolved_path=audio,
            beatgrid=None,
            cues=[],
            artwork=None,
        )
    return SourceLibrary(drive_root=root, tracks=tracks, playlists=[], warnings=[])


def patch_pdb_parse(
    monkeypatch: pytest.MonkeyPatch, root: Path, n_tracks: int
) -> Any:
    """Make ``read_library``'s pdb parse return a stub library.

    The join loop under test runs on whatever ``parse_export_pdb`` returns, so
    stubbing there exercises the real loop without needing a real export.pdb.
    """
    lib = _stub_library(root, n_tracks)
    monkeypatch.setattr(
        "rb2engine.reader.library.parse_export_pdb", lambda *_a, **_k: lib
    )
    return lib


class _Stream(io.StringIO):
    """StringIO whose ``isatty()`` answer is set by the test."""

    def __init__(self, isatty: bool) -> None:
        super().__init__()
        self._isatty = isatty

    def isatty(self) -> bool:
        return self._isatty


def run_convert_capturing_stderr(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    isatty: bool,
    args: list[str],
    n_tracks: int = 40,
) -> str:
    """Run ``rb2engine convert`` against a stubbed reader; return raw stderr.

    ``CliRunner`` replaces ``sys.stderr`` with a non-TTY buffer, which would
    make every progress test trivially pass by drawing nothing. So the stream
    the reporter writes to is injected here instead, with ``isatty()`` under
    the test's control — that is the single condition being exercised.
    """
    import rb2engine.cli as cli_mod
    from rb2engine.logging import reset_logging

    make_fake_stick(root, n_tracks=n_tracks)
    lib = _stub_library(root, n_tracks)

    def fake_read_library(drive: Path, **kwargs: Any) -> Any:
        on_progress = kwargs.get("on_progress")
        if on_progress is not None:
            total = len(lib.tracks)
            for done in range(total + 1):
                on_progress("reading tracks", done, total)
        return lib

    monkeypatch.setattr("rb2engine.reader.library.read_library", fake_read_library)

    stream = _Stream(isatty)
    monkeypatch.setattr(cli_mod, "_progress_stream", lambda: stream, raising=False)

    reset_logging()
    try:
        CliRunner().invoke(cli_mod.main, [*args, "convert", str(root)])
    finally:
        reset_logging()
    return stream.getvalue()
