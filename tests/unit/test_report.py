"""Tests for ConversionReport: schema, sinks, exit codes, report location.

WHY these tests exist (not just what they check):

- The report is the substrate for integration assertions (skipped tracks,
  dropped cues/loops). A loose schema would let silent regressions ship.
- Exit codes 0/1/2 are the only machine-readable success contract for
  scripts and CI; wrong mapping hides partial failures.
- Report files must land inside ``Engine Library/`` (or cwd fallback), never
  the drive root — otherwise ``test_nondestructive`` fails and we violate
  the non-destructive guarantee that nothing new appears outside that dir.
- Machine-stable ``reason_code`` values are what automation keys on; human
  text alone is not enough.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from rb2engine.report import (
    REPORT_FILENAME,
    REPORT_SCHEMA,
    ConversionReport,
    exit_code_for,
    resolve_report_path,
    validate_report,
)

# ---------------------------------------------------------------------------
# Schema contract
# ---------------------------------------------------------------------------


def test_report_schema_is_json_schema_object() -> None:
    """Schema must be a usable JSON Schema document (type object + properties)."""
    assert REPORT_SCHEMA["type"] == "object"
    assert "properties" in REPORT_SCHEMA
    required = set(REPORT_SCHEMA["required"])
    for key in (
        "counters",
        "skipped_tracks",
        "dropped_cues",
        "dropped_loops",
    ):
        assert key in required, f"mandated section {key!r} missing from schema required"


def test_empty_report_validates_against_schema() -> None:
    """A fresh report must already be schema-valid (all mandated sections present)."""
    report = ConversionReport()
    obj = report.to_json_obj()
    validate_report(obj)  # raises on failure
    for section in ("counters", "skipped_tracks", "dropped_cues", "dropped_loops"):
        assert section in obj


def test_populated_report_validates_and_has_reason_codes() -> None:
    """Skips and drops must carry machine-stable reason_code for automation."""
    report = ConversionReport()
    report.counters.tracks_read = 3
    report.counters.tracks_converted = 2
    report.counters.tracks_skipped = 1
    report.counters.playlists_read = 1
    report.counters.playlists_converted = 1
    report.counters.cues_dropped = 1
    report.counters.loops_dropped = 1
    report.counters.tracks_unresolvable_paths = 1
    report.counters.tracks_no_anlz = 0
    report.counters.artwork_found = 1
    report.counters.artwork_missing = 1
    report.counters.artwork_deduped = 0
    report.counters.unknown_tag_encounters = 0
    report.counters.unknown_page_type_encounters = 1
    report.counters.export_ext_present = True

    report.add_skip(
        track_id=42,
        reason_code="path_unresolvable",
        message="Could not resolve Contents path",
        title="Missing File",
    )
    report.add_dropped_cue(
        track_id=7,
        reason_code="pad_slots_exhausted",
        start_sample=44100,
        end_sample=None,
        name="Overflow Cue",
    )
    report.add_dropped_loop(
        track_id=7,
        reason_code="loop_slots_exhausted",
        start_sample=88200,
        end_sample=176400,
        name="Overflow Loop",
    )

    obj = report.to_json_obj()
    validate_report(obj)

    assert obj["skipped_tracks"][0]["reason_code"] == "path_unresolvable"
    assert obj["dropped_cues"]["7"][0]["reason_code"] == "pad_slots_exhausted"
    assert obj["dropped_loops"]["7"][0]["reason_code"] == "loop_slots_exhausted"
    assert obj["counters"]["export_ext_present"] is True


def test_validate_report_rejects_missing_counters() -> None:
    """Schema validation must fail when a mandated section is absent."""
    bad: dict[str, Any] = {
        "skipped_tracks": [],
        "dropped_cues": {},
        "dropped_loops": {},
    }
    with pytest.raises(ValueError, match="counters"):
        validate_report(bad)


# ---------------------------------------------------------------------------
# Exit-code semantics (defined in report; applied by cli)
# ---------------------------------------------------------------------------


def test_exit_code_clean_is_zero() -> None:
    """Clean conversion with no skips → 0 so scripts treat success as success."""
    report = ConversionReport()
    report.counters.tracks_converted = 10
    assert exit_code_for(report) == 0


def test_exit_code_with_skips_is_one() -> None:
    """Any track skip → 1 so CI can distinguish partial from full success."""
    report = ConversionReport()
    report.counters.tracks_converted = 9
    report.add_skip(track_id=1, reason_code="corrupt_anlz", message="bad ANLZ")
    assert exit_code_for(report) == 1


def test_exit_code_fatal_is_two() -> None:
    """Fatal → 2 means nothing usable was written; must not look like success."""
    report = ConversionReport()
    report.mark_fatal("no PIONEER export found")
    assert exit_code_for(report) == 2


def test_exit_code_fatal_dominates_skips() -> None:
    """Fatal beats skips: 2, not 1, when nothing usable was written."""
    report = ConversionReport()
    report.add_skip(track_id=1, reason_code="x", message="y")
    report.mark_fatal("disk full")
    assert exit_code_for(report) == 2


def test_dropped_cues_alone_do_not_force_exit_one() -> None:
    """Cue overflow is itemized but the track still converted → clean exit 0.

    WHY: exit 1 is for track-level skips / soft failures, not for intentional
    pad-slot policy drops. Treating drops as exit 1 would make every full
    library report a partial failure.
    """
    report = ConversionReport()
    report.counters.tracks_converted = 1
    report.add_dropped_cue(
        track_id=1,
        reason_code="pad_slots_exhausted",
        start_sample=0,
        end_sample=None,
        name=None,
    )
    assert exit_code_for(report) == 0


# ---------------------------------------------------------------------------
# Report path: Engine Library/, never drive root; cwd fallback
# ---------------------------------------------------------------------------


def test_resolve_report_path_inside_engine_library(tmp_path: Path) -> None:
    """Default path must be under Engine Library/ so nondestructive tests pass."""
    drive = tmp_path / "stick"
    eng = drive / "Engine Library"
    eng.mkdir(parents=True)
    path = resolve_report_path(drive, library_ready=True)
    assert path == eng / REPORT_FILENAME
    assert path.parent.name == "Engine Library"
    # Never the drive root
    assert path.parent != drive


def test_resolve_report_path_cwd_fallback_when_library_not_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fatal / pre-library failure falls back to cwd, not drive root."""
    drive = tmp_path / "stick"
    drive.mkdir()
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)

    path = resolve_report_path(drive, library_ready=False)
    assert path == cwd / REPORT_FILENAME
    assert path.parent == cwd
    # Must not write beside PIONEER/ on the drive root
    assert path != drive / REPORT_FILENAME


