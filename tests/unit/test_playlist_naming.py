"""Contract tests for the naming shared by the writer and verify.

WHY these are direct rather than only exercised through both callers: the whole
point of the module is that two independent consumers agree. If either drifts,
an integration test on one side can stay green while the pair silently
disagrees — which is the class of defect the module was extracted to end.
"""

from __future__ import annotations

from rb2engine.ir import SourcePlaylist
from rb2engine.playlist_naming import format_path, resolve_paths, resolve_titles


def _pl(rb_id: int, name: str, *, parent: int = 0, sort: int = 0) -> SourcePlaylist:
    return SourcePlaylist(
        rb_id=rb_id,
        parent_rb_id=parent,
        name=name,
        sort_order=sort,
        is_folder=False,
        track_rb_ids=[],
    )


def test_suffixes_follow_sibling_order_not_input_order() -> None:
    """The suffix depends on (sort_order, rb_id), never on list position.

    WHY: this is why the ordering and the rename must live in one function. A
    caller that re-derived only the rename would suffix whichever duplicate it
    happened to see first and disagree with the writer.
    """
    playlists = [
        _pl(3, "Setlist", sort=2),
        _pl(1, "Setlist", sort=0),
        _pl(2, "Setlist", sort=1),
    ]

    titles = resolve_titles(playlists)

    assert titles[1] == "Setlist"
    assert titles[2] == "Setlist (2)"
    assert titles[3] == "Setlist (3)"


def test_duplicate_names_in_different_folders_are_not_renamed() -> None:
    """Engine's constraint is per-parent, so siblings-only collisions rename."""
    playlists = [
        _pl(1, "Folder A"),
        _pl(2, "Folder B", sort=1),
        _pl(3, "Chill", parent=1),
        _pl(4, "Chill", parent=2),
    ]

    titles = resolve_titles(playlists)

    assert titles[3] == "Chill"
    assert titles[4] == "Chill"


def test_paths_distinguish_same_named_playlists() -> None:
    playlists = [
        _pl(1, "Folder A"),
        _pl(2, "Folder B", sort=1),
        _pl(3, "Chill", parent=1),
        _pl(4, "Chill", parent=2),
    ]

    paths = resolve_paths(playlists)

    assert paths[3] == ("Folder A", "Chill")
    assert paths[4] == ("Folder B", "Chill")
    assert paths[3] != paths[4]


def test_path_is_a_tuple_so_a_name_containing_the_separator_stays_distinct() -> None:
    """A "/" in a playlist name must not merge two different playlists.

    WHY: rekordbox permits "/" in names. Matching on a joined string would make
    a playlist literally named "A/B" collide with "B" inside folder "A"; only
    the display form joins.
    """
    playlists = [
        _pl(1, "A"),
        _pl(2, "B", parent=1),
        _pl(3, "A/B", sort=1),
    ]

    paths = resolve_paths(playlists)

    assert paths[2] == ("A", "B")
    assert paths[3] == ("A/B",)
    assert paths[2] != paths[3]
    # Display collapses them; matching does not.
    assert format_path(paths[2]) == format_path(paths[3]) == "A/B"
