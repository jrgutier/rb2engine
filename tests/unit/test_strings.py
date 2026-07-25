"""device_sql_string decoding — hand-authored from Deep Symmetry / crate-digger Kaitai.

Expectations are NOT derived from our implementation. They follow the
`device_sql_string` / `device_sql_short_ascii` / `device_sql_long_ascii` /
`device_sql_long_utf16le` layouts in rekordbox_pdb.ksy:

- short ASCII (discriminator odd / low bit set): field length = disc >> 1;
  payload length = (disc >> 1) - 1; then that many ASCII bytes.
- long ASCII (0x40): u2 LE total-field-length, 1 pad byte, then
  (length - 4) ASCII bytes.
- long UTF-16LE (0x90): same header shape; payload is UTF-16LE.

Why these tests matter: every track title/path/comment in export.pdb is a
device_sql_string. Wrong length math silently shifts the heap and corrupts
every subsequent field on the page. Truncation must fail loud, not return
garbage the mapper would treat as a real title.
"""

from __future__ import annotations

import struct

import pytest

from rb2engine.errors import UnsupportedFormatError
from rb2engine.reader.strings import decode_device_sql_string

# ---------------------------------------------------------------------------
# Short ASCII (discriminator low bit set)
# ---------------------------------------------------------------------------


def test_short_ascii_empty() -> None:
    """Empty string: field length 1 → discriminator (1 << 1) | 1 = 0x03.

    Empty names and blank path segments are common in export.pdb; decoding
    them as garbage would invent fake metadata.
    """
    data = bytes([0x03])
    text, consumed = decode_device_sql_string(data, 0)
    assert text == ""
    assert consumed == 1


def test_short_ascii_hello() -> None:
    """'Hello' is 5 bytes → field length 6 → disc = (6 << 1) | 1 = 0x0D."""
    data = bytes([0x0D]) + b"Hello"
    text, consumed = decode_device_sql_string(data, 0)
    assert text == "Hello"
    assert consumed == 6


def test_short_ascii_with_offset() -> None:
    """Decoder must respect `offset` so pdb row string tables can slice a heap."""
    prefix = b"\x00\x00\xff"
    body = bytes([0x07]) + b"Hi"  # field len 3 → disc 0x07
    data = prefix + body + b"TRAIL"
    text, consumed = decode_device_sql_string(data, len(prefix))
    assert text == "Hi"
    assert consumed == 3


def test_short_ascii_max_payload_126() -> None:
    """Max short form: disc 0xFF → field length 127 → 126 data bytes.

    Strings of length 127+ must use the long form; 126 is the last short
    encoding that fits in one length-and-kind byte. Getting the off-by-one
    wrong here either truncates titles or overruns into the next heap object.
    """
    payload = b"A" * 126
    disc = 0xFF  # (127 << 1) | 1, but 127 << 1 is 254; 254 | 1 = 255
    assert (disc >> 1) - 1 == 126
    data = bytes([disc]) + payload
    text, consumed = decode_device_sql_string(data, 0)
    assert text == "A" * 126
    assert consumed == 127


def test_short_ascii_boundary_just_below_max() -> None:
    """125-byte payload (field length 126) still short-form."""
    payload = b"B" * 125
    field_len = 126
    disc = (field_len << 1) | 1  # 0xFD
    data = bytes([disc]) + payload
    text, consumed = decode_device_sql_string(data, 0)
    assert text == "B" * 125
    assert consumed == 126


# ---------------------------------------------------------------------------
# Long ASCII (0x40)
# ---------------------------------------------------------------------------


def test_long_ascii_empty() -> None:
    """Empty long ASCII: total length 4 (header only), pad 0, no payload."""
    # layout: 0x40 | u2le length=4 | pad 0x00
    data = bytes([0x40, 0x04, 0x00, 0x00])
    text, consumed = decode_device_sql_string(data, 0)
    assert text == ""
    assert consumed == 4


def test_long_ascii_hello() -> None:
    """'Hello' (5) → total length 4 + 5 = 9."""
    payload = b"Hello"
    total = 4 + len(payload)
    data = bytes([0x40]) + struct.pack("<H", total) + bytes([0x00]) + payload
    text, consumed = decode_device_sql_string(data, 0)
    assert text == "Hello"
    assert consumed == total


def test_long_ascii_128_bytes_past_short_boundary() -> None:
    """128-byte ASCII cannot be short (max short data 126); must use 0x40.

    This is the path long pathnames / comments take when they exceed the
    packed-length form. If the short/long switch is wrong, paths resolve
    incorrectly and tracks are skipped.
    """
    payload = b"P" * 128
    total = 4 + len(payload)
    data = bytes([0x40]) + struct.pack("<H", total) + bytes([0x00]) + payload
    text, consumed = decode_device_sql_string(data, 0)
    assert text == "P" * 128
    assert consumed == total


# ---------------------------------------------------------------------------
# Long UTF-16LE (0x90)
# ---------------------------------------------------------------------------


def test_long_utf16le_empty() -> None:
    data = bytes([0x90, 0x04, 0x00, 0x00])
    text, consumed = decode_device_sql_string(data, 0)
    assert text == ""
    assert consumed == 4


