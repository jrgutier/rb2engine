"""Unit tests for source artwork extraction (reader/artwork.py).

WHY this module exists: Engine stores album art as BLOBs keyed by a content
hash. On the measured stick PIONEER/Artwork/ is empty, so embedded mutagen
tags are the primary source. Wrong dedup order breaks AlbumArt AUTOINCREMENT
ids and therefore every Track.albumArtId in the determinism dump. A mutagen
save() would rewrite the user's audio — the non-destructive guarantee is a
hard acceptance criterion.
"""

from __future__ import annotations

import base64
import hashlib
import os
import shutil
import warnings
from pathlib import Path

import pytest
from mutagen.id3 import APIC
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Cover

from rb2engine.ir_engine import artwork_content_hash
from rb2engine.reader.artwork import (
    SOURCE_EMBEDDED,
    SOURCE_PDB,
    ArtworkIndex,
    collect_artwork,
    extract_artwork,
    extract_pdb_artwork,
    read_artwork_bytes,
)

# Silent ~0.1s bases produced once with ffmpeg; embedded only so tests need no
# ffmpeg at runtime. Not Engine-authored — fixture scaffolding only.
_BASE_MP3 = base64.b64decode(
    "SUQzBAAAAAAAIlRTU0UAAAAOAAADTGF2ZjYyLjMuMTAwAAAAAAAAAAAAAAD/+0DAAAAAAAAAAAAA"
    "AAAAAAAAAABJbmZvAAAADwAAAAUAAATKAFFRUVFRUVFRUVFRUVFRUVFRUVF9fX19fX19fX19fX19"
    "fX19fX19faioqKioqKioqKioqKioqKioqKio1NTU1NTU1NTU1NTU1NTU1NTU1NT/////////////"
    "/////////////wAAAABMYXZjNjIuMTEAAAAAAAAAAAAAAAAkAwYAAAAAAAAEyqvNp2YAAAAAAP/7"
    "UMQAA8AAAaQAAAAgAAA0gAAABExBTUUzLjEwMFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVMQU1FMy4xMDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVX/+1LEXYPAAAGkAAAAIAAANIAAAARV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVUxBTUUz"
    "LjEwMFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVf/7UsShg8AAAaQAAAAgAAA0gAAABFVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVTEFNRTMuMTAwVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//tS"
    "xKGDwAABpAAAACAAADSAAAAEVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVX/+1LEoYPAAAGkAAAAIAAANIAAAARV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVQ=="
)
_BASE_M4A = base64.b64decode(
    "AAAAHGZ0eXBNNEEgAAACAE00QSBpc29taXNvMgAAAAhmcmVlAAAAMW1kYXTeAgBMYXZjNjIuMTEu"
    "MTAwAAIwQA4BGCAHARggBwEYIAcBGCAHARggBwAAAxJtb292AAAAbG12aGQAAAAAAAAAAAAAAAAA"
    "AAPoAAAAZAABAAABAAAAAAAAAAAAAAAAAQAAAAAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAEAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAACAAACPXRyYWsAAABcdGtoZAAAAAMAAAAAAAAA"
    "AAAAAAEAAAAAAAAAZAAAAAAAAAAAAAAAAQEAAAAAAQAAAAAAAAAAAAAAAAAAAAEAAAAAAAAAAAAA"
    "AAAAAEAAAAAAAAAAAAAAAAAAACRlZHRzAAAAHGVsc3QAAAAAAAAAAQAAAGQAAAQAAAEAAAAAAbVt"
    "ZGlhAAAAIG1kaGQAAAAAAAAAAAAAAAAAAKxEAAAVOlXEAAAAAAAtaGRscgAAAAAAAAAAc291bgAA"
    "AAAAAAAAAAAAAFNvdW5kSGFuZGxlcgAAAAFgbWluZgAAABBzbWhkAAAAAAAAAAAAAAAkZGluZgAA"
    "ABxkcmVmAAAAAAAAAAEAAAAMdXJsIAAAAAEAAAEkc3RibAAAAGpzdHNkAAAAAAAAAAEAAABabXA0"
    "YQAAAAAAAAABAAAAAAAAAAAAAQAQAAAAAKxEAAAAAAA2ZXNkcwAAAAADgICAJQABAASAgIAXQBUA"
    "AAAAAPoAAAAKZQWAgIAFEghW5QAGgICAAQIAAAAgc3R0cwAAAAAAAAACAAAABQAABAAAAAABAAAB"
    "OgAAABxzdHNjAAAAAAAAAAEAAAABAAAABgAAAAEAAAAsc3RzegAAAAAAAAAAAAAABgAAABUAAAAE"
    "AAAABAAAAAQAAAAEAAAABAAAABRzdGNvAAAAAAAAAAEAAAAsAAAAGnNncGQBAAAAcm9sbAAAAAIA"
    "AAAB//8AAAAcc2JncAAAAAByb2xsAAAAAQAAAAYAAAABAAAAYXVkdGEAAABZbWV0YQAAAAAAAAAh"
    "aGRscgAAAAAAAAAAbWRpcmFwcGwAAAAAAAAAAAAAAAAsaWxzdAAAACSpdG9vAAAAHGRhdGEAAAAB"
    "AAAAAExhdmY2Mi4zLjEwMA=="
)

