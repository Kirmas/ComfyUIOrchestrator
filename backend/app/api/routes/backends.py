import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import get_db
from app.db.models import ApiUsageLog, Backend
from app.schemas.schemas import BackendCreate, BackendRead, BackendUpdate

router = APIRouter(prefix="/api/backends", tags=["backends"])

# Rolling window, not a midnight-boundary reset -- matches Backend.daily_limit's
# enforcement in dispatcher._backend_within_quota, and sidesteps timezone /
# day-rollover edge cases a mutable "reset at midnight" counter would need.
_USAGE_WINDOW = timedelta(hours=24)


async def _used_today_by_backend(db: AsyncSession, backend_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    if not backend_ids:
        return {}
    cutoff = datetime.now(UTC) - _USAGE_WINDOW
    stmt = (
        select(ApiUsageLog.backend_id, func.count())
        .where(ApiUsageLog.backend_id.in_(backend_ids), ApiUsageLog.created_at >= cutoff)
        .group_by(ApiUsageLog.backend_id)
    )
    result = await db.execute(stmt)
    return {row[0]: row[1] for row in result.all()}


def _to_read(backend: Backend, used_today: int) -> BackendRead:
    item = BackendRead.model_validate(backend)
    item.has_api_key = bool(backend.api_key)
    item.used_today = used_today
    return item


@router.get("", response_model=list[BackendRead])
async def list_backends(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Backend).order_by(Backend.created_at))
    backends = list(result.scalars().all())
    used = await _used_today_by_backend(db, [b.id for b in backends])
    return [_to_read(b, used.get(b.id, 0)) for b in backends]


@router.post("", response_model=BackendRead, status_code=201)
async def create_backend(payload: BackendCreate, db: AsyncSession = Depends(get_db)):
    backend = Backend(**payload.model_dump())
    db.add(backend)
    await db.commit()
    await db.refresh(backend)
    return _to_read(backend, 0)


@router.get("/{backend_id}", response_model=BackendRead)
async def get_backend(backend_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    backend = await db.get(Backend, backend_id)
    if not backend:
        raise HTTPException(404, "Backend not found")
    used = await _used_today_by_backend(db, [backend.id])
    return _to_read(backend, used.get(backend.id, 0))


@router.patch("/{backend_id}", response_model=BackendRead)
async def update_backend(backend_id: uuid.UUID, payload: BackendUpdate, db: AsyncSession = Depends(get_db)):
    backend = await db.get(Backend, backend_id)
    if not backend:
        raise HTTPException(404, "Backend not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(backend, field, value)
    await db.commit()
    await db.refresh(backend)
    used = await _used_today_by_backend(db, [backend.id])
    return _to_read(backend, used.get(backend.id, 0))


@router.delete("/{backend_id}", status_code=204)
async def delete_backend(backend_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    backend = await db.get(Backend, backend_id)
    if not backend:
        raise HTTPException(404, "Backend not found")
    await db.delete(backend)
    await db.commit()