def test_long_utf16le_ascii_compatible() -> None:
    """'Hi' as UTF-16LE: 48 00 69 00 → total length 8."""
    payload = "Hi".encode("utf-16-le")
    assert payload == b"\x48\x00\x69\x00"
    total = 4 + len(payload)
    data = bytes([0x90]) + struct.pack("<H", total) + bytes([0x00]) + payload
    text, consumed = decode_device_sql_string(data, 0)
    assert text == "Hi"
    assert consumed == total


def test_long_utf16le_non_ascii() -> None:
    """Non-ASCII titles (e.g. Japanese) force the 0x90 path in real exports.

    ASCII short/long paths would mojibake these; the acceptance fixture
    deliberately includes non-ASCII titles to exercise this branch.
    """
    # "日本語" — three BMP codepoints → 6 UTF-16LE bytes
    s = "日本語"
    payload = s.encode("utf-16-le")
    assert len(payload) == 6
    total = 4 + len(payload)
    data = bytes([0x90]) + struct.pack("<H", total) + bytes([0x00]) + payload
    text, consumed = decode_device_sql_string(data, 0)
    assert text == s
    assert consumed == total


def test_long_utf16le_emoji_surrogate_pair() -> None:
    """Supplementary-plane characters use surrogate pairs in UTF-16LE.

    A decoder that reads fixed 2-byte units without surrogate handling
    would either raise or produce wrong length; we require a correct
    Unicode string.
    """
    s = "🎵"  # U+1F3B5, one surrogate pair → 4 bytes
    payload = s.encode("utf-16-le")
    assert len(payload) == 4
    total = 4 + len(payload)
    data = bytes([0x90]) + struct.pack("<H", total) + bytes([0x00]) + payload
    text, consumed = decode_device_sql_string(data, 0)
    assert text == s
    assert consumed == total


# ---------------------------------------------------------------------------
# Error paths — must not silently return garbage
# ---------------------------------------------------------------------------


def test_truncated_short_ascii_raises() -> None:
    """Declared payload past end of buffer → typed error, not partial text."""
    # Claims field length 6 ("Hello") but only two data bytes present
    data = bytes([0x0D]) + b"He"
    with pytest.raises(UnsupportedFormatError):
        decode_device_sql_string(data, 0)


def test_truncated_long_ascii_header_raises() -> None:
    """Long form needs 4 header bytes; fewer must raise."""
    data = bytes([0x40, 0x09])  # incomplete length / no pad
    with pytest.raises(UnsupportedFormatError):
        decode_device_sql_string(data, 0)


def test_truncated_long_ascii_payload_raises() -> None:
    total = 4 + 10
    data = bytes([0x40]) + struct.pack("<H", total) + bytes([0x00]) + b"short"
    assert len(data) < total
    with pytest.raises(UnsupportedFormatError):
        decode_device_sql_string(data, 0)


def test_truncated_long_utf16le_payload_raises() -> None:
    total = 4 + 8
    data = bytes([0x90]) + struct.pack("<H", total) + bytes([0x00]) + b"\x48\x00"
    with pytest.raises(UnsupportedFormatError):
        decode_device_sql_string(data, 0)


def test_empty_buffer_raises() -> None:
    with pytest.raises(UnsupportedFormatError):
        decode_device_sql_string(b"", 0)


def test_offset_past_end_raises() -> None:
    with pytest.raises(UnsupportedFormatError):
        decode_device_sql_string(b"\x03", 5)


def test_invalid_utf16_lone_surrogate_raises() -> None:
    """Odd-length or lone-surrogate UTF-16 must not become replacement garbage.

    Silent U+FFFD would look like a real title and pass later stages.
    """
    # Lone high surrogate U+D800 as the only code unit (2 bytes payload)
    payload = b"\x00\xd8"
    total = 4 + len(payload)
    data = bytes([0x90]) + struct.pack("<H", total) + bytes([0x00]) + payload
    with pytest.raises(UnsupportedFormatError):
        decode_device_sql_string(data, 0)


def test_invalid_utf16_odd_byte_length_raises() -> None:
    """UTF-16LE payload length must be even; odd length is truncated encoding."""
    payload = b"\x48\x00\x69"  # 3 bytes — not a valid UTF-16LE sequence length
    total = 4 + len(payload)
    data = bytes([0x90]) + struct.pack("<H", total) + bytes([0x00]) + payload
    with pytest.raises(UnsupportedFormatError):
        decode_device_sql_string(data, 0)


def test_unknown_discriminator_raises() -> None:
    """Even discriminators other than 0x40/0x90 are not used by rekordbox exports.

    Failing loud beats treating an unknown long form as short ASCII.
    Note: short form uses odd discriminators only (low bit = kind flag).
    """
    # 0x00 is even and not a known long marker
    data = bytes([0x00, 0x04, 0x00, 0x00])
    with pytest.raises(UnsupportedFormatError):
        decode_device_sql_string(data, 0)


def test_long_ascii_length_too_small_for_header_raises() -> None:
    """Total length must be >= 4 (header size). length=3 is corrupt."""
    data = bytes([0x40, 0x03, 0x00, 0x00])
    with pytest.raises(UnsupportedFormatError):
        decode_device_sql_string(data, 0)
