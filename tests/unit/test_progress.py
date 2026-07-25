"""Progress reporting for the long phases of ``convert``.

WHY THIS EXISTS
---------------
A 3,665-track stick takes minutes, and both halves of the conversion open every
audio file over USB. With no output, a running conversion and a hung one look
identical, and the honest response to that is to kill it. These tests exist to
keep the feedback trustworthy, which means three separate obligations:

1. The bar must be *correct* — a percentage that reaches 100 before the work
   does is worse than no bar, because it converts "slow" into "stuck".
2. It must never *corrupt machine output*. stdout carries the report and
   ``inspect --json``; ``--log-json`` owns stderr. A ``\\r``-redrawn bar in
   either stream breaks a pipeline that used to work.
3. It must never *fail a conversion*. Progress is cosmetic; a closed stream or
   a narrow terminal must not turn a completed 44 GB conversion into an error.

The wiring tests assert the callback reaches the phases that actually consume
the wall-clock. Testing only the renderer would leave the feature able to
regress into a bar that is perfectly formatted and never drawn.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from rb2engine.progress import (
    EMPTY,
    FILLED,
    ProgressReporter,
    phase_callback,
    render_bar,
)


class _FakeTTY(io.StringIO):
    """StringIO that claims to be a terminal, so the bar enables itself."""

    def isatty(self) -> bool:
        return True


# ---------------------------------------------------------------------------
# render_bar — the pure formatting layer
# ---------------------------------------------------------------------------


def test_renders_the_requested_shape() -> None:
    """The glyphs and layout the user asked for, pinned exactly."""
    assert render_bar(30, 100, width=40) == (
        "▰▰▰▰▰▰▰▰▰▰▰▰▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱  30%"
    )


@pytest.mark.parametrize(
    ("done", "total", "pct"),
    [(0, 100, 0), (1, 100, 1), (50, 100, 50), (99, 100, 99), (100, 100, 100)],
)
def test_percentage_tracks_completion(done: int, total: int, pct: int) -> None:
    assert render_bar(done, total, width=40).endswith(f"{pct:3d}%")


def test_bar_is_always_exactly_width_cells() -> None:
    """A bar that changes length mid-run smears across the redrawn line."""
    for done in range(101):
        bar = render_bar(done, 100, width=40)
        assert bar.count(FILLED) + bar.count(EMPTY) == 40


def test_percentage_truncates_so_100_means_finished() -> None:
    """3,664 of 3,665 must read 99%, not 100%.

    Rounding would park the bar at 100% for the last several seconds of every
    phase — precisely the moment the user is deciding whether it has hung.
    """
    assert render_bar(3664, 3665, width=40).endswith(" 99%")
    assert render_bar(3665, 3665, width=40).endswith("100%")


def test_full_bar_is_entirely_filled() -> None:
    bar = render_bar(7, 7, width=40)
    assert bar.count(FILLED) == 40
    assert EMPTY not in bar


def test_empty_bar_has_no_filled_cells() -> None:
    assert FILLED not in render_bar(0, 10, width=40)


def test_overshoot_is_clamped_not_wrapped() -> None:
    """A miscounted caller must not produce 143% or a bar longer than width."""
    bar = render_bar(500, 100, width=40)
    assert bar.endswith("100%")
    assert bar.count(FILLED) == 40


def test_zero_total_does_not_divide_by_zero() -> None:
    """An empty library is a legitimate input, not a crash."""
    assert render_bar(0, 0, width=40).endswith("  0%")


# ---------------------------------------------------------------------------
# ProgressReporter — when it draws, and when it must stay silent
# ---------------------------------------------------------------------------


def test_writes_nothing_when_stream_is_not_a_tty() -> None:
    """Redirected output must stay clean: `convert 2> log` is not a terminal.

    A ``\\r`` bar in a log file produces one unreadable mega-line.
    """
    stream = io.StringIO()  # plain StringIO.isatty() is False
    reporter = ProgressReporter(stream)
    for i in range(101):
        reporter("reading tracks", i, 100)
    reporter.close()
    assert stream.getvalue() == ""


def test_writes_when_stream_is_a_tty() -> None:
    stream = _FakeTTY()
    reporter = ProgressReporter(stream)
    reporter("reading tracks", 30, 100)
    out = stream.getvalue()
    assert FILLED in out
    assert "30%" in out
    assert "reading tracks" in out


def test_explicit_enabled_false_beats_a_tty() -> None:
    """The CLI passes enabled=False for --log-json; a TTY must not override it."""
    stream = _FakeTTY()
    reporter = ProgressReporter(stream, enabled=False)
    reporter("reading tracks", 30, 100)
    reporter.close()
    assert stream.getvalue() == ""


def test_redraws_at_most_once_per_percent() -> None:
    """3,665 tracks must not become 3,665 escape-sequence writes.

    Over SSH or a serial console the redraws themselves become the bottleneck,
    so the bar would slow down the conversion it is reporting on.
    """
    stream = _FakeTTY()
    reporter = ProgressReporter(stream)
    for i in range(3666):
        reporter("reading tracks", i, 3665)
    # One line per distinct whole percent, 0..100.
    assert stream.getvalue().count("\r") <= 101


def test_every_percent_is_actually_reached() -> None:
    """Throttling must not overshoot into "only redraw twice"."""
    stream = _FakeTTY()
    reporter = ProgressReporter(stream)
    for i in range(1001):
        reporter("reading tracks", i, 1000)
    assert stream.getvalue().count("\r") == 101


def test_changing_phase_breaks_the_line() -> None:
    """Each phase keeps its own completed line instead of overwriting the last.

    Without the newline the finished "reading tracks" bar is erased by
    "album art", so the user cannot see what already succeeded.
    """
    stream = _FakeTTY()
    reporter = ProgressReporter(stream)
    reporter("reading tracks", 100, 100)
    reporter("album art", 1, 100)
    out = stream.getvalue()
    assert "\n" in out
    assert out.index("reading tracks") < out.index("\n") < out.index("album art")


def test_close_terminates_the_open_line() -> None:
    """The next thing printed is the report; it must not land on the bar."""
    stream = _FakeTTY()
    reporter = ProgressReporter(stream)
    reporter("reading tracks", 50, 100)
    assert not stream.getvalue().endswith("\n")
    reporter.close()
    assert stream.getvalue().endswith("\n")


def test_close_is_idempotent_and_safe_with_no_output() -> None:
    stream = _FakeTTY()
    reporter = ProgressReporter(stream)
    reporter.close()
    reporter.close()
    assert stream.getvalue() == ""


def test_indeterminate_phase_announces_without_a_bar() -> None:
    """copyfile of a half-gigabyte database reports nothing it could measure.

    Showing a 0% bar that never moves reads as a hang; a plain label does not.
    """
    stream = _FakeTTY()
    reporter = ProgressReporter(stream)
    reporter("publishing", 0, 0)
    reporter("publishing", 0, 0)
    out = stream.getvalue()
    assert "publishing" in out
    assert FILLED not in out
    assert "%" not in out
    assert out.count("publishing") == 1  # announced once, not per call


def test_a_broken_stream_never_raises() -> None:
    """Progress must not be the reason a finished conversion reports failure."""

    class Exploding(_FakeTTY):
        def write(self, s: str) -> int:
            raise OSError("stream closed")

    reporter = ProgressReporter(Exploding())
    reporter("reading tracks", 1, 100)  # must not raise
    reporter("reading tracks", 2, 100)
    reporter.close()


def test_narrow_terminal_does_not_wrap_the_line(monkeypatch: pytest.MonkeyPatch) -> None:
    """A wrapped line defeats \\r — the redraw lands on the wrong row.

    The result is a screen full of stale bars rather than one moving bar.
    """
    import os
    import shutil as shutil_mod

    monkeypatch.setattr(
        shutil_mod, "get_terminal_size", lambda: os.terminal_size((40, 24))
    )
    stream = _FakeTTY()
    reporter = ProgressReporter(stream)
    reporter("reading tracks", 50, 100)
    line = stream.getvalue().lstrip("\r")
    assert len(line) <= 40


def test_phase_callback_binds_the_phase_name() -> None:
    seen: list[tuple[str, int, int]] = []
    cb = phase_callback(lambda p, d, t: seen.append((p, d, t)), "album art")
    assert cb is not None
    cb(3, 10)
    assert seen == [("album art", 3, 10)]


def test_phase_callback_of_none_is_none() -> None:
    """Lets callers pass the result straight through without a second check."""
    assert phase_callback(None, "album art") is None


# ---------------------------------------------------------------------------
# Wiring — the callback must reach the phases that cost the wall-clock
# ---------------------------------------------------------------------------


def _record() -> tuple[list[tuple[str, int, int]], object]:
    events: list[tuple[str, int, int]] = []

    def cb(phase: str, done: int, total: int) -> None:
        events.append((phase, done, total))

    return events, cb


def test_read_library_reports_progress_per_track(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reader's per-track loop is the ~144 s phase on a real stick.

    Asserted through read_library rather than by inspecting the loop, so the
    test survives a refactor of how the join is implemented.
    """
    from tests.unit.helpers_progress import make_fake_stick, patch_pdb_parse

    make_fake_stick(tmp_path, n_tracks=3)
    patch_pdb_parse(monkeypatch, tmp_path, 3)
    events, cb = _record()

    from rb2engine.reader.library import read_library

    read_library(tmp_path, with_anlz=False, with_artwork=True, on_progress=cb)  # type: ignore[arg-type]

    track_events = [e for e in events if e[0] == "reading tracks"]
    assert track_events, "the per-track loop reported nothing"
    # Monotonic and finishing at the total, so the bar cannot stall or overshoot.
    counts = [done for _, done, _ in track_events]
    assert counts == sorted(counts)
    assert track_events[-1][1] == track_events[-1][2] == 3