# 1×1 PNG (69 bytes) — hand-known magic; content_key from the shared hash helper.
_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVQI12P4//8/AAX+Av6nNYGE"
    "AAAAAElFTkSuQmCC"
)
_PNG_1X1_KEY = artwork_content_hash(_PNG_1X1)

_GOLDEN_PNG = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "golden"
    / "albumart_engine.png"
)


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _mp3_with_apic(path: Path, image: bytes, *, mime: str = "image/png") -> Path:
    path.write_bytes(_BASE_MP3)
    audio = MP3(path)
    if audio.tags is None:
        audio.add_tags()
    assert audio.tags is not None
    audio.tags.delall("APIC")
    audio.tags.add(
        APIC(
            encoding=3,
            mime=mime,
            type=3,
            desc="Cover",
            data=image,
        )
    )
    audio.save()
    return path


def _m4a_with_covr(path: Path, image: bytes) -> Path:
    path.write_bytes(_BASE_M4A)
    audio = MP4(path)
    fmt = (
        MP4Cover.FORMAT_PNG
        if image.startswith(b"\x89PNG")
        else MP4Cover.FORMAT_JPEG
    )
    audio["covr"] = [MP4Cover(image, imageformat=fmt)]
    audio.save()
    return path


def _mp3_no_art(path: Path) -> Path:
    path.write_bytes(_BASE_MP3)
    return path


# ---------------------------------------------------------------------------
# extract_artwork — single-track primary path
# ---------------------------------------------------------------------------


def test_extract_mp3_apic_returns_content_key_and_embedded_source(
    tmp_path: Path,
) -> None:
    """ID3 APIC is the real-stick path for .mp3; key must match image bytes.

    WHY: if we hashed the audio file or a wrong frame, shared album covers
    would not dedup and AlbumArt would bloat one row per track.
    """
    track = _mp3_with_apic(tmp_path / "track.mp3", _PNG_1X1)

    art = extract_artwork(track)

    assert art is not None
    assert art.content_key == _PNG_1X1_KEY
    assert art.source == SOURCE_EMBEDDED
    assert art.path == track
    assert read_artwork_bytes(art) == _PNG_1X1


def test_extract_m4a_covr_returns_content_key(tmp_path: Path) -> None:
    """MP4 `covr` is the dominant format on the measured stick (~m4a heavy)."""
    track = _m4a_with_covr(tmp_path / "track.m4a", _PNG_1X1)

    art = extract_artwork(track)

    assert art is not None
    assert art.content_key == _PNG_1X1_KEY
    assert art.source == SOURCE_EMBEDDED
    assert read_artwork_bytes(art) == _PNG_1X1


def test_extract_returns_none_when_no_embedded_art(tmp_path: Path) -> None:
    """Missing art is common and must be None — never an error or exception.

    WHY: t05_overflow in the fixture plan has no cover; a raised error would
    abort conversion of an otherwise valid library.
    """
    track = _mp3_no_art(tmp_path / "plain.mp3")

    assert extract_artwork(track) is None


def test_extract_does_not_modify_source_audio_bytes(tmp_path: Path) -> None:
    """Non-destructive guarantee in miniature: no mutagen save, no rewrite.

    WHY: the acceptance criterion forbids writing to the user's audio. A
    casual tags.save() rewrites the file even when tags are unchanged.
    """
    track = _m4a_with_covr(tmp_path / "guard.m4a", _PNG_1X1)
    before = _file_sha256(track)

    extract_artwork(track)

    assert _file_sha256(track) == before


def test_extract_corrupt_image_data_warns_and_returns_none(tmp_path: Path) -> None:
    """Garbage in APIC must skip the track's art, not crash the run.

    WHY: a single corrupt frame must not abort a 3,665-track conversion.
    """
    track = _mp3_with_apic(
        tmp_path / "corrupt.mp3",
        b"this-is-not-image-bytes",
        mime="image/jpeg",
    )

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        art = extract_artwork(track)

    assert art is None
    assert any(
        "corrupt" in str(w.message).lower() or "undecodable" in str(w.message).lower()
        for w in caught
    )


