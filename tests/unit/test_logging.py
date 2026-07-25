"""Tests for process-wide logging sinks (rich vs JSON-lines).

WHY THIS FILE EXISTS
--------------------
Stdout is reserved for the conversion report and ``inspect --json`` so both
stay pipeable. If log lines leak onto stdout, CI parsers and shell pipelines
break silently. ``--log-json`` must emit one parseable object per line on
stderr with stage/event/detail; -v/-vv must gate info/debug. Re-configuring
must not duplicate output (no stacked handlers).
"""

from __future__ import annotations

import json

import pytest

from rb2engine.logging import configure_logging, log_event, reset_logging


@pytest.fixture(autouse=True)
def _clean_logging() -> None:
    """Isolate each test from process-global verbose/json flags."""
    reset_logging()
    yield
    reset_logging()


# ---------------------------------------------------------------------------
# Verbosity levels (-v / -vv)
# ---------------------------------------------------------------------------


def test_default_verbose_emits_warning_and_error_only(capsys: pytest.CaptureFixture[str]) -> None:
    """verbose=0 is the default CLI mode: warnings+ always, info/debug silent.

    Operators must see failures without -v; flooding info on every convert
    would bury them.
    """
    configure_logging(verbose=0, log_json=True)

    log_event("convert", "start", level="info")
    log_event("convert", "dbg", level="debug")
    log_event("convert", "problem", level="warning", detail="soft")
    log_event("convert", "fatal", level="error", detail="hard")

    err = capsys.readouterr().err
    lines = [ln for ln in err.splitlines() if ln.strip()]
    events = [json.loads(ln)["event"] for ln in lines]
    assert events == ["problem", "fatal"]


def test_verbose_1_enables_info_not_debug(capsys: pytest.CaptureFixture[str]) -> None:
    """-v (verbose=1) must emit info and still suppress debug.

    Debug is high-volume (per-track); -v is the normal progress channel.
    """
    configure_logging(verbose=1, log_json=True)

    log_event("inspect", "start", level="info")
    log_event("inspect", "byte-detail", level="debug")
    log_event("inspect", "warn-me", level="warning")

    err = capsys.readouterr().err
    events = [json.loads(ln)["event"] for ln in err.splitlines() if ln.strip()]
    assert "start" in events
    assert "warn-me" in events
    assert "byte-detail" not in events


def test_verbose_2_enables_debug(capsys: pytest.CaptureFixture[str]) -> None:
    """-vv (verbose=2+) must emit debug events for deep diagnostics."""
    configure_logging(verbose=2, log_json=True)

    log_event("anlz", "tag", level="debug", detail={"tag": "PQTZ"})
    log_event("anlz", "ok", level="info")

    err = capsys.readouterr().err
    events = [json.loads(ln)["event"] for ln in err.splitlines() if ln.strip()]
    assert events == ["tag", "ok"]


def test_warn_alias_always_emits_at_verbose_0(capsys: pytest.CaptureFixture[str]) -> None:
    """level='warn' is accepted as warning so call sites do not silently drop."""
    configure_logging(verbose=0, log_json=True)
    log_event("pdb", "soft", level="warn", detail=1)
    payload = json.loads(capsys.readouterr().err.strip())
    assert payload["event"] == "soft"
    assert payload["level"] == "warn"


# ---------------------------------------------------------------------------
# JSON lines on stderr; stdout reserved for report/inspect
# ---------------------------------------------------------------------------


def test_log_json_emits_one_parseable_object_per_line_on_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--log-json: each event is exactly one JSON object line on stderr.

    Machine consumers (CI, log shippers) require line-delimited JSON — not
    pretty-printed multi-line blobs, not mixed prose.
    """
    configure_logging(verbose=2, log_json=True)

    log_event("reader", "scan", track_id=None, detail={"files": 3}, level="info")
    log_event("reader", "track", track_id=42, detail="ok", level="debug")

    captured = capsys.readouterr()
    assert captured.out == "", "stdout must stay empty for pipeable report/inspect"
    lines = [ln for ln in captured.err.splitlines() if ln.strip()]
    assert len(lines) == 2
    for line in lines:
        obj = json.loads(line)  # raises if not a single JSON value
        assert isinstance(obj, dict)


def test_log_json_stdout_stays_empty_even_with_multiple_events(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Regression: logging must never write to stdout (report/inspect own it)."""
    configure_logging(verbose=1, log_json=True)
    for i in range(5):
        log_event("stage", f"e{i}", track_id=i, detail=i, level="info")

    out, err = capsys.readouterr()
    assert out == ""
    assert err  # something went to stderr
    assert all(json.loads(ln) for ln in err.splitlines() if ln.strip())


