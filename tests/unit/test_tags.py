"""Unit tests for built-in embedded-cover extraction (reader/tags.py).

WHY: mutagen is GPL and must leave the runtime dependency set before public
MIT release. Extraction must stay byte-identical to what mutagen reads so
AlbumArt BLOBs and content hashes do not silently change. Mutagen remains
the test-only oracle for writing fixtures and for expected bytes.
"""

from __future__ import annotations

import base64
import struct
import warnings
from pathlib import Path

import pytest

mutagen = pytest.importorskip("mutagen")
from mutagen.id3 import APIC
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Cover

from rb2engine.reader.tags import embedded_cover_bytes

# Silent ~0.1s bases (same scaffolding as test_artwork.py). Not Engine-authored.
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

# 1×1 PNG (69 bytes)
_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVQI12P4//8/AAX+Av6nNYGE"
    "AAAAAElFTkSuQmCC"
)
# Minimal 1×1 JPEG — magic FF D8 FF; mutagen stores/returns these bytes as-is.
_JPEG_1X1 = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0a"
    "HBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA/8QAHwAAAQUBAQEB"
    "AQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1Fh"
    "ByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZ"
    "WmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXG"
    "x8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/9oACAEBAAA/APvV2yCo8UUA/9k="
)


def _mutagen_cover_bytes(path: Path) -> bytes | None:
    """Oracle: what mutagen reads back from the same file."""
    audio = mutagen.File(path)
    if audio is None:
        return None
    pictures = getattr(audio, "pictures", None)
    if pictures:
        data = getattr(pictures[0], "data", None)
        if data:
            return bytes(data)
    tags = getattr(audio, "tags", None)
    if tags is None:
        return None
    if hasattr(tags, "__contains__") and "covr" in tags:
        covers = tags["covr"]
        if covers:
            return bytes(covers[0])
    frames: list[object] = []
    try:
        keys = list(tags.keys())
    except (TypeError, AttributeError):
        return None
    for key in keys:
        sk = str(key)
        if sk.startswith(("APIC", "PIC")):
            frames.append(tags[key])
    if not frames:
        return None
    for frame in frames:
        ptype = getattr(frame, "type", None)
        data = getattr(frame, "data", None)
        if data and ptype is not None and int(ptype) == 3:
            return bytes(data)
    data = getattr(frames[0], "data", None)
    return bytes(data) if data else None


def _mp3_with_apic(
    path: Path,
    image: bytes,
    *,
    mime: str = "image/png",
    v2_version: int = 4,
    picture_type: int = 3,
    desc: str = "Cover",
) -> Path:
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
            type=picture_type,
            desc=desc,
            data=image,
        )
    )
    if v2_version == 3:
        audio.tags.update_to_v23()
    audio.save(v2_version=v2_version, v1=0)
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


def _assert_matches_mutagen(path: Path) -> None:
    ours = embedded_cover_bytes(path)
    oracle = _mutagen_cover_bytes(path)
    assert ours == oracle, (
        f"byte mismatch for {path.name}: "
        f"ours={None if ours is None else len(ours)}B "
        f"mutagen={None if oracle is None else len(oracle)}B"
    )


# ---------------------------------------------------------------------------
# Mutagen-oracle round-trips (formats we actually ship for)
# ---------------------------------------------------------------------------


def test_mp3_id3v24_apic_matches_mutagen(tmp_path: Path) -> None:
    """ID3v2.4 is mutagen's default; frame sizes are syncsafe.

    WHY: misreading v2.4 sizes as plain BE yields wrong offsets and either
    truncated garbage or None — both would corrupt AlbumArt BLOBs.
    """
    track = _mp3_with_apic(tmp_path / "v24.mp3", _PNG_1X1, v2_version=4)
    _assert_matches_mutagen(track)
    assert embedded_cover_bytes(track) == _PNG_1X1


def test_mp3_id3v23_apic_matches_mutagen(tmp_path: Path) -> None:
    """ID3v2.3 frame sizes are plain big-endian, not syncsafe.

    WHY: many DJ tools still write v2.3; the two size encodings diverge for
    any frame body ≥ 128 bytes, so the wrong decoder is not a theoretical bug.
    """
    track = _mp3_with_apic(tmp_path / "v23.mp3", _PNG_1X1, v2_version=3)
    _assert_matches_mutagen(track)
    assert embedded_cover_bytes(track) == _PNG_1X1


