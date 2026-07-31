"""Sibling ordering and Engine's unique-name-per-folder renaming.

Shared by the writer, which applies these names, and by verify, which has to
predict them in order to pair each source playlist with the engine list it
became.

The two must travel together: the suffix a duplicate receives depends on the
sibling ordering, so a caller that re-derived only the rename would still
disagree with the writer whenever the ordering mattered. Verify re-deriving both
by hand is what produced the pairing defects this module exists to prevent.

Paths are tuples of titles rather than a joined string because a rekordbox
playlist name may itself contain a separator character; joining is for display
only.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence

from rb2engine.ir import SourcePlaylist

_DISPLAY_SEP = "/"


def sibling_order(playlists: Sequence[SourcePlaylist]) -> dict[int, list[int]]:
    """``parent_rb_id`` → child rb_ids in ``(sort_order, rb_id)`` order."""
    by_rb = {pl.rb_id: pl for pl in playlists}
    groups: dict[int, list[int]] = defaultdict(list)
    for pl in playlists:
        groups[pl.parent_rb_id].append(pl.rb_id)
    for rb_ids in groups.values():
        rb_ids.sort(key=lambda r: (by_rb[r].sort_order, r))
    return dict(groups)


def resolve_titles(playlists: Sequence[SourcePlaylist]) -> dict[int, str]:
    """``rb_id`` → the title the writer will give it.

    rekordbox permits two playlists with the same name in one folder; Engine
    does not (``C_NAME_UNIQUE_FOR_PARENT``). The second and later duplicates
    become ``"Name (2)"``, ``"Name (3)"``, … in sibling order, which is
    deterministic, so re-runs produce identical names.
    """
    by_rb = {pl.rb_id: pl for pl in playlists}
    titles: dict[int, str] = {}
    for rb_ids in sibling_order(playlists).values():
        seen: dict[str, int] = {}
        for rb in rb_ids:
            original = by_rb[rb].name
            count = seen.get(original, 0)
            seen[original] = count + 1
            titles[rb] = original if count == 0 else f"{original} ({count + 1})"
    return titles


def resolve_paths(
    playlists: Sequence[SourcePlaylist],
) -> dict[int, tuple[str, ...]]:
    """``rb_id`` → its resolved titles from the root, root first.

    A playlist is identified by its whole path: the same title may legally
    appear under several folders, since Engine's uniqueness constraint is
    per-parent.
    """
    by_rb = {pl.rb_id: pl for pl in playlists}
    titles = resolve_titles(playlists)
    paths: dict[int, tuple[str, ...]] = {}
    for rb in titles:
        parts: list[str] = []
        cur = rb
        walked: set[int] = set()
        # A parent cycle is rejected by the writer; guard anyway so verify
        # cannot hang on a malformed source.
        while cur != 0 and cur in by_rb and cur not in walked:
            walked.add(cur)
            parts.append(titles[cur])
            cur = by_rb[cur].parent_rb_id
        paths[rb] = tuple(reversed(parts))
    return paths


def format_path(path: Sequence[str]) -> str:
    """Human-readable form of a playlist path, for report field names."""
    return _DISPLAY_SEP.join(path)
