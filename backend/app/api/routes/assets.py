import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import asset_response
from app.core.storage import build_asset_url, build_preview_url, get_storage
from app.db.base import get_db
from app.db.models import Asset, AssetKind, Node, NodeKind, NodeStatus
from app.schemas.schemas import AssetMoveUpdate, AssetRead, AssetSelectUpdate

router = APIRouter(prefix="/api/assets", tags=["assets"])


def to_asset_read(asset: Asset) -> AssetRead:
    """The one place an Asset becomes an AssetRead. Every route that returns one
    goes through here -- the url/preview_url/dimension fields were previously
    re-derived at eight call sites, which is exactly how `list_node_outputs` and
    `isPickable` drifted apart over the asset kinds (see core/asset_types.py)."""
    item = AssetRead.model_validate(asset)
    item.url = build_asset_url(asset.id)
    item.preview_url = build_preview_url(asset.id)
    # Dimensions come off the prefix block rather than out of the loaded <img>:
    # naturalWidth stops being the original's size the moment that <img> points
    # at a 384x384 preview.
    size = get_storage().dimensions(asset.storage_key)
    if size:
        item.width, item.height = size
    return item


@router.post("/upload", response_model=AssetRead, status_code=201)
async def upload_asset(file: UploadFile, db: AsyncSession = Depends(get_db)):
    data = await file.read()
    mime_type = file.content_type or "application/octet-stream"
    kind = AssetKind.for_mime(mime_type)
    storage = get_storage()
    key = await storage.put_object(data, mime_type, prefix="uploads", kind=kind)
    asset = Asset(node_id=None, storage_key=key, mime_type=mime_type, kind=kind, selected=False, meta={})
    db.add(asset)
    await db.commit()
    await db.refresh(asset)
    return to_asset_read(asset)


@router.get("/{asset_id}", response_model=AssetRead)
async def get_asset(asset_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    asset = await db.get(Asset, asset_id)
    if not asset:
        raise HTTPException(404, "Asset not found")
    return to_asset_read(asset)


@router.get("/{asset_id}/file")
async def get_asset_file(request: Request, asset_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Serves the raw bytes -- this is what <img src>/<model-viewer src> point at.
    Auth is the normal shared-token check, just via ?token= since browsers don't
    attach a custom Authorization header for these requests (see app/core/auth.py,
    which already falls back to the query param when there's no header).

    Streams from `payload_offset` onward: the file on disk carries a 64 KiB
    prefix block, and nobody outside core/storage.py should ever see it. See
    core/asset_response.py for why the caching had to be re-implemented rather
    than inherited from FileResponse."""
    asset = await db.get(Asset, asset_id)
    if not asset:
        raise HTTPException(404, "Asset not found")
    storage = get_storage()
    path = storage.path_of(asset.storage_key)
    if not path.is_file():
        raise HTTPException(404, "Asset file missing")
    return asset_response.stream_payload(
        request,
        path,
        storage.payload_offset(asset.storage_key),
        asset.mime_type,
        asset_response.payload_etag(asset.storage_key),
    )


@router.get("/{asset_id}/preview")
async def get_asset_preview(request: Request, asset_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """The 384x384 WebP out of the asset's prefix block -- what the grid's cells
    and the reference picker point at instead of a 40 MB, 33-megapixel original
    that they render at 118 CSS px.

    Files written before this shipped get their prefix built here, on first
    read, and the request waits for it. That costs roughly two seconds the one
    time each file is touched; afterwards this is a ~16 KB read. The decode runs
    in a thread pool (three at a time), so it never blocks the event loop and
    other API calls don't queue behind it.

    Anything with no preview -- a picture already smaller than one, a mesh, a
    file we can't decode -- falls back to the original, so an <img> pointed here
    always gets something renderable."""
    asset = await db.get(Asset, asset_id)
    if not asset:
        raise HTTPException(404, "Asset not found")
    storage = get_storage()
    path = storage.path_of(asset.storage_key)
    if not path.is_file():
        raise HTTPException(404, "Asset file missing")

    await storage.ensure_prefix(asset.storage_key, asset.kind)
    preview = storage.read_preview(asset.storage_key)
    if preview is None:
        return asset_response.stream_payload(
            request,
            path,
            storage.payload_offset(asset.storage_key),
            asset.mime_type,
            asset_response.payload_etag(asset.storage_key),
        )

    etag = asset_response.bytes_etag(preview)
    headers = {"Cache-Control": asset_response.REVALIDATE_CACHE, "ETag": etag}
    if etag in {tag.strip() for tag in request.headers.get("if-none-match", "").split(",")}:
        return Response(status_code=304, headers=headers)
    return Response(preview, media_type="image/webp", headers=headers)


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
    return to_asset_read(asset)


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
    return to_asset_read(asset)