def test_m4a_covr_png_matches_mutagen(tmp_path: Path) -> None:
    """MP4 `covr` with PNG is common on the measured stick (m4a-heavy library)."""
    track = _m4a_with_covr(tmp_path / "cover.m4a", _PNG_1X1)
    _assert_matches_mutagen(track)
    assert embedded_cover_bytes(track) == _PNG_1X1


def test_m4a_covr_jpeg_matches_mutagen(tmp_path: Path) -> None:
    """JPEG covr is the other flags value (13); sniffing magic must still win."""
    track = _m4a_with_covr(tmp_path / "cover.jpg.m4a", _JPEG_1X1)
    _assert_matches_mutagen(track)
    assert embedded_cover_bytes(track) == _JPEG_1X1


def test_no_art_returns_none(tmp_path: Path) -> None:
    """Missing embedded art is the common case — None, never an error."""
    path = tmp_path / "plain.mp3"
    path.write_bytes(_BASE_MP3)
    assert embedded_cover_bytes(path) is None
    assert _mutagen_cover_bytes(path) is None


def test_does_not_modify_source_file(tmp_path: Path) -> None:
    """Read-only contract: never open audio for writing."""
    track = _m4a_with_covr(tmp_path / "ro.m4a", _PNG_1X1)
    before = track.read_bytes()
    embedded_cover_bytes(track)
    assert track.read_bytes() == before


# ---------------------------------------------------------------------------
# Direct structural cases (not mutagen-authored)
# ---------------------------------------------------------------------------


def test_truncated_file_returns_none_without_raising(tmp_path: Path) -> None:
    """Malformed/truncated audio must warn and return None, never raise.

    WHY: one bad file among thousands must not abort the conversion run.
    """
    bad = tmp_path / "trunc.mp3"
    bad.write_bytes(b"ID3\x04\x00\x00\x00\x00\x00\xff")  # claims huge size, EOF
    with warnings.catch_warnings(record=True):
        warnings.simplefilter("always")
        result = embedded_cover_bytes(bad)
    assert result is None


def test_corrupt_m4a_returns_none(tmp_path: Path) -> None:
    """Truncated ftyp/moov must not raise."""
    bad = tmp_path / "trunc.m4a"
    bad.write_bytes(b"\x00\x00\x00\x18ftypM4A \x00\x00\x00\x00")
    assert embedded_cover_bytes(bad) is None


def test_prefer_front_cover_picture_type(tmp_path: Path) -> None:
    """When several APIC frames exist, type 3 (front cover) wins.

    WHY: some rippers embed icon + front cover; wrong pick changes the hash.
    """
    path = tmp_path / "multi.mp3"
    path.write_bytes(_BASE_MP3)
    audio = MP3(path)
    if audio.tags is None:
        audio.add_tags()
    assert audio.tags is not None
    audio.tags.delall("APIC")
    # type 0 = other, type 3 = front cover
    other = b"\xff\xd8\xff\xe0" + b"OTHERJPEG" + b"\xff\xd9"
    front = _PNG_1X1
    audio.tags.add(
        APIC(encoding=3, mime="image/jpeg", type=0, desc="other", data=other)
    )
    audio.tags.add(
        APIC(encoding=3, mime="image/png", type=3, desc="front", data=front)
    )
    audio.save(v2_version=4, v1=0)

    assert embedded_cover_bytes(path) == front
    _assert_matches_mutagen(path)


def _syncsafe(n: int) -> bytes:
    return bytes(
        [
            (n >> 21) & 0x7F,
            (n >> 14) & 0x7F,
            (n >> 7) & 0x7F,
            n & 0x7F,
        ]
    )


def _be32(n: int) -> bytes:
    return struct.pack(">I", n)


def _build_id3v2(
    *,
    major: int,
    frames: list[bytes],
) -> bytes:
    """Build a minimal ID3v2 tag (no unsync) + a tiny MPEG frame stub."""
    body = b"".join(frames)
    header = b"ID3" + bytes([major, 0, 0]) + _syncsafe(len(body))
    # Minimal silence-ish audio so the file is not tag-only empty.
    return header + body + b"\xff\xfb\x90\x00" + b"\x00" * 32


def _apic_frame_v23(image: bytes, *, mime: bytes = b"image/png") -> bytes:
    """ID3v2.3 APIC: frame size is plain big-endian."""
    # encoding(1) + mime\0 + pic_type(1) + desc\0 + image
    body = b"\x03" + mime + b"\x00" + b"\x03" + b"\x00" + image
    return b"APIC" + _be32(len(body)) + b"\x00\x00" + body


