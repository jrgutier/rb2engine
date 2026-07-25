"""verify_library: mechanical fidelity check of written m.db vs source IR.

WHY: Today the only proof a conversion is faithful is a human opening Engine DJ
and spot-checking a few tracks. A verifier that always returns ok is worthless —
these tests deliberately mutate the written database and assert each corruption
is reported. Expected discrepancies are named from the mutation applied, never
from running verify and pasting its output.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from rb2engine.ir import (
    RGB,
    CueKind,
    SourceBeat,
    SourceBeatgrid,
    SourceCue,
    SourceLibrary,
    SourcePlaylist,
    SourceTrack,
)
from rb2engine.report import ConversionReport
from rb2engine.writer.build import build_library

# ---------------------------------------------------------------------------
# Fixtures — hand-authored small library (not derived from verify output)
# ---------------------------------------------------------------------------

SAMPLE_RATE = 44100
BPM = 128.0
# Dense grid at constant tempo — mapper compresses to sparse markers.
_BEATS = [
    SourceBeat(beat_in_bar=(i % 4) + 1, sample_offset=i * 20672, bpm=BPM)
    for i in range(16)
]


def _source_track(
    rb_id: int,
    *,
    drive: Path,
    title: str,
    filename: str,
    bpm: float = BPM,
    key_name: str = "Am",
    cues: list[SourceCue] | None = None,
) -> SourceTrack:
    audio = drive / "Contents" / filename
    audio.parent.mkdir(parents=True, exist_ok=True)
    if not audio.is_file():
        audio.write_bytes(b"fake-audio-" + filename.encode())
    return SourceTrack(
        rb_id=rb_id,
        title=title,
        artist="Artist",
        album="Album",
        genre="House",
        label="Label",
        comment="",
        composer="",
        remixer="",
        year=2020,
        track_number=1,
        disc_number=None,
        bpm=bpm,
        key_name=key_name,
        rating=0,
        play_count=0,
        bitrate=320,
        file_size=1000,
        file_type="mp3",
        sample_rate=SAMPLE_RATE,
        duration_s=10,
        total_samples=SAMPLE_RATE * 10,
        raw_path=f"/Contents/{filename}",
        resolved_path=audio,
        beatgrid=SourceBeatgrid(beats=list(_BEATS), is_adjusted=False),
        cues=cues
        if cues is not None
        else [
            SourceCue(
                kind=CueKind.HOT,
                hot_slot=1,
                start_sample=44100,
                end_sample=None,
                color=RGB(255, 0, 0),
                name="Drop",
            ),
            SourceCue(
                kind=CueKind.HOT,
                hot_slot=2,
                start_sample=88200,
                end_sample=132300,
                color=RGB(0, 255, 0),
                name="Loop",
            ),
        ],
        artwork=None,
    )


def _build_fixture(tmp_path: Path) -> tuple[Path, SourceLibrary, Path]:
    """Build a 3-track library with one playlist into tmp_path; return drive, lib, m.db."""
    drive = tmp_path / "stick"
    drive.mkdir()
    (drive / "Contents").mkdir()
    (drive / "PIONEER").mkdir()

    tracks = {
        10: _source_track(10, drive=drive, title="Alpha", filename="a.mp3", bpm=120.0),
        20: _source_track(20, drive=drive, title="Beta", filename="b.mp3", bpm=128.0),
        30: _source_track(30, drive=drive, title="Gamma", filename="c.mp3", bpm=130.0),
    }
    playlists = [
        SourcePlaylist(
            rb_id=1,
            parent_rb_id=0,
            name="Main Set",
            sort_order=0,
            is_folder=False,
            track_rb_ids=[10, 20, 30],
        ),
    ]
    lib = SourceLibrary(
        drive_root=drive, tracks=tracks, playlists=playlists, warnings=[]
    )
    m_db = build_library(
        lib, drive_root=drive, report=ConversionReport(), with_artwork=False
    )
    return drive, lib, m_db


def _patch_read_library(
    monkeypatch: pytest.MonkeyPatch, lib: SourceLibrary
) -> None:
    """verify_library always re-parses the source stick; unit tests inject IR."""
    import rb2engine.verify as verify_mod

    def _read(drive_root: Path, *, with_anlz: bool = True, with_artwork: bool = True):
        del drive_root, with_anlz, with_artwork
        return lib

    monkeypatch.setattr(verify_mod, "read_library", _read)


def _fields(discrepancies: list) -> set[str]:
    return {d.field for d in discrepancies}


def _track_ids(discrepancies: list) -> set[int | None]:
    return {d.track_id for d in discrepancies}


# ---------------------------------------------------------------------------
# Happy path — a faithful build must verify clean
# ---------------------------------------------------------------------------


def test_verify_clean_build_is_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end build must produce zero discrepancies.

    WHY: If a correct conversion fails verify, operators will ignore the tool
    and go back to manual spot-checks — the entire feature collapses.
    """
    from rb2engine.verify import verify_library

    drive, lib, _m_db = _build_fixture(tmp_path)
    _patch_read_library(monkeypatch, lib)

    result = verify_library(drive, with_artwork=False)

    assert result.ok is True
    assert result.discrepancies == []
    assert result.checked == 3
    assert result.matched == 3
    assert result.mismatched == 0
    text = result.render_text()
    assert "ok" in text.lower() or "0 discrepancy" in text.lower() or "matched" in text.lower()


