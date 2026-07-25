"""Tests for the click CLI: inspect, help, exit codes, unimplemented commands.

WHY these tests exist (not just what they check):

- ``inspect --json`` is the source of ``golden_ir.json``; it must call
  ``SourceLibrary.to_json_obj()`` so path canonicalization stays
  cross-platform. Re-serializing paths in the CLI would break the CI matrix.
- ``inspect`` is read-only and exits 0 even with skips — inspection is not
  conversion; mixing those exit semantics would break debugging workflows.
- Unimplemented commands must exit 2 with an honest message, never 0 —
  a silent success pretends the conversion ran.
- ``--help`` must work without importing incomplete reader modules, so the
  tool remains usable while other workers land pdb/anlz/scan.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from rb2engine.cli import main
from rb2engine.ir import (
    RGB,
    CueKind,
    SourceArtwork,
    SourceBeat,
    SourceBeatgrid,
    SourceCue,
    SourceLibrary,
    SourcePlaylist,
    SourceTrack,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _minimal_track(
    *,
    rb_id: int,
    resolved: Path | None,
    art_path: Path | None = None,
) -> SourceTrack:
    return SourceTrack(
        rb_id=rb_id,
        title="T",
        artist="A",
        album="Al",
        genre="G",
        label="L",
        comment="C",
        composer="Co",
        remixer="R",
        year=2020,
        track_number=1,
        disc_number=None,
        bpm=128.0,
        key_name="Am",
        rating=100,
        play_count=3,
        bitrate=320,
        file_size=1000,
        file_type="mp3",
        sample_rate=44100,
        duration_s=180,
        total_samples=7_938_000,
        raw_path="D:/Contents/A/t.mp3",
        resolved_path=resolved,
        beatgrid=SourceBeatgrid(
            beats=[SourceBeat(beat_in_bar=1, sample_offset=0, bpm=128.0)],
            is_adjusted=False,
        ),
        cues=[
            SourceCue(
                kind=CueKind.HOT,
                hot_slot=1,
                start_sample=44100,
                end_sample=None,
                color=RGB(0, 255, 0),
                name="Cue1",
            ),
        ],
        artwork=(
            SourceArtwork(content_key="abc", path=art_path, source="embedded")
            if art_path is not None
            else None
        ),
    )


def _synthetic_library(drive_root: Path) -> SourceLibrary:
    rel_audio = Path("Contents") / "Artist" / "track.mp3"
    rel_art = Path("PIONEER") / "Artwork" / "cover.jpg"
    (drive_root / rel_audio).parent.mkdir(parents=True, exist_ok=True)
    (drive_root / rel_art).parent.mkdir(parents=True, exist_ok=True)
    # Touch files so resolve() works on some platforms
    (drive_root / rel_audio).write_bytes(b"")
    (drive_root / rel_art).write_bytes(b"")
    return SourceLibrary(
        drive_root=drive_root,
        tracks={
            1: _minimal_track(
                rb_id=1,
                resolved=drive_root / rel_audio,
                art_path=drive_root / rel_art,
            ),
            2: _minimal_track(
                rb_id=2,
                resolved=drive_root / rel_audio,
                art_path=None,
            ),
        },
        playlists=[
            SourcePlaylist(
                rb_id=10,
                parent_rb_id=0,
                name="Root",
                sort_order=0,
                is_folder=True,
                track_rb_ids=[1, 2],
            ),
        ],
        warnings=["example"],
    )


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


# ---------------------------------------------------------------------------
# --help (must not require reader modules)
# ---------------------------------------------------------------------------


def test_app_help(runner: CliRunner) -> None:
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "inspect" in result.output
    assert "convert" in result.output
    assert "verify" in result.output
    assert "doctor" in result.output


@pytest.mark.parametrize("cmd", ["convert", "inspect", "verify", "doctor"])
def test_subcommand_help(runner: CliRunner, cmd: str) -> None:
    result = runner.invoke(main, [cmd, "--help"])
    assert result.exit_code == 0, result.output
    assert "Usage" in result.output or "usage" in result.output.lower()


# ---------------------------------------------------------------------------
# Unimplemented commands exit 2 (never 0)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        ["verify", "DRIVE"],
        ["doctor"],  # doctor takes --engine-db, not a positional drive
    ],
)
def test_unimplemented_exits_two_not_zero(
    runner: CliRunner, args: list[str], tmp_path: Path
) -> None:
    """Unimplemented must exit 2 with an honest message — never pretend success."""
    drive = tmp_path / "stick"
    drive.mkdir()
    argv = [a if a != "DRIVE" else str(drive) for a in args]
    result = runner.invoke(main, argv)
    assert result.exit_code == 2, f"{argv} exit={result.exit_code} out={result.output!r}"
    assert result.exit_code != 0
    combined = (result.output + (result.stderr or "")).lower()
    assert "not implemented" in combined or "not yet" in combined


def test_convert_on_non_stick_exits_two(runner: CliRunner, tmp_path: Path) -> None:
    """convert IS implemented now; a directory that is not a rekordbox export
    must still fail loudly with exit 2 rather than writing a partial library."""
    drive = tmp_path / "not-a-stick"
    drive.mkdir()
    result = runner.invoke(main, ["convert", str(drive)])
    assert result.exit_code == 2
    assert not (drive / "Engine Library").exists()


# ---------------------------------------------------------------------------
# inspect --json on synthetic SourceLibrary
# ---------------------------------------------------------------------------


def test_inspect_json_uses_to_json_obj_canonicalization(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """inspect --json must use SourceLibrary.to_json_obj() (no path re-serialization).

    WHY: golden_ir.json is byte-identical across mac/win/linux only because
    to_json_obj() emits drive-relative POSIX paths and elides drive_root.
    """
    drive = tmp_path / "stick"
    drive.mkdir()
    lib = _synthetic_library(drive)

    monkeypatch.setattr("rb2engine.cli.load_source_library", lambda _d: lib)

    result = runner.invoke(main, ["inspect", str(drive), "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)

    assert data["drive_root"] == "<drive_root>"
    blob = json.dumps(data)
    # No absolute paths / OS-native separators in path fields
    assert "/Volumes" not in blob
    assert "\\" not in blob
    # Drive-relative POSIX
    assert data["tracks"]["1"]["resolved_path"] == "Contents/Artist/track.mp3"
    assert data["tracks"]["1"]["artwork"]["path"] == "PIONEER/Artwork/cover.jpg"


def test_inspect_json_byte_identical_for_two_drive_roots(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same library content under two drive_root values → identical JSON bytes.

    WHY: CI matrix mounts fixtures at different absolute paths; inspect output
    must not embed those paths or golden_ir.json cannot pin.
    """
    root_a = tmp_path / "stick_a"
    root_b = tmp_path / "other" / "stick_b"
    root_a.mkdir(parents=True)
    root_b.mkdir(parents=True)
    lib_a = _synthetic_library(root_a)
    lib_b = _synthetic_library(root_b)

    # First invoke
    monkeypatch.setattr("rb2engine.cli.load_source_library", lambda _d: lib_a)
    ra = runner.invoke(main, ["inspect", str(root_a), "--json"])
    assert ra.exit_code == 0, ra.output

    monkeypatch.setattr("rb2engine.cli.load_source_library", lambda _d: lib_b)
    rb = runner.invoke(main, ["inspect", str(root_b), "--json"])
    assert rb.exit_code == 0, rb.output

    # Normalize trailing whitespace only; content must match exactly
    assert ra.output.strip() == rb.output.strip()


