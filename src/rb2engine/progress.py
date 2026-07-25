"""Terminal progress bar for the long phases of ``convert``.

WHY THIS EXISTS
---------------
A real library is thousands of tracks on slow USB media. Both halves of a
conversion open every audio file — the reader for ANLZ + embedded art, the
writer to re-extract the art bytes it stores — so a 3,665-track stick spends
minutes with nothing on screen. Silence is indistinguishable from a hang.

Design constraints this module respects:

* **stdout is reserved** for the conversion report and ``inspect --json`` so
  both stay pipeable. Progress goes to **stderr**, like every other log sink.
* **Never break machine output.** Disabled when stderr is not a TTY (piped or
  redirected) and when ``--log-json`` is on, where a ``\\r``-redrawn bar would
  corrupt the JSON-lines stream.
* **Never fail a conversion.** A write to a closed or broken stream disables
  the bar; it does not propagate. Progress is cosmetic and must stay that way.
"""

from __future__ import annotations

import shutil
import sys
from collections.abc import Callable
from types import TracebackType
from typing import IO

# phase label, completed, total. total <= 0 means "indeterminate" — the phase
# is announced but no bar is drawn, because a fake bar is worse than none.
ProgressCallback = Callable[[str, int, int], None]

# Per-item callback handed to the writer submodules; the phase name is bound
# by the caller so those modules stay unaware of presentation.
ItemCallback = Callable[[int, int], None]

FILLED = "▰"  # ▰
EMPTY = "▱"  # ▱

BAR_WIDTH = 40
_LABEL_WIDTH = 14
# label + space + bar + " 100%" — how much room the bar needs beside itself.
_CHROME = _LABEL_WIDTH + 1 + 6
_MIN_BAR_WIDTH = 8


def render_bar(done: int, total: int, *, width: int = BAR_WIDTH) -> str:
    """Render ``▰▰▰▱▱▱ 30%`` for *done* of *total*.

    Pure and side-effect free so the formatting can be tested without a
    terminal. Percentage truncates rather than rounds: 3,664 of 3,665 tracks
    reads 99%, and only a genuinely finished phase shows 100%.
    """
    pct = (100 if done > 0 else 0) if total <= 0 else min(done, total) * 100 // total
    pct = max(0, min(100, pct))
    filled = pct * width // 100
    return f"{FILLED * filled}{EMPTY * (width - filled)} {pct:3d}%"


class ProgressReporter:
    """Draws a single redrawing bar per phase on stderr.

    Call it as ``reporter(phase, done, total)``. A change of *phase* closes the
    current line and starts a new one, so a run reads as a short stack of
    completed phases with the live one at the bottom.
    """

    def __init__(
        self,
        stream: IO[str] | None = None,
        *,
        enabled: bool | None = None,
        width: int = BAR_WIDTH,
    ) -> None:
        self._stream: IO[str] = sys.stderr if stream is None else stream
        self._width = width
        self._enabled = self._detect_tty() if enabled is None else bool(enabled)
        self._phase: str | None = None
        self._last_pct: int | None = None
        self._line_open = False

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> ProgressReporter:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Terminate the open line so later output starts on its own row."""
        if self._line_open:
            self._write("\n")
            self._line_open = False
        self._phase = None
        self._last_pct = None

    # -- the callback ------------------------------------------------------

    def __call__(self, phase: str, done: int, total: int) -> None:
        if not self._enabled:
            return

        if phase != self._phase:
            if self._line_open:
                self._write("\n")
            self._phase = phase
            self._last_pct = None
            self._line_open = False

        if total <= 0:
            # Indeterminate: announce once, and do not pretend to measure it.
            if not self._line_open:
                self._write(f"{self._label(phase)} …")
                self._line_open = True
            return

        pct = int(min(done, total) * 100 // total)
        # One redraw per whole percent: 3,665 tracks become at most 101 writes,
        # which matters over a serial console or an SSH session.
        if pct == self._last_pct:
            return
        self._last_pct = pct

        bar = render_bar(done, total, width=self._bar_width())
        self._write(f"\r{self._label(phase)} {bar}")
        self._line_open = True

    # -- internals ---------------------------------------------------------

    def _label(self, phase: str) -> str:
        return phase[:_LABEL_WIDTH].ljust(_LABEL_WIDTH)

    def _bar_width(self) -> int:
        """Shrink the bar on a narrow terminal rather than wrapping the line.

        A wrapped line defeats ``\\r``: the redraw lands on the wrong row and
        leaves a trail of stale bars behind it.
        """
        try:
            columns = shutil.get_terminal_size().columns
        except (OSError, ValueError):
            return self._width
        return max(_MIN_BAR_WIDTH, min(self._width, columns - _CHROME))

    def _detect_tty(self) -> bool:
        try:
            return bool(self._stream.isatty())
        except (AttributeError, OSError, ValueError):
            return False

    def _write(self, text: str) -> None:
        """Best-effort write; a broken stream silently disables the bar.

        Progress must never be the reason a finished conversion reports
        failure, so this swallows rather than raises.
        """
        try:
            self._stream.write(text)
            self._stream.flush()
        except (OSError, ValueError):
            self._enabled = False


def phase_callback(
    on_progress: ProgressCallback | None, phase: str
) -> ItemCallback | None:
    """Bind *phase* so writer submodules report ``(done, total)`` only.

    Returns None when there is nothing to report to, which lets callers pass
    the result straight through without a second None check.
    """
    if on_progress is None:
        return None

    def report(done: int, total: int) -> None:
        on_progress(phase, done, total)

    return report