def test_extract_non_ascii_path(tmp_path: Path) -> None:
    """DJ libraries use non-ASCII artist/album directory names on the stick."""
    folder = tmp_path / "アーティスト" / "アルバム"
    folder.mkdir(parents=True)
    track = _mp3_with_apic(folder / "曲.mp3", _PNG_1X1)

    art = extract_artwork(track)

    assert art is not None
    assert art.content_key == _PNG_1X1_KEY


def test_extract_engine_golden_png_embedded(tmp_path: Path) -> None:
    """Round-trip the Engine-authored PNG bytes through an embedded frame.

    WHY: the golden blob is what lands in AlbumArt; extraction must preserve
    those exact bytes so the writer can assert byte-equality later.
    """
    golden = _GOLDEN_PNG.read_bytes()
    # Size is incidental — the fixture is a synthetic PNG (the original was a
    # commercial cover, removed before publishing). What matters is that the
    # exact bytes survive extraction.
    assert golden.startswith(b"\x89PNG\r\n\x1a\n")
    track = _mp3_with_apic(tmp_path / "golden.mp3", golden)

    art = extract_artwork(track)

    assert art is not None
    assert art.content_key == artwork_content_hash(golden)
    assert read_artwork_bytes(art) == golden


# ---------------------------------------------------------------------------
# Secondary: PIONEER/Artwork/ file path
# ---------------------------------------------------------------------------


def test_extract_pdb_artwork_from_image_file(tmp_path: Path) -> None:
    """Secondary path for sticks that do populate PIONEER/Artwork/.

    WHY: this user's stick is empty there, but other exports may differ; both
    sources must be supported without depending on either alone.
    """
    img = tmp_path / "PIONEER" / "Artwork" / "000" / "1.jpg"
    img.parent.mkdir(parents=True)
    # Use golden PNG regardless of extension — bytes matter, not suffix.
    img.write_bytes(_PNG_1X1)

    art = extract_pdb_artwork(img)

    assert art is not None
    assert art.content_key == _PNG_1X1_KEY
    assert art.source == SOURCE_PDB
    assert art.path == img
    assert read_artwork_bytes(art) == _PNG_1X1


def test_extract_pdb_artwork_missing_file_returns_none(tmp_path: Path) -> None:
    missing = tmp_path / "nope.png"
    with warnings.catch_warnings(record=True):
        warnings.simplefilter("always")
        assert extract_pdb_artwork(missing) is None


# ---------------------------------------------------------------------------
# Batch + dedup — first-seen order is load-bearing for AUTOINCREMENT ids
# ---------------------------------------------------------------------------


def test_collect_dedupes_identical_art_first_seen_order(tmp_path: Path) -> None:
    """Two tracks, byte-identical cover → one unique entry; first track wins path.

    WHY: AlbumArt.id is AUTOINCREMENT in insertion order. Determinism tests
    compare canonical dumps; if second-seen overwrote first, albumArtId values
    would shuffle across runs whenever track iteration order is fixed but
    path retention is not.
    """
    t1 = _mp3_with_apic(tmp_path / "a.mp3", _PNG_1X1)
    t2 = _mp3_with_apic(tmp_path / "b.mp3", _PNG_1X1)
    t3 = _mp3_no_art(tmp_path / "c.mp3")

    index = collect_artwork([t1, t2, t3])

    assert isinstance(index, ArtworkIndex)
    assert len(index.unique) == 1
    assert index.unique[0].content_key == _PNG_1X1_KEY
    assert index.unique[0].path == t1  # first-seen path retained
    assert index.by_track[t1] is index.unique[0]
    assert index.by_track[t2] is index.unique[0]
    assert index.by_track[t3] is None
    assert index.report.tracks_seen == 3
    assert index.report.tracks_with_embedded == 2
    assert index.report.tracks_with_neither == 1
    assert index.report.unique_images == 1
    assert index.report.total_unique_bytes == len(_PNG_1X1)


def test_collect_distinct_covers_preserve_iteration_order(tmp_path: Path) -> None:
    """Distinct images appear in unique[] in the order tracks were iterated."""
    golden = _GOLDEN_PNG.read_bytes()
    t_plain = _mp3_no_art(tmp_path / "0.mp3")
    t_a = _mp3_with_apic(tmp_path / "1.mp3", golden)
    t_b = _mp3_with_apic(tmp_path / "2.mp3", _PNG_1X1)
    t_a2 = _mp3_with_apic(tmp_path / "3.mp3", golden)  # dedup of t_a

    index = collect_artwork([t_plain, t_a, t_b, t_a2])

    assert [a.content_key for a in index.unique] == [
        artwork_content_hash(golden),
        _PNG_1X1_KEY,
    ]
    assert index.by_track[t_a2] is index.unique[0]
    assert index.report.unique_images == 2
    assert index.report.total_unique_bytes == len(golden) + len(_PNG_1X1)


