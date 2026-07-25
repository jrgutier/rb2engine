"""rekordbox key name → Engine musical_key ordinal 0–23 (plus aliases).

Engine stores Track.key / trackData.key as int 0–23 in circle-of-fifths order
with relative minors interleaved (libdjinterop musical_key). Unknown input
returns None and emits a UserWarning — never guess; a wrong key silently
ruins harmonic mixing.
"""

from __future__ import annotations

import re
import warnings

# Confirmed from libdjinterop musical_key enum (c_major=0 … d_minor=23).
# Canonical accidentals: F# (sharp), Db/Eb/Ab/Bb (flat) — Engine's spellings.
_CANONICAL_ORDINAL: dict[tuple[str, str], int] = {
    ("C", "major"): 0,
    ("A", "minor"): 1,
    ("G", "major"): 2,
    ("E", "minor"): 3,
    ("D", "major"): 4,
    ("B", "minor"): 5,
    ("A", "major"): 6,
    ("F#", "minor"): 7,
    ("E", "major"): 8,
    ("Db", "minor"): 9,
    ("B", "major"): 10,
    ("Ab", "minor"): 11,
    ("F#", "major"): 12,
    ("Eb", "minor"): 13,
    ("Db", "major"): 14,
    ("Bb", "minor"): 15,
    ("Ab", "major"): 16,
    ("F", "minor"): 17,
    ("Eb", "major"): 18,
    ("C", "minor"): 19,
    ("Bb", "major"): 20,
    ("G", "minor"): 21,
    ("F", "major"): 22,
    ("D", "minor"): 23,
}

# Map any enharmonic spelling onto the canonical note used above.
# Flat keys use an uppercased "B" accidental token (e.g. "DB" → "Db").
_NOTE_TO_CANONICAL: dict[str, str] = {
    "C": "C",
    "B#": "C",
    "C#": "Db",
    "DB": "Db",
    "D": "D",
    "D#": "Eb",
    "EB": "Eb",
    "E": "E",
    "FB": "E",
    "F": "F",
    "E#": "F",
    "F#": "F#",
    "GB": "F#",
    "G": "G",
    "G#": "Ab",
    "AB": "Ab",
    "A": "A",
    "A#": "Bb",
    "BB": "Bb",
    "B": "B",
    "CB": "B",
}

# Camelot: 8B=C major (0), 8A=A minor (1). Open Key: 1d=C major, 1m=A minor.
_CAMELOT_RE = re.compile(r"^(\d{1,2})\s*([AB])$", re.IGNORECASE)
_OPEN_KEY_RE = re.compile(r"^(\d{1,2})\s*([DM])$", re.IGNORECASE)

# Note + optional accidental + optional mode. Trailing m/M is case-significant
# (m=minor, M=major); maj/min/major/minor are case-insensitive.
_NOTE_RE = re.compile(
    r"^"
    r"([A-G])"
    r"([#b])?"
    r"(?:"
    r"(major|minor|maj|min)"
    r"|"
    r"([mM])"
    r")?"
    r"$",
    re.IGNORECASE,
)


def _warn_unmapped(name: str) -> None:
    warnings.warn(
        f"unmapped musical key: {name!r}",
        UserWarning,
        stacklevel=3,
    )


def _from_open_key_number(num: int, *, is_minor: bool) -> int | None:
    """Open Key / Camelot share the same circle-of-fifths index 1..12."""
    if not 1 <= num <= 12:
        return None
    return 2 * (num - 1) + (1 if is_minor else 0)


def _from_camelot(num: int, letter: str) -> int | None:
    if not 1 <= num <= 12:
        return None
    # Camelot 8 → Open Key 1, Camelot 9 → 2, …, Camelot 7 → 12.
    open_key = ((num - 1 + 5) % 12) + 1
    return _from_open_key_number(open_key, is_minor=letter.upper() == "A")


def _parse_note_mode(compact: str) -> tuple[str, str] | None:
    """Return (canonical_note, mode) or None."""
    m = _NOTE_RE.fullmatch(compact)
    if m is None:
        return None

    letter = m.group(1).upper()
    acc = m.group(2) or ""
    mode_word = m.group(3)
    single_m = m.group(4)

    if acc == "#":
        table_key = letter + "#"
    elif acc.lower() == "b":
        table_key = letter + "B"
    else:
        table_key = letter

    canonical_note = _NOTE_TO_CANONICAL.get(table_key)
    if canonical_note is None:
        return None

    if mode_word is not None:
        mw = mode_word.lower()
        if mw in ("major", "maj"):
            mode = "major"
        elif mw in ("minor", "min"):
            mode = "minor"
        else:
            return None
    elif single_m is not None:
        mode = "minor" if single_m == "m" else "major"
    else:
        mode = "major"

    return canonical_note, mode


def key_name_to_ordinal(name: str) -> int | None:
    """Map a rekordbox (or Camelot / Open Key) key string to Engine ordinal 0–23.

    Returns None and warns if the string cannot be parsed confidently.
    """
    if not isinstance(name, str):
        _warn_unmapped(repr(name))
        return None

    raw = name.strip()
    if not raw:
        _warn_unmapped(name)
        return None

    m = _CAMELOT_RE.fullmatch(raw)
    if m:
        ordinal = _from_camelot(int(m.group(1)), m.group(2))
        if ordinal is None:
            _warn_unmapped(name)
            return None
        return ordinal

    m = _OPEN_KEY_RE.fullmatch(raw)
    if m:
        letter = m.group(2)
        is_minor = letter.lower() == "m"
        if letter.lower() not in ("d", "m"):
            _warn_unmapped(name)
            return None
        ordinal = _from_open_key_number(int(m.group(1)), is_minor=is_minor)
        if ordinal is None:
            _warn_unmapped(name)
            return None
        return ordinal

    # Unicode accidentals → ASCII; drop internal whitespace ("A minor" → "Aminor").
    normalized = (
        raw.replace("♯", "#")
        .replace("♭", "b")
        .replace("＃", "#")
    )
    compact = re.sub(r"\s+", "", normalized)

    parsed = _parse_note_mode(compact)
    if parsed is None:
        _warn_unmapped(name)
        return None

    note, mode = parsed
    ordinal = _CANONICAL_ORDINAL.get((note, mode))
    if ordinal is None:
        _warn_unmapped(name)
        return None
    return ordinal