def test_read_library_without_callback_still_works(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """on_progress is optional; every existing caller passes nothing."""
    from tests.unit.helpers_progress import make_fake_stick, patch_pdb_parse

    make_fake_stick(tmp_path, n_tracks=2)
    patch_pdb_parse(monkeypatch, tmp_path, 2)
    from rb2engine.reader.library import read_library

    lib = read_library(tmp_path, with_anlz=False, with_artwork=False)
    assert len(lib.tracks) == 2


def test_convert_draws_a_bar_on_a_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: `rb2engine convert` on a TTY must actually show the bar.

    This is the assertion that would have caught the original complaint. Every
    test above can pass while convert never constructs a reporter at all.
    """
    from tests.unit.helpers_progress import run_convert_capturing_stderr

    out = run_convert_capturing_stderr(tmp_path, monkeypatch, isatty=True, args=[])
    assert FILLED in out or EMPTY in out


def test_convert_is_silent_when_stderr_is_redirected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.unit.helpers_progress import run_convert_capturing_stderr

    out = run_convert_capturing_stderr(tmp_path, monkeypatch, isatty=False, args=[])
    assert FILLED not in out
    assert EMPTY not in out


def test_convert_draws_nothing_under_log_json_even_on_a_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--log-json owns stderr; a ``\\r`` bar there breaks every consumer of it.

    The TTY check alone is not enough — someone running ``--log-json`` at an
    interactive prompt still gets a terminal. This is a true differential
    against ``test_convert_draws_a_bar_on_a_terminal``: identical setup, the
    only difference is the flag, and the output must go from drawn to empty.
    """
    from tests.unit.helpers_progress import run_convert_capturing_stderr

    out = run_convert_capturing_stderr(
        tmp_path, monkeypatch, isatty=True, args=["--log-json"]
    )
    assert out == ""
