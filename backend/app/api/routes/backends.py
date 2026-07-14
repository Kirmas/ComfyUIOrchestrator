import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import get_db
from app.db.models import Backend
from app.schemas.schemas import BackendCreate, BackendRead, BackendUpdate

router = APIRouter(prefix="/api/backends", tags=["backends"])


@router.get("", response_model=list[BackendRead])
async def list_backends(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Backend).order_by(Backend.created_at))
    return result.scalars().all()


@router.post("", response_model=BackendRead, status_code=201)
async def create_backend(payload: BackendCreate, db: AsyncSession = Depends(get_db)):
    backend = Backend(**payload.model_dump())
    db.add(backend)
    await db.commit()
    await db.refresh(backend)
    return backend


@router.get("/{backend_id}", response_model=BackendRead)
async def get_backend(backend_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    backend = await db.get(Backend, backend_id)
    if not backend:
        raise HTTPException(404, "Backend not found")
    return backend


@router.patch("/{backend_id}", response_model=BackendRead)
async def update_backend(backend_id: uuid.UUID, payload: BackendUpdate, db: AsyncSession = Depends(get_db)):
    backend = await db.get(Backend, backend_id)
    if not backend:
        raise HTTPException(404, "Backend not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(backend, field, value)
    await db.commit()
    await db.refresh(backend)
    return backend


@router.delete("/{backend_id}", status_code=204)
async def delete_backend(backend_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    backend = await db.get(Backend, backend_id)
    if not backend:
        raise HTTPException(404, "Backend not found")
    await db.delete(backend)
    await db.commit()
