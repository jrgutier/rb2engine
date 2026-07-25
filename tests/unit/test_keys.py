"""Tests for mapper/keys.py — Engine musical_key ordinals.

WHY: Engine stores Track.key as 0–23 (circle-of-fifths, relative minors interleaved).
A wrong ordinal silently ruins harmonic mixing on the deck. Unknown input must return
None (never a guess); aliases must collapse to the libdjinterop-confirmed ordinals.
"""

from __future__ import annotations

import warnings
from typing import ClassVar

import pytest

from rb2engine.mapper.keys import key_name_to_ordinal

# Confirmed libdjinterop musical_key order (engine Track.key / trackData.key).
CANONICAL: list[tuple[str, int]] = [
    ("C major", 0),
    ("A minor", 1),
    ("G major", 2),
    ("E minor", 3),
    ("D major", 4),
    ("B minor", 5),
    ("A major", 6),
    ("F# minor", 7),
    ("E major", 8),
    ("Db minor", 9),
    ("B major", 10),
    ("Ab minor", 11),
    ("F# major", 12),
    ("Eb minor", 13),
    ("Db major", 14),
    ("Bb minor", 15),
    ("Ab major", 16),
    ("F minor", 17),
    ("Eb major", 18),
    ("C minor", 19),
    ("Bb major", 20),
    ("G minor", 21),
    ("F major", 22),
    ("D minor", 23),
]


class TestCanonicalNames:
    """All 24 Engine ordinals must map from their canonical names.

    WHY: These are the ground-truth labels from libdjinterop; if any drifts, every
    harmonic-mix feature in Engine will be off by a fifth or mode.
    """

    @pytest.mark.parametrize(("name", "ordinal"), CANONICAL)
    def test_canonical_name_maps_to_confirmed_ordinal(
        self, name: str, ordinal: int
    ) -> None:
        assert key_name_to_ordinal(name) == ordinal


class TestEnharmonicEquivalents:
    """Enharmonic spellings must share one ordinal — never invent a 25th key.

    WHY: rekordbox may emit C# major while Engine's table spells it Db major;
    treating them as different would leave half the library unmapped.
    """

    @pytest.mark.parametrize(
        ("a", "b", "ordinal"),
        [
            ("Db major", "C# major", 14),
            ("Db minor", "C# minor", 9),
            ("Eb major", "D# major", 18),
            ("Eb minor", "D# minor", 13),
            ("F# major", "Gb major", 12),
            ("F# minor", "Gb minor", 7),
            ("Ab major", "G# major", 16),
            ("Ab minor", "G# minor", 11),
            ("Bb major", "A# major", 20),
            ("Bb minor", "A# minor", 15),
        ],
    )
    def test_enharmonic_pair_same_ordinal(
        self, a: str, b: str, ordinal: int
    ) -> None:
        assert key_name_to_ordinal(a) == ordinal
        assert key_name_to_ordinal(b) == ordinal
        assert key_name_to_ordinal(a) == key_name_to_ordinal(b)


class TestShortFormsAndSuffixes:
    """rekordbox short forms (Am, F#m, Amin, Amaj) must parse.

    WHY: export.pdb and UI labels use compact spellings far more often than
    'A minor'; rejecting them would zero out most Track.key values.
    """

    @pytest.mark.parametrize(
        ("name", "ordinal"),
        [
            ("C", 0),
            ("Am", 1),
            ("G", 2),
            ("Em", 3),
            ("F#m", 7),
            ("F#", 12),
            ("Dbm", 9),
            ("Db", 14),
            ("Amin", 1),
            ("A min", 1),
            ("Aminor", 1),
            ("Amaj", 6),
            ("A maj", 6),
            ("Amajor", 6),
            ("Cm", 19),
            ("FM", 22),  # bare-letter major; trailing M is major suffix
            ("Fm", 17),
        ],
    )
    def test_short_and_suffixed_forms(self, name: str, ordinal: int) -> None:
        assert key_name_to_ordinal(name) == ordinal


