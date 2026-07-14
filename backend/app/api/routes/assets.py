import uuid

from fastapi import APIRouter, Depends, HTTPException, Response, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.storage import build_asset_url, get_storage
from app.db.base import get_db
from app.db.models import Asset, AssetKind, Node, NodeKind, NodeStatus
from app.schemas.schemas import AssetMoveUpdate, AssetRead, AssetSelectUpdate

router = APIRouter(prefix="/api/assets", tags=["assets"])


def _to_read(asset: Asset) -> AssetRead:
    item = AssetRead.model_validate(asset)
    item.url = build_asset_url(asset.id)
    return item


@router.post("/upload", response_model=AssetRead, status_code=201)
async def upload_asset(file: UploadFile, db: AsyncSession = Depends(get_db)):
    data = await file.read()
    mime_type = file.content_type or "application/octet-stream"
    kind = AssetKind.image if mime_type.startswith("image/") else AssetKind.other
    storage = get_storage()
    key = storage.put_object(data, mime_type, prefix="uploads")
    asset = Asset(node_id=None, storage_key=key, mime_type=mime_type, kind=kind, selected=False, meta={})
    db.add(asset)
    await db.commit()
    await db.refresh(asset)
    return _to_read(asset)


@router.get("/{asset_id}", response_model=AssetRead)
async def get_asset(asset_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    asset = await db.get(Asset, asset_id)
    if not asset:
        raise HTTPException(404, "Asset not found")
    return _to_read(asset)


@router.get("/{asset_id}/file")
async def get_asset_file(asset_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Serves the raw bytes -- this is what <img src>/<model-viewer src> point at.
    Auth is the normal shared-token check, just via ?token= since browsers don't
    attach a custom Authorization header for these requests (see app/core/auth.py,
    which already falls back to the query param when there's no header)."""
    asset = await db.get(Asset, asset_id)
    if not asset:
        raise HTTPException(404, "Asset not found")
    storage = get_storage()
    data = storage.get_object(asset.storage_key)
    return Response(content=data, media_type=asset.mime_type)


@router.delete("/{asset_id}", status_code=204)
async def delete_asset(asset_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Per-image reject in a multi-variant candidates cell (NodeCell.tsx forces
    the user to either spawn or discard each candidate) -- distinct from
    discard_node, which discards a whole cell rather than one image in it."""
    asset = await db.get(Asset, asset_id)
    if not asset:
        raise HTTPException(404, "Asset not found")
    storage = get_storage()
    storage.delete_object(asset.storage_key)
    await db.delete(asset)
    await db.commit()


@router.patch("/{asset_id}/select", response_model=AssetRead)
async def select_asset(asset_id: uuid.UUID, payload: AssetSelectUpdate, db: AsyncSession = Depends(get_db)):
    asset = await db.get(Asset, asset_id)
    if not asset:
        raise HTTPException(404, "Asset not found")
    asset.selected = payload.selected
    await db.commit()
    await db.refresh(asset)
    return _to_read(asset)


@router.post("/{asset_id}/move", response_model=AssetRead)
async def move_asset(asset_id: uuid.UUID, payload: AssetMoveUpdate, db: AsyncSession = Depends(get_db)):
    """Re-parents a generated variant onto its own dedicated asset-kind node --
    used when the user picks one output to branch into a new track (Grid.tsx's
    onSpawnTrack): the picked image should look exactly like a manually
    uploaded asset cell (one image, done), and disappear from the original
    multi-variant node's candidates grid rather than just being flagged."""
    asset = await db.get(Asset, asset_id)
    if not asset:
        raise HTTPException(404, "Asset not found")
    target = await db.get(Node, payload.node_id)
    if not target:
        raise HTTPException(404, "Target node not found")
    if target.kind != NodeKind.asset:
        raise HTTPException(400, "Can only move an asset onto an asset-kind node")

    asset.node_id = target.id
    asset.selected = True
    target.status = NodeStatus.done
    target.error = None
    await db.commit()
    await db.refresh(asset)
    return _to_read(asset)
