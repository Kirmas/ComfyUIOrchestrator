import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.grid_layout import compute_layout
from app.core.track_order import ordered_tracks, scope_of, splice_after, unlink_track
from app.db.base import get_db
from app.db.models import Node, Track
from app.schemas.schemas import NodeRead, TrackCreate, TrackRead

router = APIRouter(prefix="/api/tracks", tags=["tracks"])


@router.post("", response_model=TrackRead, status_code=201)
async def create_track(payload: TrackCreate, db: AsyncSession = Depends(get_db)):
    """Ordering is a per-scope linked list (core/track_order.py), so a new
    track is placed *relative* to the others -- after a given track, at the
    head, or (default) appended at the tail -- never at a numeric row_index
    the caller has to compute and keep contiguous."""
    dashboard_id = payload.dashboard_id
    after: Track | None = None
    if payload.after_track_id is not None:
        after = await db.get(Track, payload.after_track_id)
        if after is None:
            raise HTTPException(404, "after_track_id not found")
        # The anchor decides the scope: splicing into a list means joining it,
        # so a mismatched dashboard_id from the client can't split a chain
        # across two grids.
        dashboard_id = after.dashboard_id
    elif not payload.place_at_head:
        # No anchor given and not head -> append at this scope's current tail.
        ordered = await ordered_tracks(db, payload.project_id, dashboard_id)
        after = ordered[-1] if ordered else None

    track = Track(
        project_id=payload.project_id,
        dashboard_id=dashboard_id,
        spawned_from_node_id=payload.spawned_from_node_id,
        spawned_from_output_id=payload.spawned_from_output_id,
    )
    db.add(track)
    await db.flush()
    await splice_after(db, payload.project_id, track, after, at_head=payload.place_at_head, dashboard_id=dashboard_id)
    await db.commit()
    await db.refresh(track)
    return track


@router.get("/{track_id}", response_model=TrackRead)
async def get_track(track_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    track = await db.get(Track, track_id)
    if not track:
        raise HTTPException(404, "Track not found")
    return track


@router.get("/{track_id}/nodes", response_model=list[NodeRead])
async def list_track_nodes(track_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Node).where(Node.track_id == track_id).order_by(Node.step_index))
    return result.scalars().all()


@router.delete("/{track_id}", status_code=204)
async def delete_track(track_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Splice the track out of its chain (stitching its neighbours together)
    then delete it -- a couple of pointer writes, no renumbering of anything
    else. The old reindex-every-track-below pass (and its non-atomic partial
    failures) is gone entirely; the visible row numbers just re-derive from
    the shorter list on the next load.

    Refused if the row is one of a multi-input workflow's own span rows (an
    input-slot row below the workflow's anchor): deleting it silently shrank
    the workflow's span and dropped an input. A row is a span row exactly when
    it shows up in compute_layout's blocked_cells (the cells a spanning card
    covers in its own column). The workflow's own anchor row isn't a span row,
    so deleting that -- which cascades the workflow itself away -- is still
    allowed."""
    track = await db.get(Track, track_id)
    if not track:
        raise HTTPException(404, "Track not found")
    ordered = await ordered_tracks(db, *scope_of(track))
    pos = {t.id: i for i, t in enumerate(ordered)}
    row = pos.get(track_id)
    if row is not None:
        layout = await compute_layout(db, track.project_id)
        if any(r == row for r, _col in layout["blocked_cells"]):
            raise HTTPException(
                409,
                "This row is part of a multi-input workflow's span -- move or remove that workflow first, then delete the row.",
            )
    await unlink_track(db, track)
    await db.delete(track)
    await db.commit()