def test_collect_prefers_embedded_over_pdb(tmp_path: Path) -> None:
    """Primary is embedded; pdb is only used when embedded is absent."""
    emb_img = _PNG_1X1
    pdb_img = _GOLDEN_PNG.read_bytes()
    track = _mp3_with_apic(tmp_path / "both.mp3", emb_img)
    pdb_file = tmp_path / "art.png"
    pdb_file.write_bytes(pdb_img)

    index = collect_artwork(
        [track],
        pdb_artwork_by_track={track: pdb_file},
    )

    assert index.by_track[track] is not None
    assert index.by_track[track].source == SOURCE_EMBEDDED
    assert index.by_track[track].content_key == _PNG_1X1_KEY
    assert index.report.tracks_with_embedded == 1
    assert index.report.tracks_with_pdb == 0


def test_collect_falls_back_to_pdb_when_no_embedded(tmp_path: Path) -> None:
    """Secondary source is used when mutagen finds no picture frames."""
    track = _mp3_no_art(tmp_path / "no_emb.mp3")
    pdb_file = tmp_path / "cover.png"
    pdb_file.write_bytes(_PNG_1X1)

    index = collect_artwork(
        [track],
        pdb_artwork_by_track={track: pdb_file},
    )

    assert index.by_track[track] is not None
    assert index.by_track[track].source == SOURCE_PDB
    assert index.report.tracks_with_pdb == 1
    assert index.report.tracks_with_embedded == 0
    assert index.report.assigned_from_pdb == 1


def test_collect_report_probe_fields_for_m3(tmp_path: Path) -> None:
    """M3 / conversion report need source availability, not just a final count."""
    t1 = _m4a_with_covr(tmp_path / "e.m4a", _PNG_1X1)
    t2 = _mp3_no_art(tmp_path / "n.mp3")

    index = collect_artwork([t1, t2])
    r = index.report

    assert r.tracks_seen == 2
    assert r.tracks_with_embedded == 1
    assert r.tracks_with_neither == 1
    assert r.assigned_from_embedded == 1
    assert r.unique_images == 1
    assert r.total_unique_bytes == len(_PNG_1X1)


# ---------------------------------------------------------------------------
# Real stick probe — measurement for plan D4 artwork volume re-cost
# ---------------------------------------------------------------------------


@pytest.mark.real_stick
def test_real_stick_embedded_art_sample(tmp_path: Path) -> None:
    """Sample real USB tracks: copy then open (stick is READ-ONLY).

    Reports hit rate + average image size so the plan can re-cost artwork
    volume. Earlier "~79 KB/image" came from Engine's iTunes mirror (wrong
    corpus). Skipped when the stick is not mounted.
    """
    stick = Path("/Volumes/USB DISK")
    contents = stick / "Contents"
    if not contents.is_dir():
        pytest.skip("real stick not mounted at /Volumes/USB DISK")

    # Collect a modest sample of real audio paths (skip AppleDouble).
    candidates: list[Path] = []
    for root, _dirs, files in os.walk(contents):
        for name in files:
            if name.startswith("._"):
                continue
            lower = name.lower()
            if lower.endswith((".m4a", ".mp3", ".flac", ".aiff", ".aif", ".wav")):
                candidates.append(Path(root) / name)
        if len(candidates) >= 400:
            break

    if not candidates:
        pytest.skip("no audio files under Contents/")

    # Deterministic subsample for stable runtime.
    sample = candidates[:: max(1, len(candidates) // 40)][:40]
    work = tmp_path / "copies"
    work.mkdir()

    with_art = 0
    sizes: list[int] = []
    for i, src in enumerate(sample):
        dest = work / f"{i}{src.suffix.lower()}"
        shutil.copy2(src, dest)
        before = _file_sha256(dest)
        art = extract_artwork(dest)
        assert _file_sha256(dest) == before, f"non-destructive violated on {src}"
        if art is not None:
            with_art += 1
            data = read_artwork_bytes(art)
            assert data is not None
            sizes.append(len(data))
            assert art.content_key == artwork_content_hash(data)
            assert art.source == SOURCE_EMBEDDED

    rate = with_art / len(sample)
    avg = sum(sizes) / len(sizes) if sizes else 0.0
    # Visible in pytest -s / captured stdout for the plan's D4 re-cost.
    print(
        f"\nREAL_STICK_ARTWORK_PROBE: sample={len(sample)} "
        f"with_embedded={with_art} rate={rate:.1%} "
        f"avg_bytes={avg:.0f} "
        f"min={min(sizes) if sizes else 0} max={max(sizes) if sizes else 0}"
    )
    # Sanity: the measured stick is art-rich; a zero hit rate means the
    # extractor is broken on real files, not that the library has no covers.
    assert with_art > 0, "expected at least one embedded cover on this stick"