def test_sample_limits_tracks_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """sample=N verifies only the first N tracks (by rb_id) — USB-scale safety.

    WHY: A full 3,600-track verify over USB is slow; operators need a cheap
    smoke path that still exercises real decode+diff logic.
    """
    from rb2engine.verify import verify_library

    drive, lib, _m_db = _build_fixture(tmp_path)
    _patch_read_library(monkeypatch, lib)

    result = verify_library(drive, with_artwork=False, sample=2)

    assert result.ok is True
    assert result.checked == 2
    assert result.matched == 2


# ---------------------------------------------------------------------------
# Failure modes — prove verify can fail
# ---------------------------------------------------------------------------


def test_verify_catches_bpm_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deliberately wrong bpm in m.db must surface as a bpm discrepancy.

    WHY: BPM is the most-used harmonic-mixing field; silent drift would ship
    unmixable libraries that still 'open' fine in Engine.
    """
    from rb2engine.verify import verify_library

    drive, lib, m_db = _build_fixture(tmp_path)
    _patch_read_library(monkeypatch, lib)

    conn = sqlite3.connect(str(m_db))
    # Mutate Alpha (title known) — expected bpm remains 120 from source.
    conn.execute(
        "UPDATE Track SET bpm = 999, bpmAnalyzed = 999.0 WHERE title = ?",
        ("Alpha",),
    )
    conn.commit()
    conn.close()

    result = verify_library(drive, with_artwork=False)

    assert result.ok is False
    assert result.mismatched >= 1
    assert "bpm" in _fields(result.discrepancies) or "bpm_analyzed" in _fields(
        result.discrepancies
    )
    assert 10 in _track_ids(result.discrepancies)


def test_verify_catches_corrupt_quick_cues_blob(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Corrupting PerformanceData.quickCues must fail the cue comparison.

    WHY: Cue pad positions are the muscle-memory contract. A blob that
    decompresses to garbage (or fails to decode) is exactly the silent
    failure mode this tool exists to catch before the gig.
    """
    from rb2engine.verify import verify_library

    drive, lib, m_db = _build_fixture(tmp_path)
    _patch_read_library(monkeypatch, lib)

    conn = sqlite3.connect(str(m_db))
    row = conn.execute(
        "SELECT id FROM Track WHERE title = ?", ("Beta",)
    ).fetchone()
    assert row is not None
    track_id = row[0]
    # Replace with non-zlib garbage that decode_quick_cues cannot parse.
    conn.execute(
        "UPDATE PerformanceData SET quickCues = ? WHERE trackId = ?",
        (b"\x00\x01\x02\x03not-a-valid-quickcues-blob", track_id),
    )
    conn.commit()
    conn.close()

    result = verify_library(drive, with_artwork=False)

    assert result.ok is False
    fields = _fields(result.discrepancies)
    assert any(
        f.startswith("quick_cue") or f in {"quick_cues", "quickCues"} for f in fields
    ), fields
    assert 20 in _track_ids(result.discrepancies)


