import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import get_db
from app.db.models import Node, Track
from app.schemas.schemas import NodeRead, TrackCreate, TrackRead, TrackUpdate

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


@router.delete("/{track_id}", status_code=204)
async def delete_track(track_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    track = await db.get(Track, track_id)
    if not track:
        raise HTTPException(404, "Track not found")
    await db.delete(track)
    await db.commit()