def test_inspect_json_matches_to_json_obj_exactly(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLI JSON must be the serialization of to_json_obj(), not a parallel schema."""
    drive = tmp_path / "stick"
    drive.mkdir()
    lib = _synthetic_library(drive)
    monkeypatch.setattr("rb2engine.cli.load_source_library", lambda _d: lib)

    result = runner.invoke(main, ["inspect", str(drive), "--json"])
    assert result.exit_code == 0
    expected = json.dumps(lib.to_json_obj(), indent=2, ensure_ascii=False) + "\n"
    # Allow either indent=2 or compact — but object must equal to_json_obj()
    assert json.loads(result.output) == lib.to_json_obj()
    # Prefer pretty for golden diffs; if CLI uses indent=2, full match:
    if result.output.endswith("\n"):
        assert json.loads(result.output) == json.loads(expected)


def test_inspect_track_filter(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--track ID restricts dump to that track for focused debugging."""
    drive = tmp_path / "stick"
    drive.mkdir()
    lib = _synthetic_library(drive)
    monkeypatch.setattr("rb2engine.cli.load_source_library", lambda _d: lib)

    result = runner.invoke(main, ["inspect", str(drive), "--json", "--track", "2"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert set(data["tracks"].keys()) == {"2"}


def test_inspect_exits_zero_even_with_warnings(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inspection is not conversion: skips/warnings do not change exit code."""
    drive = tmp_path / "stick"
    drive.mkdir()
    lib = _synthetic_library(drive)
    # warnings already present on synthetic lib
    monkeypatch.setattr("rb2engine.cli.load_source_library", lambda _d: lib)
    result = runner.invoke(main, ["inspect", str(drive), "--json"])
    assert result.exit_code == 0


def test_inspect_is_read_only(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """inspect must write nothing under the drive (report, Engine Library, etc.)."""
    drive = tmp_path / "stick"
    drive.mkdir()
    pioneer = drive / "PIONEER"
    pioneer.mkdir()
    (pioneer / "export.pdb").write_bytes(b"x")
    lib = _synthetic_library(drive)
    monkeypatch.setattr("rb2engine.cli.load_source_library", lambda _d: lib)

    before = {
        p.relative_to(drive): (p.stat().st_mtime_ns, p.stat().st_size)
        for p in drive.rglob("*")
        if p.is_file()
    }
    result = runner.invoke(main, ["inspect", str(drive), "--json"])
    assert result.exit_code == 0, result.output
    after_files = {p.relative_to(drive) for p in drive.rglob("*") if p.is_file()}
    assert after_files == set(before.keys())
    for rel, (mtime, size) in before.items():
        st = (drive / rel).stat()
        assert st.st_mtime_ns == mtime
        assert st.st_size == size
    # No Engine Library / report created at drive root
    assert not (drive / "Engine Library").exists()
    assert not (drive / "rb2engine-report.json").exists()


def test_inspect_human_mode_exits_zero(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    drive = tmp_path / "stick"
    drive.mkdir()
    lib = _synthetic_library(drive)
    monkeypatch.setattr("rb2engine.cli.load_source_library", lambda _d: lib)
    result = runner.invoke(main, ["inspect", str(drive)])
    assert result.exit_code == 0, result.output
    assert "2" in result.output or "track" in result.output.lower()


# ---------------------------------------------------------------------------
# Logging: --log-json goes to stderr; stdout stays clean for inspect --json
# ---------------------------------------------------------------------------


def test_log_json_does_not_pollute_inspect_stdout(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Structured logs on stderr keep inspect --json pipeable on stdout."""
    drive = tmp_path / "stick"
    drive.mkdir()
    lib = _synthetic_library(drive)
    monkeypatch.setattr("rb2engine.cli.load_source_library", lambda _d: lib)

    result = runner.invoke(main, ["--log-json", "-v", "inspect", str(drive), "--json"])
    assert result.exit_code == 0, result.output
    # stdout (not mixed output) must be pure IR JSON
    stdout = result.stdout
    json.loads(stdout)
    # Log lines on stderr: each a JSON object with stage/event
    stderr = result.stderr or ""
    assert stderr.strip(), "expected at least one JSON log line on stderr at -v"
    for raw_line in stderr.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        obj = json.loads(line)
        assert "stage" in obj
        assert "event" in obj


def test_exit_code_mapping_exported_for_cli() -> None:
    """CLI applies report.exit_code_for; re-assert mapping stays 0/1/2."""
    from rb2engine.report import ConversionReport, exit_code_for

    clean = ConversionReport()
    assert exit_code_for(clean) == 0
    skips = ConversionReport()
    skips.add_skip(track_id=1, reason_code="x", message="y")
    assert exit_code_for(skips) == 1
    fatal = ConversionReport()
    fatal.mark_fatal("boom")
    assert exit_code_for(fatal) == 2
