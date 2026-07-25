"""Tests for the ir / ir_engine decoupling boundary.

WHY these tests exist (not just what they check):

- Frozen dataclasses stop accidental mutation of shared library state as data
  flows reader → mapper → writer. Mutation bugs would be silent and racey.
- Positions are sample counts; `is_loop` is the single source of truth for the
  U3 "loop frees pad" policy — wrong truth → loops steal pads mid-set.
- `to_json_obj()` path canonicalization is what makes `golden_ir.json` byte-
  identical across macOS/Windows/Linux. Without it, absolute paths and
  OS-native separators break CI on at least two of three platforms.
- The artwork dedup key must strip leading zero hex digits so keys match the
  shape Engine stores (observed 39-char values). Wrong stripping → false
  non-dedup or golden fixture mismatch at authoring time.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

import rb2engine.ir_engine as ir_engine_mod
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
from rb2engine.ir_engine import artwork_content_hash

# ---------------------------------------------------------------------------
# Immutability
# ---------------------------------------------------------------------------


def test_source_cue_is_frozen() -> None:
    """Shared IR must not be mutated after construction (pipeline safety)."""
    cue = SourceCue(
        kind=CueKind.HOT,
        hot_slot=1,
        start_sample=100,
        end_sample=None,
        color=RGB(255, 0, 0),
        name="Intro",
    )
    with pytest.raises(FrozenInstanceError):
        cue.start_sample = 200  # type: ignore[misc]


def test_source_library_is_frozen() -> None:
    lib = SourceLibrary(
        drive_root=Path("/Volumes/USB"),
        tracks={},
        playlists=[],
        warnings=[],
    )
    with pytest.raises(FrozenInstanceError):
        lib.drive_root = Path("/other")  # type: ignore[misc]


# ---------------------------------------------------------------------------
# SourceCue.is_loop — U3 routing hinge
# ---------------------------------------------------------------------------


def test_is_loop_false_when_end_sample_none() -> None:
    """Point cue: must route to pads, not loop slots."""
    cue = SourceCue(
        kind=CueKind.HOT,
        hot_slot=1,
        start_sample=0,
        end_sample=None,
        color=None,
        name=None,
    )
    assert cue.is_loop is False


def test_is_loop_true_when_end_sample_set() -> None:
    """Saved loop: end_sample is the only loop signal (U3 frees the pad)."""
    cue = SourceCue(
        kind=CueKind.MEMORY,
        hot_slot=None,
        start_sample=1000,
        end_sample=5000,
        color=None,
        name="8-bar",
    )
    assert cue.is_loop is True


# ---------------------------------------------------------------------------
# JSON path canonicalization — golden_ir.json cross-platform pin
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
            beats=[
                SourceBeat(beat_in_bar=1, sample_offset=0, bpm=128.0),
            ],
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
            SourceArtwork(
                content_key="abc",
                path=art_path,
                source="embedded",
            )
            if art_path is not None
            else None
        ),
    )


def test_to_json_obj_identical_for_different_drive_roots(tmp_path: Path) -> None:
    """Two drive_root values must produce byte-identical JSON.

    WHY: CI runners on mac/win/linux mount fixtures at different absolute
    paths with different separators. If absolute paths leak into the
    serialization, golden_ir.json cannot pin across platforms.
    """
    root_a = tmp_path / "stick_a"
    root_b = tmp_path / "other" / "stick_b"
    rel_audio = Path("Contents") / "Artist" / "track.mp3"
    rel_art = Path("PIONEER") / "Artwork" / "cover.jpg"

    for root in (root_a, root_b):
        (root / rel_audio).parent.mkdir(parents=True, exist_ok=True)
        (root / rel_art).parent.mkdir(parents=True, exist_ok=True)

    lib_a = SourceLibrary(
        drive_root=root_a,
        tracks={
            1: _minimal_track(
                rb_id=1,
                resolved=root_a / rel_audio,
                art_path=root_a / rel_art,
            ),
        },
        playlists=[
            SourcePlaylist(
                rb_id=10,
                parent_rb_id=0,
                name="Root",
                sort_order=0,
                is_folder=True,
                track_rb_ids=[],
            ),
        ],
        warnings=["example"],
    )
    lib_b = SourceLibrary(
        drive_root=root_b,
        tracks={
            1: _minimal_track(
                rb_id=1,
                resolved=root_b / rel_audio,
                art_path=root_b / rel_art,
            ),
        },
        playlists=[
            SourcePlaylist(
                rb_id=10,
                parent_rb_id=0,
                name="Root",
                sort_order=0,
                is_folder=True,
                track_rb_ids=[],
            ),
        ],
        warnings=["example"],
    )

    json_a = json.dumps(lib_a.to_json_obj(), separators=(",", ":"), ensure_ascii=False)
    json_b = json.dumps(lib_b.to_json_obj(), separators=(",", ":"), ensure_ascii=False)
    assert json_a == json_b


def test_to_json_obj_elides_drive_root() -> None:
    """drive_root absolute path is runner-specific; emit a stable placeholder."""
    lib = SourceLibrary(
        drive_root=Path("/Volumes/USB DISK"),
        tracks={},
        playlists=[],
        warnings=[],
    )
    obj = lib.to_json_obj()
    assert obj["drive_root"] == "<drive_root>"
    assert "/Volumes" not in json.dumps(obj)


def test_to_json_obj_external_path_canary() -> None:
    """Path outside drive_root is a resolver bug → emit <external>, never leak abs."""
    drive = Path("/media/stick")
    lib = SourceLibrary(
        drive_root=drive,
        tracks={
            1: _minimal_track(
                rb_id=1,
                resolved=Path("/tmp/elsewhere/track.mp3"),
                art_path=None,
            ),
        },
        playlists=[],
        warnings=[],
    )
    obj = lib.to_json_obj()
    track = obj["tracks"]["1"]
    assert track["resolved_path"] == "<external>"
    dumped = json.dumps(obj)
    assert "/tmp/elsewhere" not in dumped
    assert "\\" not in dumped


def test_to_json_obj_paths_are_drive_relative_posix() -> None:
    """Under drive_root: relative, forward slashes, no drive letter, no leading /."""
    drive = Path("/media/stick")
    lib = SourceLibrary(
        drive_root=drive,
        tracks={
            1: _minimal_track(
                rb_id=1,
                resolved=drive / "Contents" / "A" / "t.mp3",
                art_path=drive / "PIONEER" / "Artwork" / "x.jpg",
            ),
        },
        playlists=[],
        warnings=[],
    )
    obj = lib.to_json_obj()
    track = obj["tracks"]["1"]
    assert track["resolved_path"] == "Contents/A/t.mp3"
    assert track["artwork"]["path"] == "PIONEER/Artwork/x.jpg"
    # raw_path is the original rekordbox string (may contain "D:/…"); only
    # pathlib fields are canonicalized for cross-platform golden identity.
    for field in ("resolved_path", "artwork"):
        fragment = (
            track["resolved_path"]
            if field == "resolved_path"
            else track["artwork"]["path"]
        )
        assert fragment is not None
        assert not fragment.startswith("/")
        assert "\\" not in fragment
        assert not (len(fragment) >= 2 and fragment[1] == ":")
    dumped = json.dumps(obj)
    assert '"/media/' not in dumped  # absolute drive_root must not leak


def test_to_json_obj_deterministic_key_order() -> None:
    """Key order must be stable so golden dumps are byte-comparable without sorting."""
    drive = Path("/d")
    lib = SourceLibrary(
        drive_root=drive,
        tracks={
            1: _minimal_track(rb_id=1, resolved=drive / "Contents" / "t.mp3"),
        },
        playlists=[
            SourcePlaylist(
                rb_id=1,
                parent_rb_id=0,
                name="P",
                sort_order=0,
                is_folder=False,
                track_rb_ids=[1],
            ),
        ],
        warnings=[],
    )
    obj = lib.to_json_obj()
    # Top-level order is fixed by the serialization contract.
    assert list(obj.keys()) == ["drive_root", "tracks", "playlists", "warnings"]
    track_keys = list(obj["tracks"]["1"].keys())
    # First keys must be stable; full list is the contract field order.
    assert track_keys[0] == "rb_id"
    assert track_keys[1] == "title"
    assert "resolved_path" in track_keys
    # Re-serializing twice yields identical key order in JSON text.
    a = json.dumps(obj, separators=(",", ":"))
    b = json.dumps(lib.to_json_obj(), separators=(",", ":"))
    assert a == b


def test_to_json_obj_round_trip_shape() -> None:
    """Small SourceLibrary → to_json_obj preserves structure and sample positions."""
    drive = Path("/stick")
    lib = SourceLibrary(
        drive_root=drive,
        tracks={
            42: _minimal_track(
                rb_id=42,
                resolved=drive / "Contents" / "x.mp3",
                art_path=drive / "art.jpg",
            ),
        },
        playlists=[
            SourcePlaylist(
                rb_id=7,
                parent_rb_id=0,
                name="Set",
                sort_order=1,
                is_folder=False,
                track_rb_ids=[42],
            ),
        ],
        warnings=["warn1"],
    )
    obj = lib.to_json_obj()
    assert obj["drive_root"] == "<drive_root>"
    assert obj["warnings"] == ["warn1"]
    assert obj["playlists"][0]["track_rb_ids"] == [42]
    t = obj["tracks"]["42"]
    assert t["rb_id"] == 42
    assert t["sample_rate"] == 44100
    assert t["cues"][0]["start_sample"] == 44100
    assert t["cues"][0]["kind"] == "HOT"
    assert t["cues"][0]["color"] == {"r": 0, "g": 255, "b": 0}
    assert t["beatgrid"]["beats"][0]["sample_offset"] == 0
    assert t["artwork"]["source"] == "embedded"
    assert t["artwork"]["content_key"] == "abc"
    assert t["resolved_path"] == "Contents/x.mp3"
    # JSON-serializable (no Path / Enum left)
    json.dumps(obj)


# ---------------------------------------------------------------------------
# Artwork content-key helper (Engine-shaped sha1, leading zeros stripped)
# ---------------------------------------------------------------------------


def test_artwork_content_hash_known_vector() -> None:
    """sha1('abc') is a well-known vector; no leading zero to strip."""
    # https://www.nist.gov — classic SHA-1 test vector for "abc"
    expected_full = "a9993e364706816aba3e25717850c26c9cd0d89d"
    assert hashlib.sha1(b"abc").hexdigest() == expected_full
    assert artwork_content_hash(b"abc") == expected_full


def test_artwork_content_hash_strips_leading_zero_digits() -> None:
    """Leading hex zeros must be stripped so keys match Engine's unpadded shape.

    WHY: Engine stores a 39-char value (40-char sha1 with a leading '0' removed).
    AUTHORING.md / golden_mapped_ir.json hand-author with the same rule. If we
    keep zero-padding, ~6% of covers disagree with authored goldens and with
    Engine's observed hash length.
    """
    # Brute-found 4-byte input whose sha1 hex starts with '0':
    payload = b"\x02\x00\x00\x00"
    full = hashlib.sha1(payload).hexdigest()
    assert full.startswith("0"), "fixture pre-condition: digest must start with 0"
    assert len(full) == 40
    stripped = full.lstrip("0") or "0"
    assert len(stripped) == 39
    assert artwork_content_hash(payload) == stripped
    assert artwork_content_hash(payload) == "aaf76f425c6e0f43a36197de768e67d9e035abb"


def test_artwork_content_hash_all_zeros_edge(monkeypatch: pytest.MonkeyPatch) -> None:
    """An all-zero digest must yield "0", never the empty string.

    WHY: the key is `digest.lstrip("0")`, so a digest of all zero digits would
    strip to "" and collide with every other degenerate value — and an empty
    AlbumArt.hash would silently break dedup. No real input produces such a
    sha1, so the guard is only reachable by substituting the digest. Without
    this, the `or "0"` branch is untested.
    """

    class _FakeDigest:
        def hexdigest(self) -> str:
            return "0" * 40

    monkeypatch.setattr(
        ir_engine_mod.hashlib, "sha1", lambda *_a, **_k: _FakeDigest()
    )

    assert artwork_content_hash(b"anything") == "0"
