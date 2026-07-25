"""export.pdb DeviceSQL walker → SourceLibrary (construct-based, Option D).

Layout follows Deep Symmetry crate-digger's `rekordbox_pdb.ksy`, reimplemented
with the MIT `construct` library (no Kaitai / no JVM). DeviceSQL strings are
delegated to `reader.strings.decode_device_sql_string` (already verified).

Gates
-----
G1a  header insanity → UnsupportedFormatError
G1b  page_type outside the known enum on an unconsumed table → warn + continue
G1c  consumed table missing-when-required or present-but-unparseable → UnsupportedFormatError
"""

from __future__ import annotations

import struct
from enum import IntEnum
from pathlib import Path, PurePosixPath
from typing import Any

from construct import (  # type: ignore[import-untyped]
    Array,
    Bytes,
    Const,
    Int16ul,
    Int32ul,
    Struct,
    this,
)
from construct.core import ConstructError

from rb2engine.errors import UnsupportedFormatError
from rb2engine.ir import SourceLibrary, SourcePlaylist, SourceTrack
from rb2engine.reader.paths import resolve_track_path
from rb2engine.reader.strings import decode_device_sql_string

# ---------------------------------------------------------------------------
# Page-type taxonomy (crate-digger enums.page_type)
# ---------------------------------------------------------------------------


class PageType(IntEnum):
    TRACKS = 0
    GENRES = 1
    ARTISTS = 2
    ALBUMS = 3
    LABELS = 4
    KEYS = 5
    COLORS = 6
    PLAYLIST_TREE = 7
    PLAYLIST_ENTRIES = 8
    UNKNOWN_9 = 9
    UNKNOWN_10 = 10
    HISTORY_PLAYLISTS = 11
    HISTORY_ENTRIES = 12
    ARTWORK = 13
    UNKNOWN_14 = 14
    UNKNOWN_15 = 15
    COLUMNS = 16
    UNKNOWN_17 = 17
    UNKNOWN_18 = 18
    HISTORY = 19


KNOWN_PAGE_TYPES: frozenset[int] = frozenset(int(v) for v in PageType)

# Required: absence is fatal. Optional-consumed: absence OK, malformation fatal.
REQUIRED_TABLES: frozenset[PageType] = frozenset(
    {
        PageType.TRACKS,
        PageType.PLAYLIST_TREE,
        PageType.PLAYLIST_ENTRIES,
    }
)
OPTIONAL_CONSUMED_TABLES: frozenset[PageType] = frozenset(
    {
        PageType.GENRES,
        PageType.ARTISTS,
        PageType.ALBUMS,
        PageType.LABELS,
        PageType.KEYS,
        PageType.COLORS,
    }
)
CONSUMED_TABLES: frozenset[PageType] = REQUIRED_TABLES | OPTIONAL_CONSUMED_TABLES

PAGE_HEADER_SIZE = 40
ROW_GROUP_SIZE = 0x24  # 16 × u2 offsets + present u2 + transaction u2

# Track string-offset indices (ksy track_row.ofs_strings)
_STR_COMMENT = 16
_STR_TITLE = 17
_STR_FILENAME = 19
_STR_FILE_PATH = 20
_TRACK_NUM_STRINGS = 21
_TRACK_FIXED_SIZE = 94  # bytes before ofs_strings[]

# Plausible header bounds (real exports use len_page=4096, num_tables≈20).
_MIN_PAGE = 512
_MAX_PAGE = 65536
_MAX_TABLES = 256

# ---------------------------------------------------------------------------
# construct structs — file header + page header (row heap is hand-walked)
# ---------------------------------------------------------------------------

TablePointer = Struct(
    "page_type" / Int32ul,
    "empty_candidate" / Int32ul,
    "first_page" / Int32ul,
    "last_page" / Int32ul,
)

FileHeader = Struct(
    "unknown0" / Int32ul,
    "len_page" / Int32ul,
    "num_tables" / Int32ul,
    "next_unused_page" / Int32ul,
    "unknown1" / Int32ul,
    "sequence" / Int32ul,
    "gap" / Const(b"\x00\x00\x00\x00"),
    "tables" / Array(this.num_tables, TablePointer),
)