class TestUnicodeAccidentals:
    """Unicode ♯/♭ (as libdjinterop prints them) must parse.

    WHY: some tools and libdjinterop's own ostream use ♯/♭; treating those as
    unknown would drop keys that are already in Engine form.
    """

    @pytest.mark.parametrize(
        ("name", "ordinal"),
        [
            ("F♯ minor", 7),
            ("F♯m", 7),
            ("F♯", 12),
            ("D♭ major", 14),
            ("D♭m", 9),
            ("A♭ minor", 11),
            ("E♭", 18),
            ("B♭m", 15),
        ],
    )
    def test_unicode_sharp_flat(self, name: str, ordinal: int) -> None:
        assert key_name_to_ordinal(name) == ordinal


class TestCaseAndWhitespace:
    """Case and surrounding whitespace must not change the ordinal.

    WHY: pdb strings and UI paste differ in case/spacing; harmonic identity does not.
    """

    @pytest.mark.parametrize(
        ("name", "ordinal"),
        [
            ("  c major  ", 0),
            ("a MINOR", 1),
            ("f#m", 7),
            ("  8A  ", 1),  # Camelot A minor
            ("\tDb\tmajor\n", 14),
        ],
    )
    def test_case_and_whitespace_normalized(
        self, name: str, ordinal: int
    ) -> None:
        assert key_name_to_ordinal(name) == ordinal


class TestCamelotWheel:
    """Full Camelot wheel (1A–12A, 1B–12B) must map onto the 24 ordinals.

    WHY: Mixed-In-Key / rekordbox Camelot display is what many DJs store as the
    key string; missing any spoke breaks harmonic mixing for that half of the wheel.
    """

    # Camelot B = major, A = minor. Alignment: 8B=C major, 8A=A minor.
    CAMELOT: ClassVar[list[tuple[str, int]]] = [
        ("8B", 0),
        ("8A", 1),
        ("9B", 2),
        ("9A", 3),
        ("10B", 4),
        ("10A", 5),
        ("11B", 6),
        ("11A", 7),
        ("12B", 8),
        ("12A", 9),
        ("1B", 10),
        ("1A", 11),
        ("2B", 12),
        ("2A", 13),
        ("3B", 14),
        ("3A", 15),
        ("4B", 16),
        ("4A", 17),
        ("5B", 18),
        ("5A", 19),
        ("6B", 20),
        ("6A", 21),
        ("7B", 22),
        ("7A", 23),
    ]

    @pytest.mark.parametrize(("name", "ordinal"), CAMELOT)
    def test_camelot_full_wheel(self, name: str, ordinal: int) -> None:
        assert key_name_to_ordinal(name) == ordinal
        assert key_name_to_ordinal(name.lower()) == ordinal


class TestOpenKeyNotation:
    """Open Key (1d–12d / 1m–12m) must map onto the same 24 ordinals.

    WHY: Open Key is another common export spelling; 1d is C major (ordinal 0),
    matching Engine's circle-of-fifths layout exactly.
    """

    OPEN_KEY: ClassVar[list[tuple[str, int]]] = [
        ("1d", 0),
        ("1m", 1),
        ("2d", 2),
        ("2m", 3),
        ("3d", 4),
        ("3m", 5),
        ("4d", 6),
        ("4m", 7),
        ("5d", 8),
        ("5m", 9),
        ("6d", 10),
        ("6m", 11),
        ("7d", 12),
        ("7m", 13),
        ("8d", 14),
        ("8m", 15),
        ("9d", 16),
        ("9m", 17),
        ("10d", 18),
        ("10m", 19),
        ("11d", 20),
        ("11m", 21),
        ("12d", 22),
        ("12m", 23),
    ]

    @pytest.mark.parametrize(("name", "ordinal"), OPEN_KEY)
    def test_open_key_full_wheel(self, name: str, ordinal: int) -> None:
        assert key_name_to_ordinal(name) == ordinal


class TestUnknownInput:
    """Unparseable keys must return None and warn — never invent an ordinal.

    WHY: A wrong key silently breaks harmonic mixing; None lets the writer leave
    Track.key NULL and increment keys_unmapped instead of lying to the deck.
    """

    @pytest.mark.parametrize(
        "name",
        [
            "",
            "   ",
            "not-a-key",
            "H major",  # German H is not accepted; we do not guess → B
            "13A",  # Camelot out of range
            "0B",
            "13d",
            "0m",
            "X",
            "major",
            "8C",  # Camelot only A/B
        ],
    )
    def test_unknown_returns_none_and_warns(self, name: str) -> None:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = key_name_to_ordinal(name)
        assert result is None
        assert any(issubclass(w.category, UserWarning) for w in caught)
