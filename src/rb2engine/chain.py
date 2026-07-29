"""The one place that reconstructs an Engine ``nextEntityId`` chain.

Engine stores playlist membership as a singly linked list: each
``PlaylistEntity`` row points at its successor and the tail points at 0. Track
order is whatever that chain says, so anything wanting to know "what does this
playlist actually contain" has to walk it.

WHY THIS IS SHARED
------------------
The walk existed in three places that had drifted apart. The writer's copy
treated a row the chain never reaches as fatal; ``verify``'s copy silently
returned the shorter, tidier list and reported no problem. Two oracles that
disagree about what "correct" means will eventually contradict each other on a
real stick — one refusing to publish a database the other calls clean.

They still need different *reactions*: the writer must abort before publishing,
while ``verify`` must record the finding and carry on checking. So the walk
raises, and each caller decides what that means. What they can no longer do is
disagree about whether there is something to react to.
"""

from __future__ import annotations

from collections.abc import Sequence

# Engine / libdjinterop sentinel: tail of every next*Id chain points at 0.
NO_NEXT = 0


class ChainInconsistent(RuntimeError):
    """A ``nextEntityId`` chain does not account for every row in its list.

    Subclasses ``RuntimeError`` because the writer's contract is to raise
    ``RuntimeError`` on a database it refuses to publish, and callers that only
    care about that broader promise should not need to know this type exists.
    """

    def __init__(self, list_id: int, message: str) -> None:
        super().__init__(f"playlist listId={list_id}: {message}")
        self.list_id = list_id


def walk_entity_chain(
    list_id: int, rows: Sequence[tuple[int, int, int]]
) -> list[int]:
    """Track ids for one list in chain order, from ``(id, trackId, nextEntityId)``.

    Walking the chain rather than reading rows in id order is deliberate: a row
    spliced into the middle, or one no walk reaches, is invisible to a plain
    ``ORDER BY id`` comparison. A real conversion published a spurious entry
    second-to-last, exactly where row order would have hidden it.

    Raises ``ChainInconsistent`` if the chain forks or fails to reach every row.
    """
    by_next: dict[int, tuple[int, int]] = {}
    for eid, tid, nxt in rows:
        # Two rows sharing a successor would silently collapse into one dict
        # entry. The count check below would still fire, but it would blame the
        # wrong thing, so name this corruption for what it is.
        if int(nxt) in by_next:
            raise ChainInconsistent(
                list_id,
                f"two PlaylistEntity rows share nextEntityId={int(nxt)} — "
                "chain is forked",
            )
        by_next[int(nxt)] = (int(eid), int(tid))

    order: list[int] = []
    curr = NO_NEXT
    seen: set[int] = set()
    while curr in by_next:
        eid, track_id = by_next[curr]
        if eid in seen:  # corrupt chain must not spin forever
            break
        seen.add(eid)
        order.insert(0, track_id)
        curr = eid

    # A row that no chain walk reaches is still a row Engine may honour; make
    # the count mismatch loud instead of letting the walk hide it.
    if len(order) != len(rows):
        raise ChainInconsistent(
            list_id,
            f"{len(rows)} PlaylistEntity rows but the nextEntityId chain "
            f"reaches {len(order)} — chain is inconsistent",
        )
    return order