# Bytes 0–23 of a page; the next u32 packs num_row_offsets/num_rows/flags.
PageHeaderLead = Struct(
    "gap" / Bytes(4),
    "page_index" / Int32ul,
    "page_type" / Int32ul,
    "next_page" / Int32ul,
    "sequence" / Int32ul,
    "unknown" / Int32ul,
)

PageHeaderTail = Struct(
    "free_size" / Int16ul,
    "used_size" / Int16ul,
    "transaction_row_count" / Int16ul,
    "transaction_row_index" / Int16ul,
    "unknown_a" / Int16ul,
    "unknown_b" / Int16ul,
)


# ---------------------------------------------------------------------------
# Header / gate helpers
# ---------------------------------------------------------------------------


def _is_power_of_two(n: int) -> bool:
    return n > 0 and (n & (n - 1)) == 0


def assert_pdb_supported(len_page: int, num_tables: int) -> None:
    """G1a — header sanity. Raises UnsupportedFormatError on insanity."""
    if not _is_power_of_two(len_page) or not (_MIN_PAGE <= len_page <= _MAX_PAGE):
        raise UnsupportedFormatError(
            f"export.pdb len_page={len_page} is not a plausible power of two "
            f"in [{_MIN_PAGE}, {_MAX_PAGE}]; refuse to parse. "
            f"Run `rb2engine inspect --raw` and report the header."
        )
    if num_tables < 1 or num_tables > _MAX_TABLES:
        raise UnsupportedFormatError(
            f"export.pdb num_tables={num_tables} is implausible "
            f"(expected 1..{_MAX_TABLES}); refuse to parse."
        )


