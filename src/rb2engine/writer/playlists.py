"""Playlist tree (parentListId/nextListId) and PlaylistEntity nextEntityId chains.

Engine stores two linked lists:

* ``Playlist.parentListId`` — folder hierarchy (0 = root);
  ``Playlist.nextListId`` — sibling order within a parent (tail → 0).
* ``PlaylistEntity.nextEntityId`` — track order within a playlist (tail → 0).

Because we always build fresh in final order, all ids are allocated up front and
each row's ``next*Id`` is set to its successor's id in a single pass. That
avoids rewiring and fighting the insert triggers that maintain the chains when
rows are appended one-at-a-time at the head.

Playlist entries whose source track was skipped (absent from ``track_id_map``)
are dropped rather than failing the playlist write.
"""

from __future__ import annotations

import logging
import sqlite3
from collections import defaultdict
from collections.abc import Mapping, MutableMapping, Sequence

from rb2engine.chain import walk_entity_chain
from rb2engine.ir import SourcePlaylist
from rb2engine.playlist_naming import resolve_titles

# Engine / libdjinterop sentinel: tail of every next*Id chain.
_NO_NEXT = 0

# Determinism: no wall-clock in writer/. Fixed epoch string for lastEditTime.
_LAST_EDIT_TIME = "1970-01-01 00:00:00"


logger = logging.getLogger(__name__)


