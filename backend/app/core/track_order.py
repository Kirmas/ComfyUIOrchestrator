"""Per-project track ordering as a doubly-linked list (Track.prev_track_id /
Track.next_track_id), replacing the old dense, reindexed row_index column.

The visible "track N" number is derived from a track's position in this order
at render time and never persisted, so it can't gap or desync -- and a
delete/insert is a pointer splice (a couple of writes), never a bulk renumber
of every track below it. That non-atomic renumber (a partial Promise.all of
row_index PATCHes) was the 2026-07-21 data-loss surface this module exists to
remove.

Anything that needs "the track above/below" or a positional offset
(cell_index input resolution, a workflow node's row-span, the output-binding
range check) resolves it through ordered_tracks() here rather than by integer
arithmetic on a stored column.
"""
import uuid

from sqlalchemy import select

from app.db.models import Track


async def ordered_tracks(db, project_id) -> list[Track]:
    """Every track of a project in list order (head -> next -> ...).

    Defensive against a broken chain: it starts from the head (the track whose
    prev is None), guards against cycles with a visited set, and appends any
    track the walk never reached (an orphan left by a crash mid-splice) at the
    end instead of dropping it. A corrupted chain therefore degrades to "a few
    rows in an odd order", never "rows silently vanish" -- the opposite
    failure mode from the gapped row_index it replaces.
    """
    result = await db.execute(select(Track).where(Track.project_id == project_id))
    tracks = list(result.scalars().all())
    if not tracks:
        return []
    by_id = {t.id: t for t in tracks}
    heads = [t for t in tracks if t.prev_track_id is None]
    # One head is the healthy case. If the chain is broken (zero or several),
    # pick a deterministic start so at least the order is stable run-to-run.
    start = heads[0] if heads else min(tracks, key=lambda t: str(t.id))
    ordered: list[Track] = []
    seen: set[uuid.UUID] = set()
    cur: Track | None = start
    while cur is not None and cur.id not in seen:
        seen.add(cur.id)
        ordered.append(cur)
        cur = by_id.get(cur.next_track_id) if cur.next_track_id else None
    for t in tracks:
        if t.id not in seen:
            ordered.append(t)
            seen.add(t.id)
    return ordered


async def unlink_track(db, track: Track) -> None:
    """Detach `track` from its chain, stitching its two neighbours together.
    Leaves `track` with null prev/next. Does not delete the row or commit."""
    prev = await db.get(Track, track.prev_track_id) if track.prev_track_id else None
    nxt = await db.get(Track, track.next_track_id) if track.next_track_id else None
    if prev is not None:
        prev.next_track_id = track.next_track_id
    if nxt is not None:
        nxt.prev_track_id = track.prev_track_id
    track.prev_track_id = None
    track.next_track_id = None


async def splice_after(db, project_id, track: Track, after: Track | None, *, at_head: bool = False) -> None:
    """Insert `track` into the chain. `track` must already be detached (a
    freshly created row, or one passed through unlink_track first).

    - after is a track       -> place `track` immediately after it.
    - after is None, at_head -> make `track` the new head.
    - after is None, not at_head is not a valid call (append-at-tail is
      expressed by passing the current tail as `after`); the caller resolves
      the tail via ordered_tracks()[-1].

    Fixes all affected pointers. Does not commit.
    """
    if after is None:
        if not at_head:
            # Empty project: `track` is simply the whole list.
            return
        result = await db.execute(
            select(Track).where(
                Track.project_id == project_id,
                Track.prev_track_id.is_(None),
                Track.id != track.id,
            )
        )
        old_head = result.scalars().first()
        track.prev_track_id = None
        track.next_track_id = old_head.id if old_head is not None else None
        if old_head is not None:
            old_head.prev_track_id = track.id
        return

    nxt = await db.get(Track, after.next_track_id) if after.next_track_id else None
    track.prev_track_id = after.id
    track.next_track_id = after.next_track_id
    after.next_track_id = track.id
    if nxt is not None:
        nxt.prev_track_id = track.id