def test_resolve_report_path_cwd_fallback_when_engine_lib_unwritable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unwritable Engine Library/ → cwd, never drive root."""
    drive = tmp_path / "stick"
    eng = drive / "Engine Library"
    eng.mkdir(parents=True)
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)

    # Simulate unwritable by making the directory non-writable when possible.
    # On some CI users may be root; also force via monkeypatch of access check.
    real_access = os.access

    def fake_access(path: Any, mode: int, *args: Any, **kwargs: Any) -> bool:
        if Path(path) == eng and mode == os.W_OK:
            return False
        return real_access(path, mode, *args, **kwargs)

    monkeypatch.setattr(os, "access", fake_access)

    path = resolve_report_path(drive, library_ready=True)
    assert path == cwd / REPORT_FILENAME


def test_resolve_report_path_override(tmp_path: Path) -> None:
    """--report PATH must win over the default location."""
    drive = tmp_path / "stick"
    (drive / "Engine Library").mkdir(parents=True)
    override = tmp_path / "custom-report.json"
    path = resolve_report_path(drive, override=override, library_ready=True)
    assert path == override


def test_write_json_creates_file_and_is_valid(tmp_path: Path) -> None:
    """JSON sink writes schema-valid report content."""
    report = ConversionReport()
    report.counters.tracks_read = 1
    out = tmp_path / "Engine Library" / REPORT_FILENAME
    out.parent.mkdir()
    written = report.write_json(out)
    assert written == out
    data = json.loads(out.read_text(encoding="utf-8"))
    validate_report(data)


def test_render_text_mentions_counts() -> None:
    """Human sink must surface the counters operators care about."""
    report = ConversionReport()
    report.counters.tracks_converted = 5
    report.counters.tracks_skipped = 1
    report.counters.playlists_converted = 2
    report.add_skip(track_id=9, reason_code="no_anlz", message="missing ANLZ")
    text = report.render_text()
    assert "5" in text
    assert "skipped" in text.lower() or "Skip" in text
    assert "9" in text or "no_anlz" in text
