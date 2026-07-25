"""Logging sinks: rich human-readable vs JSON-lines structured events.

Stdout is reserved for the conversion report and ``inspect --json`` so both
stay pipeable. All log events go to stderr.
"""

from __future__ import annotations

import json
import sys
from typing import Any

_verbose: int = 0
_log_json: bool = False
_configured: bool = False


def configure_logging(*, verbose: int = 0, log_json: bool = False) -> None:
    """Configure process-wide logging style.

    *verbose*: 0 = warnings+, 1 = info (-v), 2+ = debug (-vv).
    *log_json*: one JSON object per line on stderr with stage/track_id/event/detail.
    """
    global _verbose, _log_json, _configured
    _verbose = max(0, int(verbose))
    _log_json = bool(log_json)
    _configured = True


def reset_logging() -> None:
    """Test helper: restore defaults."""
    global _verbose, _log_json, _configured
    _verbose = 0
    _log_json = False
    _configured = False


def log_event(
    stage: str,
    event: str,
    *,
    track_id: int | None = None,
    detail: Any = None,
    level: str = "info",
) -> None:
    """Emit a structured or human log line to **stderr**.

    Levels: ``error`` always, ``warning`` always, ``info`` at -v, ``debug`` at -vv.
    """
    if not _should_emit(level):
        return

    if _log_json:
        payload: dict[str, Any] = {
            "stage": stage,
            "track_id": track_id,
            "event": event,
            "detail": detail,
            "level": level,
        }
        sys.stderr.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
        return

    # Human-readable via rich when available
    parts = [f"[{stage}]"]
    if track_id is not None:
        parts.append(f"track={track_id}")
    parts.append(event)
    if detail is not None:
        parts.append(f"— {detail}")
    line = " ".join(parts)
    try:
        from rich.console import Console

        Console(stderr=True).print(line)
    except (OSError, TypeError, ValueError):
        sys.stderr.write(line + "\n")


def _should_emit(level: str) -> bool:
    level = level.lower()
    if level in ("error", "warning", "warn"):
        return True
    if level == "info":
        return _verbose >= 1
    if level == "debug":
        return _verbose >= 2
    return _verbose >= 1