def test_log_event_includes_stage_event_detail_track_id(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Structured fields are the contract for --log-json consumers.

    Missing stage/event/detail forces every tool to re-parse free text.
    """
    configure_logging(verbose=1, log_json=True)
    log_event(
        "mapper",
        "cue_dropped",
        track_id=99,
        detail={"reason": "slot_out_of_range", "slot": 9},
        level="warning",
    )

    payload = json.loads(capsys.readouterr().err.strip())
    assert payload["stage"] == "mapper"
    assert payload["event"] == "cue_dropped"
    assert payload["track_id"] == 99
    assert payload["detail"] == {"reason": "slot_out_of_range", "slot": 9}
    assert payload["level"] == "warning"


def test_log_event_detail_none_and_track_id_null_in_json(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Optional fields stay present as null so schema is stable for parsers."""
    configure_logging(verbose=1, log_json=True)
    log_event("cli", "start", level="info")

    payload = json.loads(capsys.readouterr().err.strip())
    assert payload["track_id"] is None
    assert payload["detail"] is None
    assert "stage" in payload and "event" in payload


# ---------------------------------------------------------------------------
# Human-readable path still on stderr
# ---------------------------------------------------------------------------


def test_human_mode_writes_to_stderr_not_stdout(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Default human logs also reserve stdout for report/inspect JSON.

    rich may treat ``[stage]`` as markup, so we assert on event/detail text
    and stream separation — not on the bracketed stage token surviving.
    """
    configure_logging(verbose=1, log_json=False)
    log_event("inspect", "start", track_id=1, detail="drive=/tmp/x", level="info")

    out, err = capsys.readouterr()
    assert out == ""
    assert err  # something landed on stderr
    assert "start" in err
    assert "drive=/tmp/x" in err


# ---------------------------------------------------------------------------
# Configure twice — no duplicated output / handlers
# ---------------------------------------------------------------------------


def test_configure_logging_twice_does_not_duplicate_output(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Calling configure_logging twice must not stack sinks.

    If each configure added a handler, every event would print twice and
    JSONL consumers would see duplicate records — a classic logging footgun.
    This module uses process flags rather than handler lists; reconfigure
    must still yield exactly one line per log_event.
    """
    configure_logging(verbose=1, log_json=True)
    configure_logging(verbose=1, log_json=True)

    log_event("convert", "once", track_id=1, detail="x", level="info")

    lines = [ln for ln in capsys.readouterr().err.splitlines() if ln.strip()]
    assert len(lines) == 1
    assert json.loads(lines[0])["event"] == "once"


def test_reconfigure_updates_verbose_and_json_flags(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Second configure replaces settings — does not layer them."""
    configure_logging(verbose=0, log_json=True)
    log_event("a", "silent-info", level="info")
    assert capsys.readouterr().err == ""

    configure_logging(verbose=2, log_json=True)
    log_event("a", "now-visible", level="debug")
    err = capsys.readouterr().err
    assert json.loads(err.strip())["event"] == "now-visible"


def test_reset_logging_restores_quiet_defaults(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """reset_logging is the test/helper path back to verbose=0, json off."""
    configure_logging(verbose=2, log_json=True)
    reset_logging()

    log_event("x", "info-hidden", level="info")
    log_event("x", "debug-hidden", level="debug")
    log_event("x", "warn-shown", level="warning")

    # Human mode after reset (log_json False) — still on stderr.
    err = capsys.readouterr().err
    assert "info-hidden" not in err
    assert "debug-hidden" not in err
    assert "warn-shown" in err


def test_log_event_does_not_print_when_filtered(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Filtered levels must produce zero bytes on both streams."""
    configure_logging(verbose=0, log_json=True)
    log_event("s", "e", level="info")
    log_event("s", "e", level="debug")
    out, err = capsys.readouterr()
    assert out == ""
    assert err == ""


def test_json_detail_non_serializable_uses_default_str(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """detail may contain Path-like values; logging must not raise."""
    configure_logging(verbose=1, log_json=True)
    log_event("paths", "resolve", detail=object(), level="info")
    payload = json.loads(capsys.readouterr().err.strip())
    assert payload["event"] == "resolve"
    assert isinstance(payload["detail"], str)
