"""AlbumArt rows + content_key dedup; first-seen order pins AUTOINCREMENT ids.

Image bytes are BLOBs inside m.db (no images/ directory). Track.albumArtId is
wired by writer/tracks.py via the returned content_key → id map; Track.albumArt
URI is also set there (shape image://planck/0).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from pathlib import Path

from rb2engine.ir import SourceArtwork
from rb2engine.progress import ItemCallback
from rb2engine.reader.artwork import read_artwork_bytes


def _load_image_bytes(art: SourceArtwork) -> bytes | None:
    """Load BLOB payload for a SourceArtwork without keeping library-scale caches.

    Prefer reader.artwork.read_artwork_bytes (embedded re-extract / pdb file).
    Fall back to raw path read so tests and plain image paths still work when
    source is not tagged as pdb/embedded.
    """
    data = read_artwork_bytes(art)
    if data:
        return data
    if art.path is None:
        return None
    path = Path(art.path)
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    return raw or None


def insert_artwork(
    conn: sqlite3.Connection,
    arts: Sequence[SourceArtwork],
    *,
    on_progress: ItemCallback | None = None,
) -> dict[str, int]:
    """Insert deduped AlbumArt rows in FIRST-SEEN order.

    That order fixes AUTOINCREMENT ids and therefore every Track.albumArtId in
    a canonical dump. Returns content_key → AlbumArt.id. Duplicate content_keys
    in *arts* reuse the first-seen id (no second row).

    *on_progress* receives ``(done, total)`` after each item. This loop
    re-reads the image bytes out of every source audio file, so on a large
    library it is one of the two phases worth reporting.
    """
    id_by_key: dict[str, int] = {}
    total = len(arts)
    for done, art in enumerate(arts, start=1):
        # finally, not tail-of-loop: the skip paths below use `continue`, and a
        # bar that stalls on a run of deduped or unreadable art looks like a hang.
        try:
            key = art.content_key
            if key in id_by_key:
                continue
            payload = _load_image_bytes(art)
            if payload is None:
                # Unreadable art is skipped rather than inserting an empty BLOB —
                # callers leave albumArtId NULL for missing keys.
                continue
            cur = conn.execute(
                "INSERT INTO AlbumArt (hash, albumArt) VALUES (?, ?)",
                (key, payload),
            )
            art_id = cur.lastrowid
            if art_id is None:
                raise RuntimeError("AlbumArt INSERT did not produce lastrowid")
            id_by_key[key] = int(art_id)
        finally:
            if on_progress is not None:
                on_progress(done, total)
    return id_by_key
