"""Tests for writer/playlists.py — dual linked-list playlist tree.

WHY: Engine reconstructs folder hierarchy from Playlist.parentListId +
nextListId (sibling chain) and track order from PlaylistEntity.nextEntityId.
If either chain is wrong, Engine silently shows wrong order or a broken tree —
there is no runtime error. These tests reconstruct both chains exactly as
libdjinterop/Engine do (walk from sentinel 0 backwards, reverse) and compare
to the SourcePlaylist input, so a off-by-one next*Id fails loudly.

Expected values come from the hand-authored SourcePlaylist fixtures below and
from Engine-observed sentinels (nextListId/nextEntityId tail = 0), never from
running our own writer and pasting its output.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from rb2engine.ir import SourcePlaylist
from rb2engine.writer import schema as schema_mod
from rb2engine.writer.playlists import insert_playlists

# Engine / libdjinterop sentinel: tail of every next*Id chain points at 0.
_NO_NEXT = 0


def _open_empty_db(tmp_path: Path) -> sqlite3.Connection:
    path = tmp_path / "m.db"
    return schema_mod.create_database(path, (3, 0, 1), database_uuid="test-uuid-playlists")


def _sibling_order(conn: sqlite3.Connection, parent_list_id: int) -> list[tuple[int, str]]:
    """Reconstruct sibling order the way Engine does: tail (next=0) → head."""
    rows = conn.execute(
        "SELECT id, title, nextListId FROM Playlist WHERE parentListId = ?",
        (parent_list_id,),
    ).fetchall()
    by_next = {next_id: (id_, title) for id_, title, next_id in rows}
    order: list[tuple[int, str]] = []
    curr = _NO_NEXT
    while curr in by_next:
        id_, title = by_next[curr]
        order.insert(0, (id_, title))
        curr = id_
    return order


def _entity_track_order(conn: sqlite3.Connection, list_id: int) -> list[int]:
    """Reconstruct track order from nextEntityId chain (tail sentinel = 0)."""
    rows = conn.execute(
        "SELECT id, trackId, nextEntityId FROM PlaylistEntity WHERE listId = ?",
        (list_id,),
    ).fetchall()
    by_next = {next_id: (id_, track_id) for id_, track_id, next_id in rows}
    order: list[int] = []
    curr = _NO_NEXT
    while curr in by_next:
        id_, track_id = by_next[curr]
        order.insert(0, track_id)
        curr = id_
    return order


def test_playlist_tree_and_track_order_reconstruct_from_linked_lists(
    tmp_path: Path,
) -> None:
    """Folder hierarchy + sibling order + per-playlist track order must round-trip.

    Tree under test (sort_order dictates sibling position):
        Root
        ├── FolderA (folder)          sort 0
        │   ├── NestedPL              sort 0  tracks [10, 20, 30]
        │   └── NestedPL2             sort 1  tracks [40]
        └── RootPL                    sort 1  tracks [20, 10]
    """
    playlists = [
        SourcePlaylist(
            rb_id=1,
            parent_rb_id=0,
            name="FolderA",
            sort_order=0,
            is_folder=True,
            track_rb_ids=[],
        ),
        SourcePlaylist(
            rb_id=2,
            parent_rb_id=1,
            name="NestedPL",
            sort_order=0,
            is_folder=False,
            track_rb_ids=[10, 20, 30],
        ),
        SourcePlaylist(
            rb_id=3,
            parent_rb_id=1,
            name="NestedPL2",
            sort_order=1,
            is_folder=False,
            track_rb_ids=[40],
        ),
        SourcePlaylist(
            rb_id=4,
            parent_rb_id=0,
            name="RootPL",
            sort_order=1,
            is_folder=False,
            track_rb_ids=[20, 10],
        ),
    ]
    # rb_id → engine Track.id (pre-assigned as if insert_tracks already ran)
    track_id_map = {10: 101, 20: 102, 30: 103, 40: 104}

    conn = _open_empty_db(tmp_path)
    try:
        n = insert_playlists(conn, playlists, track_id_map=track_id_map)
        conn.commit()

        assert n == 4

        # Root siblings: FolderA then RootPL
        root = _sibling_order(conn, 0)
        assert [title for _, title in root] == ["FolderA", "RootPL"]

        folder_id = root[0][0]
        root_pl_id = root[1][0]

        # FolderA children: NestedPL then NestedPL2
        children = _sibling_order(conn, folder_id)
        assert [title for _, title in children] == ["NestedPL", "NestedPL2"]

        nested_id = children[0][0]
        nested2_id = children[1][0]

        # parentListId wiring
        parent_of = {
            row[0]: row[1]
            for row in conn.execute("SELECT id, parentListId FROM Playlist")
        }
        assert parent_of[folder_id] == 0
        assert parent_of[root_pl_id] == 0
        assert parent_of[nested_id] == folder_id
        assert parent_of[nested2_id] == folder_id

        # Track order via entity chain (engine track ids, not rb ids)
        assert _entity_track_order(conn, nested_id) == [101, 102, 103]
        assert _entity_track_order(conn, nested2_id) == [104]
        assert _entity_track_order(conn, root_pl_id) == [102, 101]
        # Folders have no entities
        assert _entity_track_order(conn, folder_id) == []

        # Tail sentinels are 0 (Engine convention)
        tails = conn.execute(
            "SELECT nextListId FROM Playlist WHERE nextListId = 0"
        ).fetchall()
        assert len(tails) >= 1
        entity_tails = conn.execute(
            "SELECT nextEntityId FROM PlaylistEntity WHERE nextEntityId = 0"
        ).fetchall()
        assert len(entity_tails) == 3  # three non-empty playlists

        # databaseUuid on entities matches Information.uuid
        info_uuid = conn.execute("SELECT uuid FROM Information").fetchone()[0]
        uuids = {
            r[0]
            for r in conn.execute("SELECT DISTINCT databaseUuid FROM PlaylistEntity")
        }
        assert uuids == {info_uuid}
    finally:
        conn.close()


def test_skips_playlist_entries_whose_track_was_skipped(tmp_path: Path) -> None:
    """Missing track_id_map entries must be dropped, not raise.

    WHY: convert continues after soft track skips (exit 1). Playlist rows must
    still be written; only the missing members vanish. Failing here would turn
    a soft track skip into a fatal playlist failure.
    """
    playlists = [
        SourcePlaylist(
            rb_id=1,
            parent_rb_id=0,
            name="Partial",
            sort_order=0,
            is_folder=False,
            track_rb_ids=[1, 2, 3, 4],  # 2 and 4 were skipped at track stage
        ),
    ]
    track_id_map = {1: 11, 3: 33}  # 2 and 4 absent

    conn = _open_empty_db(tmp_path)
    try:
        n = insert_playlists(conn, playlists, track_id_map=track_id_map)
        conn.commit()
        assert n == 1
        list_id = conn.execute("SELECT id FROM Playlist").fetchone()[0]
        assert _entity_track_order(conn, list_id) == [11, 33]
        count = conn.execute("SELECT COUNT(*) FROM PlaylistEntity").fetchone()[0]
        assert count == 2
    finally:
        conn.close()


def test_empty_playlist_has_no_entities(tmp_path: Path) -> None:
    """An empty leaf playlist is still a Playlist row; zero entities."""
    playlists = [
        SourcePlaylist(
            rb_id=1,
            parent_rb_id=0,
            name="Empty",
            sort_order=0,
            is_folder=False,
            track_rb_ids=[],
        ),
    ]
    conn = _open_empty_db(tmp_path)
    try:
        assert insert_playlists(conn, playlists, track_id_map={}) == 1
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM Playlist").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM PlaylistEntity").fetchone()[0] == 0
    finally:
        conn.close()


def test_single_playlist_next_list_id_is_sentinel(tmp_path: Path) -> None:
    """Sole root playlist is both head and tail → nextListId = 0."""
    playlists = [
        SourcePlaylist(
            rb_id=9,
            parent_rb_id=0,
            name="Only",
            sort_order=0,
            is_folder=False,
            track_rb_ids=[5],
        ),
    ]
    conn = _open_empty_db(tmp_path)
    try:
        insert_playlists(conn, playlists, track_id_map={5: 1})
        conn.commit()
        row = conn.execute(
            "SELECT nextListId, parentListId FROM Playlist WHERE title = 'Only'"
        ).fetchone()
        assert row == (0, 0)
        list_id = conn.execute("SELECT id FROM Playlist").fetchone()[0]
        ent = conn.execute(
            "SELECT nextEntityId FROM PlaylistEntity WHERE listId = ?",
            (list_id,),
        ).fetchone()
        assert ent == (0,)
    finally:
        conn.close()


def test_returns_zero_for_empty_input(tmp_path: Path) -> None:
    conn = _open_empty_db(tmp_path)
    try:
        assert insert_playlists(conn, [], track_id_map={}) == 0
        assert conn.execute("SELECT COUNT(*) FROM Playlist").fetchone()[0] == 0
    finally:
        conn.close()
