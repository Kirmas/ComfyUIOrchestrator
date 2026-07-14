import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import get_db
from app.db.models import Capability
from app.schemas.schemas import CapabilityCreate, CapabilityRead, CapabilityUpdate

router = APIRouter(prefix="/api/capabilities", tags=["capabilities"])


@router.get("", response_model=list[CapabilityRead])
async def list_capabilities(
    node_type_slug: str | None = None, backend_id: uuid.UUID | None = None, db: AsyncSession = Depends(get_db)
):
    stmt = select(Capability)
    if node_type_slug:
        stmt = stmt.where(Capability.node_type_slug == node_type_slug)
    if backend_id:
        stmt = stmt.where(Capability.backend_id == backend_id)
    result = await db.execute(stmt.order_by(Capability.created_at))
    return result.scalars().all()


@router.post("", response_model=CapabilityRead, status_code=201)
async def create_capability(payload: CapabilityCreate, db: AsyncSession = Depends(get_db)):
    capability = Capability(**payload.model_dump())
    db.add(capability)
    await db.commit()
    await db.refresh(capability)
    return capability


@router.get("/{capability_id}", response_model=CapabilityRead)
async def get_capability(capability_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    capability = await db.get(Capability, capability_id)
    if not capability:
        raise HTTPException(404, "Capability not found")
    return capability


@router.patch("/{capability_id}", response_model=CapabilityRead)
async def update_capability(capability_id: uuid.UUID, payload: CapabilityUpdate, db: AsyncSession = Depends(get_db)):
    capability = await db.get(Capability, capability_id)
    if not capability:
        raise HTTPException(404, "Capability not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(capability, field, value)
    await db.commit()
    await db.refresh(capability)
    return capability


@router.delete("/{capability_id}", status_code=204)
async def delete_capability(capability_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    capability = await db.get(Capability, capability_id)
    if not capability:
        raise HTTPException(404, "Capability not found")
    await db.delete(capability)
    await db.commit()
