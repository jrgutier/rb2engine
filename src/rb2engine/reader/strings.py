"""device_sql_string codecs: short-ascii / 0x40 long-ascii / 0x90 utf16le.

Layout follows Deep Symmetry crate-digger's `rekordbox_pdb.ksy` (the format
this project will vendor). Length fields count the **entire** string field
including header bytes, not the payload alone.

API
---
`decode_device_sql_string(data, offset) -> tuple[str, int]`

Returns `(text, bytes_consumed)` where `bytes_consumed` is the number of
bytes of `data` starting at `offset` that form the complete string field
(discriminator through last payload byte). Callers advance by that amount
(or use it when validating a fixed offset table).

Raises `UnsupportedFormatError` on truncation, unknown discriminators,
corrupt length headers, or invalid UTF-16 sequences. Never returns
replacement characters for undecodable input.
"""

from __future__ import annotations

import struct

from rb2engine.errors import UnsupportedFormatError

# Long-form discriminators observed in rekordbox exports (ksy switch cases).
_LONG_ASCII = 0x40
_LONG_UTF16LE = 0x90
# Long forms: disc (1) + u2 length (2) + pad (1) = 4 header bytes.
_LONG_HEADER = 4


def decode_device_sql_string(data: bytes, offset: int = 0) -> tuple[str, int]:
    """Decode one DeviceSQL string at `offset` inside `data`.

    Parameters
    ----------
    data:
        Buffer containing at least one device_sql_string (often a page heap).
    offset:
        Index of the discriminator byte within `data`.

    Returns
    -------
    text:
        Decoded Unicode string.
    consumed:
        Total bytes read from `offset` (header + payload).

    Raises
    ------
    UnsupportedFormatError
        Buffer too short for the declared length, unknown encoding kind,
        long-form length smaller than the 4-byte header, or invalid UTF-16.
    """
    if offset < 0 or offset >= len(data):
        raise UnsupportedFormatError(
            f"device_sql_string offset {offset} out of range for buffer of "
            f"{len(data)} bytes"
        )

    disc = data[offset]

    if disc == _LONG_ASCII:
        return _decode_long(data, offset, encoding="ascii")
    if disc == _LONG_UTF16LE:
        return _decode_long(data, offset, encoding="utf-16-le")
    if disc & 1:
        # Short ASCII: low bit is the kind flag; remaining bits hold field length.
        return _decode_short_ascii(data, offset, disc)

    raise UnsupportedFormatError(
        f"unknown device_sql_string discriminator 0x{disc:02x} at offset {offset}"
    )


def _decode_short_ascii(data: bytes, offset: int, disc: int) -> tuple[str, int]:
    """Short ASCII: field_length = disc >> 1; payload = field_length - 1.

    The packed length includes the discriminator itself, so empty string is
    disc 0x03 (field length 1). Maximum payload is 126 bytes (disc 0xFF).
    """
    field_length = disc >> 1
    if field_length < 1:
        # disc odd and >>1 == 0 would require disc == 0 or 1; 0 is even (handled
        # above). disc == 1 → field_length 0 is corrupt.
        raise UnsupportedFormatError(
            f"device_sql_short_ascii field length {field_length} at offset {offset}"
        )

    end = offset + field_length
    if end > len(data):
        raise UnsupportedFormatError(
            f"device_sql_short_ascii truncated: need {field_length} bytes at "
            f"offset {offset}, buffer ends at {len(data)}"
        )

    payload = data[offset + 1 : end]
    try:
        text = payload.decode("ascii")
    except UnicodeDecodeError as exc:
        raise UnsupportedFormatError(
            f"device_sql_short_ascii is not valid ASCII at offset {offset}: {exc}"
        ) from exc

    return text, field_length


def _decode_long(data: bytes, offset: int, *, encoding: str) -> tuple[str, int]:
    """Long ASCII (0x40) or UTF-16LE (0x90): u2 total length + pad + payload.

    `length` is the full field size including the 4-byte header (discriminator,
    length u2, pad u1). Payload size is therefore `length - 4`.
    """
    if offset + _LONG_HEADER > len(data):
        raise UnsupportedFormatError(
            f"device_sql_long header truncated at offset {offset}: need "
            f"{_LONG_HEADER} bytes, buffer ends at {len(data)}"
        )

    total_length = struct.unpack_from("<H", data, offset + 1)[0]
    # pad at offset+3 is ignored (always 0 in observed exports)

    if total_length < _LONG_HEADER:
        raise UnsupportedFormatError(
            f"device_sql_long length {total_length} smaller than header "
            f"({_LONG_HEADER}) at offset {offset}"
        )

    end = offset + total_length
    if end > len(data):
        raise UnsupportedFormatError(
            f"device_sql_long truncated: need {total_length} bytes at offset "
            f"{offset}, buffer ends at {len(data)}"
        )

    payload = data[offset + _LONG_HEADER : end]
    try:
        # strict: refuse lone surrogates / odd-length UTF-16 (surrogatepass would hide them)
        text = payload.decode(encoding)
    except UnicodeDecodeError as exc:
        raise UnsupportedFormatError(
            f"device_sql_long {encoding} decode failed at offset {offset}: {exc}"
        ) from exc

    return text, total_length