def insert_playlists(
    conn: sqlite3.Connection,
    playlists: Sequence[SourcePlaylist],
    *,
    track_id_map: Mapping[int, int],
    intended_out: MutableMapping[int, Sequence[int]] | None = None,
) -> int:
    """Insert Playlist + PlaylistEntity rows for *playlists*.

    Parameters
    ----------
    conn:
        Open connection to an Engine m.db (schema already applied, Information
        row present).
    playlists:
        Source playlist tree. Sibling order is ``sort_order`` ascending within
        each ``parent_rb_id`` group.
    track_id_map:
        ``SourceTrack.rb_id`` → Engine ``Track.id``. Entries whose rb_id is
        missing are skipped (soft track skips must not fail playlist write).
    intended_out:
        Optional sink receiving ``listId`` → intended track order. The caller
        needs it to re-check the database *after* it has been committed and
        copied to the target volume; the in-transaction check below cannot see
        that far.

    Returns
    -------
    int
        Number of playlists written (folders and leaves).
    """
    if not playlists:
        return 0

    info_row = conn.execute("SELECT uuid FROM Information LIMIT 1").fetchone()
    if info_row is None:
        raise RuntimeError(
            "insert_playlists requires an Information row (create_m_db first)"
        )
    database_uuid: str = info_row[0]

    # Stable allocation: one engine id per source playlist, 1..N in input
    # order so parents that appear before children keep simple id maps. Source
    # parent_rb_id references rb_id, not list position — build rb→engine map.
    by_rb: dict[int, SourcePlaylist] = {}
    for pl in playlists:
        if pl.rb_id in by_rb:
            raise ValueError(f"duplicate SourcePlaylist.rb_id: {pl.rb_id}")
        by_rb[pl.rb_id] = pl

    # Engine Playlist.id allocation (explicit PRIMARY KEY values).
    # Order of allocation does not need to match sibling order; only the
    # nextListId pointers define order. Allocate in a parents-before-children
    # wave so parentListId always resolves.
    engine_id_of: dict[int, int] = {}
    next_engine_id = 1
    remaining = set(by_rb)
    while remaining:
        progress = False
        for rb_id in sorted(remaining, key=lambda r: (by_rb[r].sort_order, r)):
            parent_rb = by_rb[rb_id].parent_rb_id
            if parent_rb != 0 and parent_rb not in engine_id_of:
                if parent_rb not in by_rb:
                    raise ValueError(
                        f"playlist rb_id={rb_id} parent_rb_id={parent_rb} "
                        f"is not present in the playlists list"
                    )
                continue  # parent not yet allocated
            engine_id_of[rb_id] = next_engine_id
            next_engine_id += 1
            remaining.discard(rb_id)
            progress = True
        if not progress:
            raise ValueError(
                "playlist parent cycle or unresolved parents: "
                f"{sorted(remaining)}"
            )

    # Group siblings by parent engine id, ordered by sort_order then rb_id.
    siblings: dict[int, list[int]] = defaultdict(list)  # parent_engine → [rb_id]
    for rb_id, pl in by_rb.items():
        parent_engine = 0 if pl.parent_rb_id == 0 else engine_id_of[pl.parent_rb_id]
        siblings[parent_engine].append(rb_id)
    for group in siblings.values():
        group.sort(key=lambda r: (by_rb[r].sort_order, r))

    # nextListId: each sibling points at the next sibling's engine id; tail → 0.
    next_list_of: dict[int, int] = {}  # engine_id → nextListId
    for group in siblings.values():
        engine_ids = [engine_id_of[rb] for rb in group]
        for i, eid in enumerate(engine_ids):
            if i + 1 < len(engine_ids):
                next_list_of[eid] = engine_ids[i + 1]
            else:
                next_list_of[eid] = _NO_NEXT

    # Disambiguate duplicate sibling names.
    #
    # rekordbox permits two playlists with the same name in the same folder;
    # Engine's schema does NOT (CONSTRAINT C_NAME_UNIQUE_FOR_PARENT UNIQUE
    # (title, parentListId)). A real library hit this: "Setlist Bigroom" x3 and
    # "Setlist Classic" x2 under one folder, which aborted the whole write.
    #
    # Renaming beats dropping — the user keeps every playlist and can see what
    # happened. The suffix is assigned in the already-deterministic sibling
    # order (sort_order, rb_id), so re-runs produce identical names and the
    # determinism guarantee holds.
    # The algorithm lives in playlist_naming so verify predicts exactly what we
    # write; it must not be reimplemented on either side.
    title_of = resolve_titles(playlists)
    renamed: list[tuple[str, str]] = [
        (by_rb[rb].name, title_of[rb])
        for rb in by_rb
        if title_of[rb] != by_rb[rb].name
    ]
    if renamed:
        logger.warning(
            "renamed %d playlist(s) to satisfy Engine's unique-name-per-folder "
            "constraint (rekordbox allows duplicates, Engine does not): %s",
            len(renamed),
            ", ".join(f"{o!r} -> {n!r}" for o, n in renamed[:5]),
        )

    # Insert all Playlist rows with precomputed nextListId. Explicit ids mean
    # the insert triggers (which rewire nextListId for head-insert) find no
    # colliding nextListId under the same parent and leave our pointers alone.
    for rb_id, pl in by_rb.items():
        eid = engine_id_of[rb_id]
        parent_engine = 0 if pl.parent_rb_id == 0 else engine_id_of[pl.parent_rb_id]
        conn.execute(
            """
            INSERT INTO Playlist (
                id, title, parentListId, isPersisted, nextListId,
                lastEditTime, isExplicitlyExported
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                eid,
                title_of[rb_id],
                parent_engine,
                1,  # isPersisted: export all converted lists as visible
                next_list_of[eid],
                _LAST_EDIT_TIME,
                1,  # isExplicitlyExported
            ),
        )

    # Keep AUTOINCREMENT sequence ahead of our explicit ids.
    max_id = next_engine_id - 1
    if max_id > 0:
        _bump_sequence(conn, "Playlist", max_id)

    # PlaylistEntity chains — one chain per non-folder (or any list with tracks).
    # Pre-allocate entity ids across all playlists so a single pass sets
    # nextEntityId to the successor id without rewiring.
    entity_rows: list[tuple[int, int, int, str, int, int]] = []
    # (id, listId, trackId, databaseUuid, nextEntityId, membershipReference)
    next_entity_id = 1

    duplicate_entries = 0
    # listId → the exact track order this function intends to write. Kept so
    # the write can be read back and checked against intent (see below).
    intended: dict[int, list[int]] = {}
    for rb_id, pl in by_rb.items():
        list_id = engine_id_of[rb_id]
        # Preserve source order; drop members whose tracks were skipped.
        #
        # Also de-duplicate within the playlist: rekordbox lets the same track
        # appear more than once in one playlist, Engine does not (CONSTRAINT
        # C_NAME_UNIQUE_FOR_LIST UNIQUE (listId, databaseUuid, trackId)). A real
        # library hit this. Keeping the FIRST occurrence preserves the position
        # the DJ actually built around; dropping the playlist would be far worse
        # than dropping a repeat.
        engine_track_ids = []
        seen_tracks: set[int] = set()
        for rb in pl.track_rb_ids:
            if rb not in track_id_map:
                continue
            engine_track_id = track_id_map[rb]
            if engine_track_id in seen_tracks:
                duplicate_entries += 1
                continue
            seen_tracks.add(engine_track_id)
            engine_track_ids.append(engine_track_id)
        if not engine_track_ids:
            continue

        n = len(engine_track_ids)
        intended[list_id] = engine_track_ids
        entity_ids = list(range(next_entity_id, next_entity_id + n))
        next_entity_id += n
        for i, track_id in enumerate(engine_track_ids):
            eid = entity_ids[i]
            nxt = entity_ids[i + 1] if i + 1 < n else _NO_NEXT
            entity_rows.append(
                (eid, list_id, track_id, database_uuid, nxt, 0)
            )

    if entity_rows:
        conn.executemany(
            """
            INSERT INTO PlaylistEntity (
                id, listId, trackId, databaseUuid, nextEntityId,
                membershipReference
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            entity_rows,
        )
        _bump_sequence(conn, "PlaylistEntity", next_entity_id - 1)

    assert_entities_match_intent(conn, intended)
    if intended_out is not None:
        intended_out.update(intended)

    if duplicate_entries:
        logger.warning(
            "dropped %d duplicate playlist entrie(s): rekordbox allows the same "
            "track twice in one playlist, Engine does not; first occurrence kept",
            duplicate_entries,
        )

    return len(playlists)


def assert_entities_match_intent(
    conn: sqlite3.Connection, intended: Mapping[int, Sequence[int]]
) -> None:
    """Read every written chain back and fail if it differs from intent.

    The writer builds each chain in memory and inserts it in one pass, so in
    principle the database must agree. In practice a conversion shipped two
    playlists containing a track that appeared nowhere in the source, and
    ``convert`` still exited 0 — the corruption was only found later by
    ``verify``. Checking here turns that class of silent wrong-database into a
    build-time failure, before the new m.db is swapped into place.

    Every row in the table is examined, not just the lists we meant to fill: a
    spurious row landing on a folder or an empty playlist is precisely as wrong
    as one landing on a playlist we wrote, and scoping the check to *intended*
    would leave that whole class invisible.
    """
    grouped: dict[int, list[tuple[int, int, int]]] = defaultdict(list)
    for list_id, eid, tid, nxt in conn.execute(
        "SELECT listId, id, trackId, nextEntityId FROM PlaylistEntity"
    ):
        grouped[int(list_id)].append((int(eid), int(tid), int(nxt)))

    unexpected_lists = sorted(set(grouped) - set(intended))
    if unexpected_lists:
        raise RuntimeError(
            f"PlaylistEntity rows exist for listId(s) {unexpected_lists} that "
            "no playlist write intended"
        )

    for list_id, expected in intended.items():
        actual = walk_entity_chain(list_id, grouped.get(list_id, []))
        if list(actual) != list(expected):
            extra = sorted(set(actual) - set(expected))
            missing = sorted(set(expected) - set(actual))
            raise RuntimeError(
                f"playlist listId={list_id}: written entries do not match the "
                f"{len(expected)} intended ({len(actual)} written; "
                f"unexpected track ids {extra or 'none'}; "
                f"absent track ids {missing or 'none'})"
            )


def _bump_sequence(conn: sqlite3.Connection, table: str, seq: int) -> None:
    """Ensure sqlite_sequence.seq >= *seq* after explicit PRIMARY KEY inserts.

    ``sqlite_sequence`` has no PRIMARY KEY, so UPSERT is unavailable; delete +
    insert is the portable form.
    """
    conn.execute("DELETE FROM sqlite_sequence WHERE name = ?", (table,))
    conn.execute(
        "INSERT INTO sqlite_sequence(name, seq) VALUES (?, ?)",
        (table, seq),
    )