def _parse_file_header(data: bytes) -> Any:
    # Returns a construct Container. `construct` ships no type stubs, so a
    # precise return type is not expressible here; `Any` states that honestly
    # rather than annotating `object` and then reaching through it.
    if len(data) < 28:
        raise UnsupportedFormatError(
            f"export.pdb truncated: {len(data)} bytes, need at least 28 for header"
        )
    # Peek counts before construct so G1a fires with a clear message.
    len_page = struct.unpack_from("<I", data, 4)[0]
    num_tables = struct.unpack_from("<I", data, 8)[0]
    assert_pdb_supported(len_page, num_tables)

    header_size = 28 + 16 * num_tables
    if len(data) < header_size:
        raise UnsupportedFormatError(
            f"export.pdb truncated: need {header_size} bytes for {num_tables} "
            f"table pointers, file is {len(data)} bytes"
        )
    try:
        return FileHeader.parse(data[:header_size])
    except Exception as exc:  # construct raises various subclasses
        raise UnsupportedFormatError(
            f"export.pdb header unreadable: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Page / row-index walking
# ---------------------------------------------------------------------------


def _decode_page_counts(page: bytes) -> tuple[int, int, int]:
    """Return (num_row_offsets, num_rows, page_flags) from the packed u32 at +24."""
    word = struct.unpack_from("<I", page, 24)[0]
    num_row_offsets = word & 0x1FFF
    num_rows = (word >> 13) & 0x7FF
    page_flags = (word >> 24) & 0xFF
    return num_row_offsets, num_rows, page_flags


def _is_data_page(page_flags: int) -> bool:
    # ksy: is_data_page = (page_flags & 0x40) == 0
    return (page_flags & 0x40) == 0


def _iter_present_row_bases(
    page: bytes, len_page: int, num_row_offsets: int
) -> list[int]:
    """Absolute offsets within `page` of each *present* row body."""
    if num_row_offsets <= 0:
        return []
    num_groups = (num_row_offsets - 1) // 16 + 1
    bases: list[int] = []
    for g in range(num_groups):
        group_base = len_page - (g * ROW_GROUP_SIZE)
        if group_base - 4 < PAGE_HEADER_SIZE:
            break
        present_flags = struct.unpack_from("<H", page, group_base - 4)[0]
        for r in range(16):
            abs_i = g * 16 + r
            if abs_i >= num_row_offsets:
                break
            if (present_flags >> r) & 1 == 0:
                continue  # deleted / absent — honour the bitmask
            ofs_pos = group_base - (6 + 2 * r)
            if ofs_pos < 0:
                continue
            ofs_row = struct.unpack_from("<H", page, ofs_pos)[0]
            row_base = ofs_row + PAGE_HEADER_SIZE
            if 0 <= row_base < len_page:
                bases.append(row_base)
    return bases


def _walk_table_pages(
    data: bytes,
    len_page: int,
    first_page: int,
    last_page: int,
    expected_type: int,
) -> list[tuple[bytes, list[int]]]:
    """Follow next_page from first→last; yield (page_bytes, present_row_bases)."""
    out: list[tuple[bytes, list[int]]] = []
    idx = first_page
    seen: set[int] = set()
    file_pages = len(data) // len_page

    while idx not in seen:
        seen.add(idx)
        if idx < 0 or idx >= file_pages:
            break
        start = idx * len_page
        page = data[start : start + len_page]
        if len(page) < PAGE_HEADER_SIZE:
            break

        try:
            lead = PageHeaderLead.parse(page[:24])
        except ConstructError:
            # Malformed page header: stop walking this chain. Narrow on purpose —
            # a bare `except Exception` would also swallow bugs in our own code
            # and silently truncate the table, which inverts the fail-loud rule.
            break

        next_page = int(lead.next_page)
        page_type = int(lead.page_type)
        num_row_offsets, _num_rows, page_flags = _decode_page_counts(page)

        # Stop if we landed on a different table type (safety).
        if page_type != expected_type and idx != first_page:
            break

        if _is_data_page(page_flags) and page_type == expected_type:
            bases = _iter_present_row_bases(page, len_page, num_row_offsets)
            out.append((page, bases))

        if idx == last_page:
            break
        if next_page == idx or next_page >= file_pages:
            break
        idx = next_page

    return out


# ---------------------------------------------------------------------------
# Row parsers (offsets from crate-digger ksy; verified on real export.pdb)
# ---------------------------------------------------------------------------


def _need(page: bytes, row_base: int, size: int, what: str) -> None:
    if row_base < 0 or row_base + size > len(page):
        raise UnsupportedFormatError(
            f"{what} row truncated at offset {row_base} (need {size} bytes, "
            f"page is {len(page)})"
        )


def _string_at(page: bytes, row_base: int, ofs: int) -> str:
    abs_off = row_base + ofs
    if abs_off < 0 or abs_off >= len(page):
        raise UnsupportedFormatError(
            f"string offset {ofs} out of page (row_base={row_base}, page={len(page)})"
        )
    text, _ = decode_device_sql_string(page, abs_off)
    return text


def _parse_genre_row(page: bytes, row_base: int) -> tuple[int, str]:
    _need(page, row_base, 5, "genre")
    gid = struct.unpack_from("<I", page, row_base)[0]
    name = _string_at(page, row_base, 4)
    return gid, name


def _parse_label_row(page: bytes, row_base: int) -> tuple[int, str]:
    _need(page, row_base, 5, "label")
    lid = struct.unpack_from("<I", page, row_base)[0]
    name = _string_at(page, row_base, 4)
    return lid, name


def _parse_key_row(page: bytes, row_base: int) -> tuple[int, str]:
    _need(page, row_base, 9, "key")
    kid = struct.unpack_from("<I", page, row_base)[0]
    name = _string_at(page, row_base, 8)
    return kid, name


def _parse_color_row(page: bytes, row_base: int) -> tuple[int, str]:
    _need(page, row_base, 9, "color")
    cid = struct.unpack_from("<H", page, row_base + 5)[0]
    name = _string_at(page, row_base, 8)
    return cid, name


def _parse_artist_row(page: bytes, row_base: int) -> tuple[int, str]:
    _need(page, row_base, 10, "artist")
    subtype = struct.unpack_from("<H", page, row_base)[0]
    aid = struct.unpack_from("<I", page, row_base + 4)[0]
    if subtype & 0x04:
        _need(page, row_base, 12, "artist")
        ofs = struct.unpack_from("<H", page, row_base + 0x0A)[0]
    else:
        ofs = page[row_base + 9]
    name = _string_at(page, row_base, ofs)
    return aid, name


def _parse_album_row(page: bytes, row_base: int) -> tuple[int, str]:
    _need(page, row_base, 0x16, "album")
    subtype = struct.unpack_from("<H", page, row_base)[0]
    album_id = struct.unpack_from("<I", page, row_base + 12)[0]
    if subtype & 0x04:
        _need(page, row_base, 0x18, "album")
        ofs = struct.unpack_from("<H", page, row_base + 0x16)[0]
    else:
        ofs = page[row_base + 0x15]
    name = _string_at(page, row_base, ofs)
    return album_id, name


def _parse_playlist_tree_row(
    page: bytes, row_base: int
) -> tuple[int, int, int, bool, str]:
    _need(page, row_base, 21, "playlist_tree")
    parent_id = struct.unpack_from("<I", page, row_base)[0]
    sort_order = struct.unpack_from("<I", page, row_base + 8)[0]
    pl_id = struct.unpack_from("<I", page, row_base + 12)[0]
    raw_folder = struct.unpack_from("<I", page, row_base + 16)[0]
    name = _string_at(page, row_base, 20)
    return parent_id, sort_order, pl_id, raw_folder != 0, name


def _parse_playlist_entry_row(page: bytes, row_base: int) -> tuple[int, int, int]:
    _need(page, row_base, 12, "playlist_entry")
    entry_index, track_id, playlist_id = struct.unpack_from("<III", page, row_base)
    return entry_index, track_id, playlist_id


def _parse_track_row(page: bytes, row_base: int) -> dict:
    _need(page, row_base, _TRACK_FIXED_SIZE + 2 * _TRACK_NUM_STRINGS, "track")
    sample_rate = struct.unpack_from("<I", page, row_base + 8)[0]
    composer_id = struct.unpack_from("<I", page, row_base + 12)[0]
    file_size = struct.unpack_from("<I", page, row_base + 16)[0]
    key_id = struct.unpack_from("<I", page, row_base + 32)[0]
    label_id = struct.unpack_from("<I", page, row_base + 40)[0]
    remixer_id = struct.unpack_from("<I", page, row_base + 44)[0]
    bitrate = struct.unpack_from("<I", page, row_base + 48)[0]
    track_number = struct.unpack_from("<I", page, row_base + 52)[0]
    tempo = struct.unpack_from("<I", page, row_base + 56)[0]
    genre_id = struct.unpack_from("<I", page, row_base + 60)[0]
    album_id = struct.unpack_from("<I", page, row_base + 64)[0]
    artist_id = struct.unpack_from("<I", page, row_base + 68)[0]
    tid = struct.unpack_from("<I", page, row_base + 72)[0]
    disc_number = struct.unpack_from("<H", page, row_base + 76)[0]
    play_count = struct.unpack_from("<H", page, row_base + 78)[0]
    year = struct.unpack_from("<H", page, row_base + 80)[0]
    duration = struct.unpack_from("<H", page, row_base + 84)[0]
    rating = page[row_base + 89]

    ofs_strings = struct.unpack_from(
        f"<{_TRACK_NUM_STRINGS}H", page, row_base + _TRACK_FIXED_SIZE
    )

    def s(i: int) -> str:
        return _string_at(page, row_base, ofs_strings[i])

    return {
        "id": tid,
        "sample_rate": sample_rate,
        "composer_id": composer_id,
        "file_size": file_size,
        "key_id": key_id,
        "label_id": label_id,
        "remixer_id": remixer_id,
        "bitrate": bitrate,
        "track_number": track_number,
        "tempo": tempo,
        "genre_id": genre_id,
        "album_id": album_id,
        "artist_id": artist_id,
        "disc_number": disc_number,
        "play_count": play_count,
        "year": year,
        "duration": duration,
        "rating": rating,
        "comment": s(_STR_COMMENT),
        "title": s(_STR_TITLE),
        "filename": s(_STR_FILENAME),
        "file_path": s(_STR_FILE_PATH),
    }


# ---------------------------------------------------------------------------
# Table loaders
# ---------------------------------------------------------------------------


def _load_id_name_table(
    data: bytes,
    len_page: int,
    first: int,
    last: int,
    page_type: PageType,
    row_parser,
    table_label: str,
) -> dict[int, str]:
    result: dict[int, str] = {}
    try:
        for page, bases in _walk_table_pages(
            data, len_page, first, last, int(page_type)
        ):
            for rb in bases:
                rid, name = row_parser(page, rb)
                result[rid] = name
    except UnsupportedFormatError:
        raise
    except Exception as exc:
        raise UnsupportedFormatError(
            f"export.pdb {table_label} table unparseable: {exc}"
        ) from exc
    return result


def _table_map(
    header_tables: list,
) -> dict[int, tuple[int, int]]:
    """page_type → (first_page, last_page). First-wins on duplicates."""
    out: dict[int, tuple[int, int]] = {}
    for t in header_tables:
        ptype = int(t.page_type)
        if ptype not in out:
            out[ptype] = (int(t.first_page), int(t.last_page))
    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_export_pdb(path: Path, drive_root: Path) -> SourceLibrary:
    """Parse ``export.pdb`` into a SourceLibrary (tracks + playlists, no ANLZ).

    Parameters
    ----------
    path:
        Path to an export.pdb file (use a *copy* of stick data; never open the
        live stick read-write).
    drive_root:
        Mount root used to resolve ``Contents/`` paths to real files.
    """
    path = Path(path)
    drive_root = Path(drive_root)
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise UnsupportedFormatError(f"cannot read export.pdb at {path}: {exc}") from exc

    header = _parse_file_header(data)
    len_page = int(header.len_page)
    tables = _table_map(list(header.tables))
    warnings: list[str] = []

    # G1b: unknown page_type values on tables we do not consume.
    for t in header.tables:
        ptype = int(t.page_type)
        if ptype not in KNOWN_PAGE_TYPES:
            warnings.append(
                f"unknown page_type={ptype} on unconsumed table "
                f"(first_page={int(t.first_page)}); skipped"
            )
            continue
        # Known but not in CONSUMED_TABLES: skip quietly (history, artwork, …).

    # G1c: required tables must be listed.
    for req in REQUIRED_TABLES:
        if int(req) not in tables:
            raise UnsupportedFormatError(
                f"export.pdb missing required table {req.name.lower()} "
                f"(page_type={int(req)})"
            )

    def _bounds(pt: PageType) -> tuple[int, int] | None:
        return tables.get(int(pt))

    # --- optional consumed lookup tables (malformed → G1c) ---
    artists: dict[int, str] = {}
    albums: dict[int, str] = {}
    genres: dict[int, str] = {}
    labels: dict[int, str] = {}
    keys: dict[int, str] = {}
    # colors parsed for G1c integrity; SourceTrack has no color field yet
    colors: dict[int, str] = {}

    optional_loaders: list[tuple[PageType, object, str, dict]] = [
        (PageType.ARTISTS, _parse_artist_row, "artists", artists),
        (PageType.ALBUMS, _parse_album_row, "albums", albums),
        (PageType.GENRES, _parse_genre_row, "genres", genres),
        (PageType.LABELS, _parse_label_row, "labels", labels),
        (PageType.KEYS, _parse_key_row, "keys", keys),
        (PageType.COLORS, _parse_color_row, "colors", colors),
    ]
    for pt, parser, label, target in optional_loaders:
        b = _bounds(pt)
        if b is None:
            continue  # optional absence is fine
        first, last = b
        try:
            target.update(
                _load_id_name_table(data, len_page, first, last, pt, parser, label)
            )
        except UnsupportedFormatError as exc:
            raise UnsupportedFormatError(
                f"export.pdb {label} table present but unparseable: {exc}"
            ) from exc

    # --- tracks (required) ---
    tracks: dict[int, SourceTrack] = {}
    t_first, t_last = tables[int(PageType.TRACKS)]
    try:
        for page, bases in _walk_table_pages(
            data, len_page, t_first, t_last, int(PageType.TRACKS)
        ):
            for rb in bases:
                raw = _parse_track_row(page, rb)
                tid = int(raw["id"])
                raw_path = raw["file_path"]
                resolved = resolve_track_path(raw_path, drive_root)
                if resolved is None and raw_path:
                    warnings.append(
                        f"track id={tid}: could not resolve path {raw_path!r}"
                    )

                filename = raw["filename"] or PurePosixPath(raw_path).name
                suffix = PurePosixPath(filename).suffix
                file_type = suffix.lstrip(".").lower() if suffix else ""

                sample_rate = int(raw["sample_rate"])
                duration_s = int(raw["duration"])
                total_samples = (
                    duration_s * sample_rate if sample_rate > 0 and duration_s > 0 else None
                )

                tn = int(raw["track_number"])
                dn = int(raw["disc_number"])
                tracks[tid] = SourceTrack(
                    rb_id=tid,
                    title=raw["title"],
                    artist=artists.get(int(raw["artist_id"]), ""),
                    album=albums.get(int(raw["album_id"]), ""),
                    genre=genres.get(int(raw["genre_id"]), ""),
                    label=labels.get(int(raw["label_id"]), ""),
                    comment=raw["comment"],
                    composer=artists.get(int(raw["composer_id"]), ""),
                    remixer=artists.get(int(raw["remixer_id"]), ""),
                    year=int(raw["year"]),
                    track_number=tn if tn != 0 else None,
                    disc_number=dn if dn != 0 else None,
                    bpm=int(raw["tempo"]) / 100.0,
                    key_name=keys.get(int(raw["key_id"])) or None,
                    rating=int(raw["rating"]),
                    play_count=int(raw["play_count"]),
                    bitrate=int(raw["bitrate"]),
                    file_size=int(raw["file_size"]),
                    file_type=file_type,
                    sample_rate=sample_rate,
                    duration_s=duration_s,
                    total_samples=total_samples,
                    raw_path=raw_path,
                    resolved_path=resolved,
                    beatgrid=None,
                    cues=[],
                    artwork=None,
                )
    except UnsupportedFormatError:
        raise
    except Exception as exc:
        raise UnsupportedFormatError(
            f"export.pdb tracks table unparseable: {exc}"
        ) from exc

    # --- playlist tree (required) ---
    tree_rows: list[tuple[int, int, int, bool, str]] = []
    pt_first, pt_last = tables[int(PageType.PLAYLIST_TREE)]
    try:
        for page, bases in _walk_table_pages(
            data, len_page, pt_first, pt_last, int(PageType.PLAYLIST_TREE)
        ):
            for rb in bases:
                tree_rows.append(_parse_playlist_tree_row(page, rb))
    except UnsupportedFormatError:
        raise
    except Exception as exc:
        raise UnsupportedFormatError(
            f"export.pdb playlist_tree table unparseable: {exc}"
        ) from exc

    # --- playlist entries (required) ---
    # playlist_id → list of (entry_index, track_id)
    membership: dict[int, list[tuple[int, int]]] = {}
    pe_first, pe_last = tables[int(PageType.PLAYLIST_ENTRIES)]
    try:
        for page, bases in _walk_table_pages(
            data, len_page, pe_first, pe_last, int(PageType.PLAYLIST_ENTRIES)
        ):
            for rb in bases:
                entry_index, track_id, playlist_id = _parse_playlist_entry_row(
                    page, rb
                )
                membership.setdefault(playlist_id, []).append((entry_index, track_id))
    except UnsupportedFormatError:
        raise
    except Exception as exc:
        raise UnsupportedFormatError(
            f"export.pdb playlist_entries table unparseable: {exc}"
        ) from exc

    for entries in membership.values():
        entries.sort(key=lambda x: x[0])

    playlists: list[SourcePlaylist] = []
    for parent_id, sort_order, pl_id, is_folder, name in tree_rows:
        track_ids = [tid for _, tid in membership.get(pl_id, [])]
        playlists.append(
            SourcePlaylist(
                rb_id=pl_id,
                parent_rb_id=parent_id,
                name=name,
                sort_order=sort_order,
                is_folder=is_folder,
                track_rb_ids=track_ids,
            )
        )
    # Stable order: sort_order within parent, then id.
    playlists.sort(key=lambda p: (p.parent_rb_id, p.sort_order, p.rb_id))

    # Silence unused-dict lint for colors (parsed for G1c only).
    _ = colors

    return SourceLibrary(
        drive_root=drive_root,
        tracks=tracks,
        playlists=playlists,
        warnings=warnings,
    )