def _apic_frame_v24(image: bytes, *, mime: bytes = b"image/png") -> bytes:
    """ID3v2.4 APIC: frame size is syncsafe."""
    body = b"\x03" + mime + b"\x00" + b"\x03" + b"\x00" + image
    return b"APIC" + _syncsafe(len(body)) + b"\x00\x00" + body


def test_id3v23_vs_v24_frame_size_encoding(tmp_path: Path) -> None:
    """A payload that makes plain-BE and syncsafe size bytes diverge.

    Frame body length 128 → v2.3 stores ``00 00 00 80``; v2.4 stores
    ``00 00 01 00``. Syncsafe-decoding the v2.3 size yields 0; plain-BE
    decoding the v2.4 size yields 256. Either mistake fails this test.

    WHY: this is the classic silent misread that would ship wrong art or None.
    """
    # APIC body layout: enc(1)+mime(10)+"\0"+type(1)+desc(0)+"\0"+image
    # mime "image/png" = 9 chars + NUL = 10; total overhead = 1+10+1+1 = 13
    # Want total body == 128 so size encoding differs: image = 128 - 13 = 115
    overhead = 1 + len(b"image/png") + 1 + 1 + 1  # enc, mime, nul, type, desc nul
    assert overhead == 13
    image = b"\x89PNG\r\n\x1a\n" + bytes(range(256))[: 128 - overhead - 8]
    image = image + b"\x00" * (128 - overhead - len(image))
    assert len(image) + overhead == 128

    v23 = tmp_path / "size_v23.mp3"
    v23.write_bytes(_build_id3v2(major=3, frames=[_apic_frame_v23(image)]))
    v24 = tmp_path / "size_v24.mp3"
    v24.write_bytes(_build_id3v2(major=4, frames=[_apic_frame_v24(image)]))

    assert embedded_cover_bytes(v23) == image
    assert embedded_cover_bytes(v24) == image

    # Prove the encodings actually differ in the file bytes.
    assert b"\x00\x00\x00\x80" in v23.read_bytes()  # plain BE 128
    assert b"\x00\x00\x01\x00" in v24.read_bytes()  # syncsafe 128


def test_mp4_meta_fullbox_version_flags_skipped(tmp_path: Path) -> None:
    """``meta`` is a FullBox: 4 version/flags bytes before children.

    Hand-build moov/udta/meta/ilst/covr/data. If the parser treats meta as a
    plain box, the first child type becomes the version/flags word and covr
    is never found.

    WHY: this is a classic MP4 metadata bug; mutagen-written files always
    include those 4 bytes, so a naive walk fails on every real m4a.
    """
    # data box: size + 'data' + version/flags(4) + reserved(4) + payload
    payload = _PNG_1X1
    data_body = b"\x00\x00\x00\x00" + b"\x00\x00\x00\x00" + payload  # flags=0
    data_box = _be32(8 + len(data_body)) + b"data" + data_body

    covr_body = data_box
    covr_box = _be32(8 + len(covr_body)) + b"covr" + covr_body

    ilst_body = covr_box
    ilst_box = _be32(8 + len(ilst_body)) + b"ilst" + ilst_body

    # meta FullBox: version/flags then hdlr (optional) + ilst
    meta_children = b"\x00\x00\x00\x00" + ilst_box  # version=0, flags=0
    meta_box = _be32(8 + len(meta_children)) + b"meta" + meta_children

    udta_body = meta_box
    udta_box = _be32(8 + len(udta_body)) + b"udta" + udta_body

    moov_body = udta_box
    moov_box = _be32(8 + len(moov_body)) + b"moov" + moov_body

    ftyp = _be32(20) + b"ftyp" + b"M4A " + _be32(0) + b"M4A "
    # free atom padding so structure is plausible
    free = _be32(16) + b"free" + b"\x00" * 8

    path = tmp_path / "hand_meta.m4a"
    path.write_bytes(ftyp + free + moov_box)

    assert embedded_cover_bytes(path) == payload


def test_unknown_extension_with_id3_magic(tmp_path: Path) -> None:
    """Dispatch by content magic, not only by file extension."""
    track = _mp3_with_apic(tmp_path / "song.bin", _PNG_1X1, v2_version=4)
    assert embedded_cover_bytes(track) == _PNG_1X1


def test_empty_file_returns_none(tmp_path: Path) -> None:
    path = tmp_path / "empty.mp3"
    path.write_bytes(b"")
    assert embedded_cover_bytes(path) is None

