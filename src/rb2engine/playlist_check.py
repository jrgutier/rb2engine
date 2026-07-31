"""Compare an Engine database's playlists against the source they came from.

Shared by ``verify`` and by the writer's pre-publish gate. The two react
differently — verify records a finding and keeps checking, the writer refuses to
publish — but they must never disagree about what is wrong, which is why the
comparison itself lives here.

Why this exists separately from ``assert_entities_match_intent``
---------------------------------------------------------------
That gate compares the database against what ``insert_playlists`` *intended* to
write, and both sides of it derive from ``track_id_map``. A mapping fault is
therefore invisible to it: the intent and the rows agree with each other while
both disagree with the source.

This module never receives ``track_id_map``. It recomputes each expected Engine
track id from the source track itself, through ``map_track`` and the database's
own ``Track.path`` index — the same route Engine will use to find the file. So
it fails exactly where the intent gate cannot.

What it cannot do: it compares a parse against itself. Run inline during a
conversion it can never detect that the source *file* was misread, because both
sides descend from the same parse. Torn-source detection is the reader's job
(see ``reader/pdb.py`` G1d).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from rb2engine.chain import ChainInconsistent, walk_entity_chain
from rb2engine.ir import SourceLibrary, SourcePlaylist
from rb2engine.playlist_naming import format_path, resolve_paths

# Discrepancy kinds, kept as constants so verify's field names and the writer's
# error text cannot drift apart.
MISSING = "missing"
CHAIN = "chain"
TRACK_ORDER = "track_order"


@dataclass(frozen=True)
class PlaylistProblem:
    """One disagreement between a database playlist and its source."""

    path: tuple[str, ...]
    kind: str
    expected: object
    actual: object

    @property
    def label(self) -> str:
        return format_path(self.path)

    def describe(self) -> str:
        if self.kind == MISSING:
            return f"playlist {self.label!r} is absent from the database"
        if self.kind == CHAIN:
            return f"playlist {self.label!r} has a broken entry chain: {self.actual}"
        exp = cast(list[int], self.expected)
        act = cast(list[int], self.actual)
        extra = sorted(set(act) - set(exp))
        missing = sorted(set(exp) - set(act))
        return (
            f"playlist {self.label!r}: {len(act)} entries written, "
            f"{len(exp)} expected from the source "
            f"(unexpected track ids {extra or 'none'}; "
            f"absent track ids {missing or 'none'})"
        )


def db_playlist_paths(conn: sqlite3.Connection) -> dict[tuple[str, ...], int]:
    """Engine playlist path (root first) → list id.

    Built by walking ``parentListId`` to the root, so a title is only ever
    matched within its own folder.
    """
    rows: dict[int, tuple[str, int]] = {
        int(r[0]): (str(r[1]), int(r[2]))
        for r in conn.execute("SELECT id, title, parentListId FROM Playlist")
    }
    paths: dict[tuple[str, ...], int] = {}
    for list_id in rows:
        parts: list[str] = []
        cur = list_id
        walked: set[int] = set()
        # Guard against a cyclic parent chain in a database we did not write.
        while cur != 0 and cur in rows and cur not in walked:
            walked.add(cur)
            title, parent = rows[cur]
            parts.append(title)
            cur = parent
        paths[tuple(reversed(parts))] = list_id
    return paths


def db_track_ids_by_path(conn: sqlite3.Connection) -> dict[str, int]:
    """Engine ``Track.path`` → ``Track.id``."""
    return {
        str(path): int(tid) for tid, path in conn.execute("SELECT id, path FROM Track")
    }


def expected_entity_track_ids(
    pl: SourcePlaylist,
    source: SourceLibrary,
    *,
    track_id_by_path: Mapping[str, int],
    drive_root: Path,
    engine_lib: Path,
) -> list[int]:
    """Engine track ids this playlist should hold, derived from the source.

    Order is preserved. Tracks that were skipped during conversion have no
    database row and drop out here the same way, and a track repeated inside one
    playlist keeps only its first occurrence — Engine's uniqueness constraint
    does not permit the repeat.
    """
    # Resolved at call time, exactly as writer/build.py does. Binding it at
    # import would let this check run a different mapper than the writer just
    # used, which would make the comparison meaningless rather than independent.
    from rb2engine.mapper.track import map_track

    out: list[int] = []
    seen: set[int] = set()
    for rb in pl.track_rb_ids:
        src = source.tracks.get(rb)
        if src is None or src.resolved_path is None:
            continue
        et = map_track(src, drive_root=drive_root, engine_library_dir=engine_lib)
        db_id = track_id_by_path.get(et.path)
        if db_id is None or db_id in seen:
            continue
        seen.add(db_id)
        out.append(db_id)
    return out


def entity_track_order(
    conn: sqlite3.Connection, list_id: int
) -> tuple[list[int], str | None]:
    """Track order from the nextEntityId chain, plus any inconsistency found.

    Returns ``(order, problem)``. ``problem`` is None when the chain accounts
    for every row; otherwise it describes what is wrong and ``order`` holds the
    rows in id order as a best effort.

    The writer must abort on a problem and verify must record it and keep going,
    which is why this reports rather than decides.
    """
    rows = [
        (int(eid), int(track_id), int(next_id))
        for eid, track_id, next_id in conn.execute(
            "SELECT id, trackId, nextEntityId FROM PlaylistEntity WHERE listId = ?",
            (list_id,),
        )
    ]
    try:
        return walk_entity_chain(list_id, rows), None
    except ChainInconsistent as exc:
        return [track_id for _, track_id, _ in rows], str(exc)


def compare_playlists(
    source: SourceLibrary,
    conn: sqlite3.Connection,
    *,
    drive_root: Path,
    engine_lib: Path,
    track_id_by_path: Mapping[str, int] | None = None,
) -> list[PlaylistProblem]:
    """Every disagreement between *conn*'s playlists and *source*.

    Empty means the database's playlist membership, order and chain integrity
    all match what the source implies. ``track_id_by_path`` is optional purely
    to let a caller that already loaded the Track table avoid a second query.
    """
    if track_id_by_path is None:
        track_id_by_path = db_track_ids_by_path(conn)

    problems: list[PlaylistProblem] = []
    path_to_id = db_playlist_paths(conn)
    source_paths = resolve_paths(source.playlists)

    for pl in source.playlists:
        path = source_paths[pl.rb_id]
        list_id = path_to_id.get(path)
        if list_id is None:
            problems.append(
                PlaylistProblem(path, MISSING, expected="present", actual="absent")
            )
            continue

        expected = expected_entity_track_ids(
            pl,
            source,
            track_id_by_path=track_id_by_path,
            drive_root=drive_root,
            engine_lib=engine_lib,
        )
        actual, chain_problem = entity_track_order(conn, list_id)
        if chain_problem is not None:
            # Reported in its own right: a broken chain is a defect even when
            # the set of tracks happens to match what the source expected.
            problems.append(
                PlaylistProblem(
                    path,
                    CHAIN,
                    expected="every row reachable from the nextEntityId chain",
                    actual=chain_problem,
                )
            )
        if expected != actual:
            problems.append(
                PlaylistProblem(path, TRACK_ORDER, expected=expected, actual=actual)
            )

    return problems
