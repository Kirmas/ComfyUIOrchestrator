"""Project Storage -- a place to keep .blend files so they are not lost.

Deliberately NOT the board and NOT the grid: a .blend is neither a reference
picture nor a cell's output, it is somebody's work in progress that has to
survive. It is an ordinary project library asset (`Asset.project_id`, no node,
so it is under backup and storage GC like any asset) whose mime is BLEND_MIME;
the board's library list filters those out (routes/boards.py). Rename, delete
and download are the generic asset routes. Opening one as a live scene is
`POST /api/scenes` with that asset id (core/blender_scene.py copies it; the
stored file is never modified).
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes.assets import to_asset_read, upload_name
from app.core.blend_file import BLEND_MIME, is_blend_upload
from app.core.storage import get_storage
from app.db.base import get_db
from app.db.models import Asset, AssetKind, Project
from app.schemas.schemas import AssetRead

router = APIRouter(prefix="/api/projects", tags=["storage"])


@router.get("/{project_id}/storage", response_model=list[AssetRead])
async def list_storage(project_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Asset).where(Asset.project_id == project_id, Asset.mime_type == BLEND_MIME).order_by(Asset.created_at.desc())
    )
    return [to_asset_read(a) for a in result.scalars().all()]


@router.post("/{project_id}/storage", response_model=AssetRead, status_code=201)
async def upload_to_storage(project_id: uuid.UUID, file: UploadFile, db: AsyncSession = Depends(get_db)):
    if not await db.get(Project, project_id):
        raise HTTPException(404, "Project not found")
    data = await file.read()
    if not is_blend_upload(data, file.filename):
        raise HTTPException(415, "Storage only takes Blender (.blend) files for now")
    key = await get_storage().put_object(data, BLEND_MIME, prefix=f"projects/{project_id}", kind=AssetKind.other)
    asset = Asset(project_id=project_id, storage_key=key, mime_type=BLEND_MIME, kind=AssetKind.other, selected=False,
                  tags=[], meta={"filename": file.filename, "source": "storage", "size": len(data)}, name=upload_name(file.filename))
    db.add(asset)
    await db.commit()
    await db.refresh(asset)
    return to_asset_read(asset)
