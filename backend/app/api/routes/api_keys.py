import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import get_db
from app.db.models import ApiKeyPermission
from app.schemas.schemas import ApiKeyPermissionCreate, ApiKeyPermissionRead

router = APIRouter(prefix="/api/api-keys", tags=["api_keys"])


@router.get("", response_model=list[ApiKeyPermissionRead])
async def list_api_keys(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(ApiKeyPermission).order_by(ApiKeyPermission.created_at))
    return result.scalars().all()


@router.post("", response_model=ApiKeyPermissionRead, status_code=201)
async def create_api_key(payload: ApiKeyPermissionCreate, db: AsyncSession = Depends(get_db)):
    entry = ApiKeyPermission(**payload.model_dump())
    db.add(entry)
    await db.commit()
    await db.refresh(entry)
    return entry


@router.delete("/{entry_id}", status_code=204)
async def delete_api_key(entry_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    entry = await db.get(ApiKeyPermission, entry_id)
    if not entry:
        raise HTTPException(404, "Entry not found")
    await db.delete(entry)
    await db.commit()
