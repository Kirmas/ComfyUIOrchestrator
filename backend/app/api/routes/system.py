import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core import storage_gc, storage_migration
from app.core.storage import build_asset_url
from app.db.base import get_db
from app.db.models import Asset, Project
from app.schemas.schemas import AssetRead

router = APIRouter(prefix="/api/system", tags=["system"])


@router.get("/storage")
async def get_storage_info(db: AsyncSession = Depends(get_db)):
    settings = get_settings()
    stats = storage_migration.media_dir_stats()
    asset_count = (await db.execute(select(func.count()).select_from(Asset))).scalar_one()
    try:
        disk = storage_migration.disk_report(settings.media_dir)
    except OSError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {
        "media_dir": settings.media_dir,
        "disk": disk,
        "size_bytes": stats["size_bytes"],
        "file_count": stats["file_count"],
        "asset_count": asset_count,
    }


@router.get("/storage/browse")
async def browse(path: str | None = None):
    try:
        return storage_migration.list_dirs(path)
    except (NotADirectoryError, FileNotFoundError, PermissionError) as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/storage/browse/mkdir")
async def mkdir(body: dict):
    path = body.get("path")
    if not path:
        raise HTTPException(400, "path is required")
    try:
        return storage_migration.make_dir(path)
    except (ValueError, OSError) as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/storage/migrate")
async def migrate(body: dict):
    new_path = body.get("new_path")
    if not new_path:
        raise HTTPException(400, "new_path is required")
    try:
        storage_migration.start_migration(new_path)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    except (ValueError, OSError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return storage_migration.get_status()


@router.get("/storage/migrate/status")
async def migrate_status():
    return storage_migration.get_status()


@router.get("/storage/orphans")
async def get_orphans(db: AsyncSession = Depends(get_db)):
    return await storage_gc.scan_orphans(db)


@router.get("/storage/orphans/preview")
async def preview_orphan(path: str):
    """Raw bytes for an orphan file that has no Asset row (and so no
    /api/assets/{id}/file URL) -- used as an <img src> in the orphan list,
    same ?token= auth fallback as that route (core/auth.py)."""
    try:
        file_path = storage_gc.preview_path(path)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not file_path.is_file():
        raise HTTPException(404, "File not found")
    # storage_key files have no extension (storage.py::put_object), so the
    # Content-Type Starlette would otherwise guess from the filename is
    # always wrong -- pass the same header-sniffed mime type the scan result
    # already reported.
    return FileResponse(file_path, media_type=storage_gc.guess_mime_type(file_path))


@router.post("/storage/orphans/delete", status_code=204)
async def delete_orphan(body: dict, db: AsyncSession = Depends(get_db)):
    path = body.get("path")
    if not path:
        raise HTTPException(400, "path is required")
    try:
        await storage_gc.delete_orphan(db, path)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/storage/orphans/adopt", response_model=AssetRead)
async def adopt_orphan(body: dict, db: AsyncSession = Depends(get_db)):
    path = body.get("path")
    project_id = body.get("project_id")
    if not path or not project_id:
        raise HTTPException(400, "path and project_id are required")
    project = await db.get(Project, uuid.UUID(project_id))
    if not project:
        raise HTTPException(404, "Project not found")
    try:
        asset = await storage_gc.adopt_orphan(db, path, project.id)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    read = AssetRead.model_validate(asset)
    read.url = build_asset_url(asset.id)
    return read


@router.get("/unowned-assets")
async def get_unowned_assets(db: AsyncSession = Depends(get_db)):
    return await storage_gc.scan_unowned_assets(db)


@router.post("/unowned-assets/delete", status_code=204)
async def delete_unowned_asset(body: dict, db: AsyncSession = Depends(get_db)):
    asset_id = body.get("asset_id")
    if not asset_id:
        raise HTTPException(400, "asset_id is required")
    try:
        await storage_gc.delete_unowned_asset(db, uuid.UUID(asset_id))
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/unowned-assets/adopt", response_model=AssetRead)
async def adopt_unowned_asset(body: dict, db: AsyncSession = Depends(get_db)):
    asset_id = body.get("asset_id")
    project_id = body.get("project_id")
    if not asset_id or not project_id:
        raise HTTPException(400, "asset_id and project_id are required")
    project = await db.get(Project, uuid.UUID(project_id))
    if not project:
        raise HTTPException(404, "Project not found")
    try:
        asset = await storage_gc.adopt_unowned_asset(db, uuid.UUID(asset_id), project.id)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    read = AssetRead.model_validate(asset)
    read.url = build_asset_url(asset.id)
    return read
