"""Live Blender scenes -- REST surface over core/blender_scene.py.

The MCP tools (mcp/tools.py scene_*) and the "Scenes" page both go through
here. A scene's files are not in the database or the media dir: see the
manager's docstring. The one thing that crosses over is export, which makes an
ordinary library Asset (node_id None, like an upload) -- that is the only way
a result leaves a scene, and the asset then lives and dies like any other.
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes.assets import to_asset_read
from app.config import get_settings
from app.core.blender import find_blender_binary
from app.core.blender_scene import SCENE_GUIDE, SceneError, SceneInput, scene_manager
from app.core.storage import get_storage
from app.core.upload_type import resolve_upload
from app.db.base import get_db
from app.db.models import Asset
from app.mcp.client import AGENT_ACTOR_HEADER
from app.schemas.schemas import AssetRead

router = APIRouter(prefix="/api/scenes", tags=["scenes"])


class SceneCreate(BaseModel):
    asset_ids: list[uuid.UUID] = Field(min_length=1, max_length=8)
    name: str | None = None
    owner: str | None = None
    # None = the server's default (simplify inputs denser than ~1M triangles);
    # 0 = never simplify; N = simplify anything over N triangles.
    max_tris: int | None = Field(default=None, ge=0)


class SceneRun(BaseModel):
    code: str
    timeout_s: float = 120


class SceneRender(BaseModel):
    views: list[str | list[float]] = Field(default_factory=lambda: ["front"])
    frames: list[int] | None = None
    engine: str = "workbench"
    size: int = 512


class SceneExport(BaseModel):
    format: str = "glb"
    name: str | None = None


def _raise(exc: SceneError):
    raise HTTPException(exc.status, str(exc)) from exc


def _decorate(summary: dict, request: Request) -> dict:
    """Picture URLs for the Scenes page. `<img>` can't send an Authorization
    header, so (as for assets) the token rides in the query string -- which is
    why an MCP agent's calls don't get them: it has no use for a URL and
    shouldn't have the token in its context. `v` busts the browser cache when
    a render overwrites the file."""
    if request.headers.get(AGENT_ACTOR_HEADER) == "agent" or not summary.get("has_preview"):
        return summary
    base = f"/api/scenes/{summary['id']}"
    tail = f"?token={get_settings().api_token}&v={int(summary['last_activity'] * 1000)}"
    return {**summary, "preview_url": f"{base}/preview{tail}", "render_url": f"{base}/render{tail}"}


@router.get("/guide")
async def scene_guide() -> dict:
    return {"guide": SCENE_GUIDE}


@router.get("")
async def list_scenes(request: Request) -> dict:
    listing = await scene_manager.list_scenes()
    # `available` is how the page decides whether to show the Scenes tab at all:
    # with no Blender on the box there is nothing to list (same rule as the
    # Blender node type, which isn't registered without it).
    return {**listing, "available": find_blender_binary() is not None,
            "scenes": [_decorate(sc, request) for sc in listing["scenes"]]}


@router.post("", status_code=201)
async def create_scene(body: SceneCreate, request: Request, db: AsyncSession = Depends(get_db)) -> dict:
    storage = get_storage()
    inputs: list[SceneInput] = []
    for asset_id in body.asset_ids:
        asset = await db.get(Asset, asset_id)
        if asset is None:
            raise HTTPException(404, f"asset {asset_id} not found")
        try:
            data = storage.get_object(asset.storage_key)
        except OSError:
            raise HTTPException(404, f"asset {asset_id}: its file is missing from storage") from None
        inputs.append(SceneInput(asset_id=str(asset.id), kind=str(getattr(asset.kind, "value", asset.kind)),
                                 mime=asset.mime_type, data=data))
    try:
        return _decorate(await scene_manager.create(inputs, name=body.name, owner=body.owner, max_tris=body.max_tris), request)
    except SceneError as exc:
        _raise(exc)


@router.get("/{scene_id}")
async def get_scene(scene_id: str, request: Request) -> dict:
    try:
        return _decorate(await scene_manager.summary(scene_id), request)
    except SceneError as exc:
        _raise(exc)


@router.get("/{scene_id}/info")
async def scene_info(scene_id: str, request: Request) -> dict:
    try:
        info = await scene_manager.info(scene_id)
        return {**info, "scene": _decorate(info["scene"], request)}
    except SceneError as exc:
        _raise(exc)


@router.post("/{scene_id}/run")
async def run_in_scene(scene_id: str, body: SceneRun) -> dict:
    try:
        return await scene_manager.run(scene_id, body.code, body.timeout_s)
    except SceneError as exc:
        _raise(exc)


@router.post("/{scene_id}/render")
async def render_scene(scene_id: str, body: SceneRender) -> dict:
    try:
        return await scene_manager.render(scene_id, body.views, body.frames, body.engine, body.size)
    except SceneError as exc:
        _raise(exc)


@router.get("/{scene_id}/render")
async def latest_render(scene_id: str) -> FileResponse:
    """The picture the last render call produced (a contact sheet if it was several)."""
    try:
        return FileResponse(scene_manager.render_path(scene_id), media_type="image/png", headers={"Cache-Control": "no-cache"})
    except SceneError as exc:
        _raise(exc)


@router.get("/{scene_id}/preview")
async def scene_preview(scene_id: str) -> FileResponse:
    """Small thumbnail of the latest render, for the Scenes list."""
    try:
        return FileResponse(scene_manager.render_path(scene_id, thumb=True), media_type="image/png",
                            headers={"Cache-Control": "no-cache"})
    except SceneError as exc:
        _raise(exc)


@router.post("/{scene_id}/export", response_model=AssetRead, status_code=201)
async def export_scene(scene_id: str, body: SceneExport, db: AsyncSession = Depends(get_db)):
    try:
        data, mime, ext = await scene_manager.export(scene_id, body.format)
        summary = await scene_manager.summary(scene_id)
    except SceneError as exc:
        _raise(exc)
    mime_type, kind = resolve_upload(data, mime)
    key = await get_storage().put_object(data, mime_type, prefix="uploads", kind=kind)
    filename = f"{(body.name or summary['name'] or 'scene').strip()[:80]}.{ext}"
    asset = Asset(node_id=None, storage_key=key, mime_type=mime_type, kind=kind, selected=False,
                  meta={"filename": filename, "source": "blender_scene", "scene_id": scene_id})
    db.add(asset)
    await db.commit()
    await db.refresh(asset)
    return to_asset_read(asset)


@router.post("/{scene_id}/close")
async def close_scene(scene_id: str, request: Request) -> dict:
    """Stop the Blender process (frees its memory) but keep the checkpoint."""
    try:
        return _decorate(await scene_manager.close(scene_id), request)
    except SceneError as exc:
        _raise(exc)


@router.delete("/{scene_id}", status_code=204)
async def destroy_scene(scene_id: str) -> None:
    try:
        await scene_manager.destroy(scene_id)
    except SceneError as exc:
        _raise(exc)
