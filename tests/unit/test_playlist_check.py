"""Problem reporting for the shared playlist comparison.

WHY these are worth their own test: ``describe()`` is what a DJ actually sees
when a conversion is refused. Every branch of it is reachable from the writer's
pre-publish gate, but only the track-order one is exercised by the integration
tests, so the other two could rot into something unreadable without any test
noticing.
"""

from __future__ import annotations

from rb2engine.playlist_check import (
    CHAIN,
    MISSING,
    TRACK_ORDER,
    PlaylistProblem,
)


def test_missing_playlist_reads_as_absent() -> None:
    p = PlaylistProblem(
        ("Sets", "Warmup"), MISSING, expected="present", actual="absent"
    )

    assert p.label == "Sets/Warmup"
    assert p.describe() == "playlist 'Sets/Warmup' is absent from the database"


def test_broken_chain_names_the_chain_problem() -> None:
    p = PlaylistProblem(
        ("Main",),
        CHAIN,
        expected="every row reachable from the nextEntityId chain",
        actual="row 7 is unreachable",
    )

    assert "broken entry chain" in p.describe()
    assert "row 7 is unreachable" in p.describe()


def test_track_order_names_both_directions_of_the_difference() -> None:
    """The message must say what is extra AND what is absent.

    WHY: the incident this check exists for was an *extra* entry with nothing
    missing. A message that only reported absences would have described that
    database as fine.
    """
    p = PlaylistProblem(
        ("Organic House",), TRACK_ORDER, expected=[1, 2, 3], actual=[1, 2, 9, 3]
    )

    text = p.describe()

    assert "4 entries written" in text
    assert "3 expected" in text
    assert "unexpected track ids [9]" in text
    assert "absent track ids none" in text
