"""Regression tests against REAL bytes captured from a rekordbox-exported stick.

WHY THIS FILE EXISTS
--------------------
`test_strings.py` and `test_paths.py` are written against the *specification*.
That is correct TDD, but a specification can be wrong — and in this project it
was, twice:

1. The research recorded `track_row.file_path` as drive-letter prefixed
   (``D:/Contents/Artist/Album/Track.mp3``). Real bytes from the user's stick
   use a leading-slash, drive-letter-free form (``/Contents/...``). The reader
   survived only because it searches for the ``Contents/`` segment instead of
   parsing a drive prefix.
2. The short-ASCII length derivation was flagged by its author as the detail
   most likely to need confirmation against real data.

These tests pin behaviour against bytes rekordbox actually wrote, so a future
"cleanup" of the decoder cannot silently break real-world parsing while the
spec-derived tests stay green. The fixture is a 348-byte slice of a real
``export.pdb``; no stick needs to be mounted to run it.
"""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath

import pytest

from rb2engine.reader.paths import resolve_track_path
from rb2engine.reader.strings import decode_device_sql_string

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "real_bytes"
BLOB = FIXTURE_DIR / "pdb_strings.bin"
MANIFEST = FIXTURE_DIR / "pdb_strings.json"


def _cases() -> list[dict]:
    return json.loads(MANIFEST.read_text())


@pytest.mark.parametrize("case", _cases(), ids=lambda c: f"{c['kind']}@{c['offset']}")
def test_decodes_real_export_pdb_strings(case: dict) -> None:
    """Decoding real rekordbox bytes must yield the exact original text.

    These offsets and expected values were captured from a real 3,039,232-byte
    `export.pdb`. The expected strings are self-evidently correct (real ANLZ
    paths and a real `/Contents/` audio path), so this is ground truth rather
    than a transcript of our own output.
    """
    data = BLOB.read_bytes()
    text, consumed = decode_device_sql_string(data, case["offset"])

    assert text == case["text"]
    assert consumed == case["consumed"]


def test_real_paths_are_leading_slash_not_drive_letter() -> None:
    """Pins the format correction: real paths carry NO drive letter.

    If someone reintroduces drive-letter parsing as a *requirement* (rather than
    as one tolerated form), this fails.
    """
    texts = [c["text"] for c in _cases()]
    contents_paths = [t for t in texts if t.startswith("/Contents/")]

    assert contents_paths, "fixture must contain at least one /Contents/ path"
    for p in contents_paths:
        assert p[1:3] != ":/", f"real path unexpectedly drive-letter prefixed: {p}"
        assert p.startswith("/Contents/")


def test_resolver_accepts_both_real_and_documented_path_forms(tmp_path: Path) -> None:
    """Both the real (leading-slash) and documented (drive-letter) forms resolve.

    WHY: the reader must not depend on which of the two rekordbox emits. This is
    the property that saved the implementation when the documented assumption
    turned out to be wrong on real hardware.
    """
    rel = PurePosixPath("Contents/Cour T_/All Stars 07/Labirinto Babylonia.m4a")
    target = tmp_path / Path(*rel.parts)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"fake audio")

    real_form = "/" + str(rel)
    documented_form = "D:/" + str(rel)

    assert resolve_track_path(real_form, tmp_path) == target
    assert resolve_track_path(documented_form, tmp_path) == target