def test_verify_catches_deleted_track_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A source track missing from m.db is a discrepancy, not a crash.

    WHY: A silent skip during conversion looks exactly like this — the track
    is in the source library, gone from Engine, and no exception was raised.
    """
    from rb2engine.verify import verify_library

    drive, lib, m_db = _build_fixture(tmp_path)
    _patch_read_library(monkeypatch, lib)

    conn = sqlite3.connect(str(m_db))
    row = conn.execute(
        "SELECT id FROM Track WHERE title = ?", ("Gamma",)
    ).fetchone()
    assert row is not None
    tid = row[0]
    conn.execute("DELETE FROM PlaylistEntity WHERE trackId = ?", (tid,))
    conn.execute("DELETE FROM PerformanceData WHERE trackId = ?", (tid,))
    conn.execute("DELETE FROM Track WHERE id = ?", (tid,))
    conn.commit()
    conn.close()

    result = verify_library(drive, with_artwork=False)

    assert result.ok is False
    assert 30 in _track_ids(result.discrepancies)
    assert any(
        d.field in {"missing", "presence", "track"} for d in result.discrepancies
    ), _fields(result.discrepancies)


def test_verify_catches_playlist_chain_reorder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reordering nextEntityId must fail playlist track-order verification.

    WHY: Engine reconstructs order solely from the linked list. A wrong chain
    plays the set in the wrong order with no GUI error — the operator only
    notices mid-set.
    """
    from rb2engine.verify import verify_library

    drive, lib, m_db = _build_fixture(tmp_path)
    _patch_read_library(monkeypatch, lib)

    conn = sqlite3.connect(str(m_db))
    # Original order of engine track ids via entities for listId=1: head→…→tail.
    # Swap so the chain is no longer [Alpha, Beta, Gamma] = rb [10,20,30].
    entities = conn.execute(
        "SELECT id, trackId, nextEntityId FROM PlaylistEntity "
        "WHERE listId = (SELECT id FROM Playlist WHERE title = ?) "
        "ORDER BY id",
        ("Main Set",),
    ).fetchall()
    assert len(entities) == 3
    # entities currently: e1→e2→e3→0 with tracks t1,t2,t3.
    # Reorder to t1→t3→t2 (swap Beta and Gamma in the chain).
    e1, e2, e3 = entities[0][0], entities[1][0], entities[2][0]
    # Make head e1 → e3 → e2 → 0
    conn.execute(
        "UPDATE PlaylistEntity SET nextEntityId = ? WHERE id = ?", (e3, e1)
    )
    conn.execute(
        "UPDATE PlaylistEntity SET nextEntityId = ? WHERE id = ?", (e2, e3)
    )
    conn.execute(
        "UPDATE PlaylistEntity SET nextEntityId = 0 WHERE id = ?", (e2,)
    )
    conn.commit()
    conn.close()

    result = verify_library(drive, with_artwork=False)

    assert result.ok is False
    assert any(
        "playlist" in d.field or d.field.endswith("order") or d.field == "track_order"
        for d in result.discrepancies
    ), _fields(result.discrepancies)


def test_render_text_lists_discrepancies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """render_text must surface field names so CLI operators can act on them."""
    from rb2engine.verify import verify_library

    drive, lib, m_db = _build_fixture(tmp_path)
    _patch_read_library(monkeypatch, lib)

    conn = sqlite3.connect(str(m_db))
    conn.execute("UPDATE Track SET title = 'WRONG' WHERE title = 'Alpha'")
    conn.commit()
    conn.close()

    result = verify_library(drive, with_artwork=False)
    text = result.render_text()

    assert result.ok is False
    assert "title" in text.lower() or "Alpha" in text or "WRONG" in text
    assert "10" in text or "Alpha" in text
