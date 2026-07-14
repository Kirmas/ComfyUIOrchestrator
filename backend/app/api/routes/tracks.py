import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import get_db
from app.db.models import Node, Track
from app.schemas.schemas import NodeRead, TrackCreate, TrackRead, TrackShift, TrackUpdate

router = APIRouter(prefix="/api/tracks", tags=["tracks"])


@router.post("", response_model=TrackRead, status_code=201)
async def create_track(payload: TrackCreate, db: AsyncSession = Depends(get_db)):
    track = Track(**payload.model_dump())
    db.add(track)
    await db.commit()
    await db.refresh(track)
    return track


@router.get("/{track_id}", response_model=TrackRead)
async def get_track(track_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    track = await db.get(Track, track_id)
    if not track:
        raise HTTPException(404, "Track not found")
    return track


@router.patch("/{track_id}", response_model=TrackRead)
async def update_track(track_id: uuid.UUID, payload: TrackUpdate, db: AsyncSession = Depends(get_db)):
    """row_index doubles as display label and sort key (see Grid.tsx) and,
    via track_below_prev inputs, as an adjacency link between tracks -- the
    frontend always sends a full reindex (0..N-1) across every track it
    moves, not just the one being dragged, so this only ever needs to write
    one track at a time.
    """
    track = await db.get(Track, track_id)
    if not track:
        raise HTTPException(404, "Track not found")
    track.row_index = payload.row_index
    await db.commit()
    await db.refresh(track)
    return track


@router.get("/{track_id}/nodes", response_model=list[NodeRead])
async def list_track_nodes(track_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Node).where(Node.track_id == track_id).order_by(Node.step_index))
    return result.scalars().all()


@router.post("/{track_id}/shift", response_model=list[NodeRead])
async def shift_track(track_id: uuid.UUID, payload: TrackShift, db: AsyncSession = Depends(get_db)):
    """Moves every node in this track left/right by `delta` columns -- a pure
    layout nudge (e.g. lining a spawned branch up under its parent's column).
    self_prev/explicit inputs are relative-order/id based, so a uniform shift
    doesn't disturb them. delta must be even: _kind_for_step (api/routes/
    nodes.py) alternates each step's asset/workflow kind off step_index parity,
    so an odd shift would flip every node's kind out from under its own
    template/params. This does NOT special-case track_below_prev: that input
    type resolves against this track's raw step_index from the *adjacent*
    track (row_index +/-1, see resolve_node_inputs), so shifting a track that
    feeds or is fed by one via track_below_prev can change which asset an
    existing node resolves to. Caller's call whether that matters here.
    """
    if payload.delta == 0 or payload.delta % 2 != 0:
        raise HTTPException(400, "delta must be a non-zero even number")
    track = await db.get(Track, track_id)
    if not track:
        raise HTTPException(404, "Track not found")
    result = await db.execute(select(Node).where(Node.track_id == track_id))
    nodes = list(result.scalars().all())
    if any(n.step_index + payload.delta < 0 for n in nodes):
        raise HTTPException(400, "Shift would move a node to a negative step_index")
    for n in nodes:
        n.step_index += payload.delta
    await db.commit()
    for n in nodes:
        await db.refresh(n)
    return nodes


@router.delete("/{track_id}", status_code=204)
async def delete_track(track_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    track = await db.get(Track, track_id)
    if not track:
        raise HTTPException(404, "Track not found")
    await db.delete(track)
    await db.commit()
