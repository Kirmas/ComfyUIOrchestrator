"""Unreal / Blender bridge routes (core/bridge.py has the why).

Two audiences, two credentials:

Plugin-facing, `/api/bridge/device/*` -- the http auth middleware lets these
through and each route checks a *device key* itself (`_device`); the site's
API_TOKEN is never accepted or needed here:
- `POST /device/pair`, `POST /device/pair/{id}/poll`  ask to pair, wait for approval (no key yet)
- `WS   /device/ws`               the live connection
- `GET  /device/me`               this device's name and key expiry
- `GET  /device/targets`          projects + their graphs, for the plugin's picker
- `POST /device/upload`           a file from the app -> project library (+ optional ref cell)
- `GET  /device/assets/{id}/file` download an asset that was sent to this device

Page-facing (API_TOKEN, like the rest of /api): pairing approvals, the device
list (revoke), who is connected, send an asset, plugin downloads.
"""
import asyncio
import io
import json
import logging
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response, UploadFile, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes.assets import get_asset_file, resolve_download_name, to_asset_read, upload_name
from app.api.routes.boards import create_library_asset
from app.core.auth import _extract_token
from app.core.bridge import APPS, DEVICE_KEY_LIFETIME, BridgeError, PairingError, hash_key, hub, new_device_key, pairing
from app.core.glb_patch import add_material_textures
from app.core.grid_scope import scope_start_kind
from app.core.storage import get_storage
from app.db.base import async_session_maker, get_db
from app.core.upload_type import resolve_upload
from app.db.models import Asset, BridgeDevice, Dashboard, Node, NodeKind, NodeStatus, Project, Track
from app.mcp.client import get_client

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/bridge", tags=["bridge"])

# How long POST /send waits for the app's verdict before answering "still
# importing" -- long enough for an ordinary mesh, short enough for a phone.
SEND_WAIT_S = 90.0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime) -> datetime:
    # SQLite (dev) hands timestamps back naive; Postgres aware.
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def _device_for_key(db: AsyncSession, key: str | None) -> tuple[BridgeDevice | None, str]:
    if not key:
        return None, "no device key -- pair this app first"
    device = (await db.execute(select(BridgeDevice).where(BridgeDevice.key_hash == hash_key(key)))).scalar_one_or_none()
    if device is None:
        return None, "unknown device key (revoked?) -- pair this app again"
    if _aware(device.expires_at) <= _utcnow():
        return None, "device key expired -- pair this app again"
    return device, ""


async def _device(request: Request, db: AsyncSession = Depends(get_db)) -> BridgeDevice:
    device, why = await _device_for_key(db, _extract_token(request.headers.get("authorization"), None))
    if device is None:
        raise HTTPException(401, why)
    return device


def _device_public(d: BridgeDevice) -> dict:
    return {"id": str(d.id), "app": d.app, "name": d.name,
            "created_at": d.created_at.isoformat() if d.created_at else None,
            "expires_at": _aware(d.expires_at).isoformat(),
            "last_seen_at": _aware(d.last_seen_at).isoformat() if d.last_seen_at else None,
            "online": bool(hub.of_device(str(d.id)))}


# ---------------------------------------------------------------- pairing ----
class PairAsk(BaseModel):
    app: str
    name: str = Field(min_length=1, max_length=80)


@router.post("/device/pair")
async def ask_to_pair(payload: PairAsk, request: Request):
    """Unauthenticated by necessity -- it is how a plugin gets a key at all --
    but all it can do is put a line on the Settings page for the person to
    approve. The code it returns is shown there too."""
    if payload.app not in APPS:
        raise HTTPException(400, f"app must be one of {APPS}")
    try:
        req, secret = pairing.create(payload.app, payload.name.strip(), request.client.host if request.client else "?")
    except PairingError as exc:
        raise HTTPException(429, str(exc)) from exc
    log.info("bridge: pairing requested by %s '%s' from %s (code %s)", req.app, req.name, req.remote, req.code)
    return {"request_id": req.id, "secret": secret, "code": req.code}


class PairPoll(BaseModel):
    secret: str


@router.post("/device/pair/{request_id}/poll")
async def poll_pairing(request_id: str, payload: PairPoll):
    return pairing.poll(request_id, payload.secret)


@router.get("/pairing")
async def list_pairing_requests():
    return [r.public() for r in pairing.pending()]


@router.post("/pairing/{request_id}/approve")
async def approve_pairing(request_id: str, db: AsyncSession = Depends(get_db)):
    try:
        req = pairing.get_pending(request_id)
    except PairingError as exc:
        raise HTTPException(404, str(exc)) from exc
    key = new_device_key()
    device = BridgeDevice(app=req.app, name=req.name, key_hash=hash_key(key), expires_at=_utcnow() + DEVICE_KEY_LIFETIME)
    db.add(device)
    await db.commit()
    req.status, req.key, req.expires_at = "approved", key, _aware(device.expires_at)
    log.info("bridge: approved %s '%s' (device %s)", req.app, req.name, device.id)
    return _device_public(device)


@router.post("/pairing/{request_id}/deny", status_code=204)
async def deny_pairing(request_id: str):
    try:
        pairing.get_pending(request_id).status = "denied"
    except PairingError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/devices")
async def list_devices(db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(BridgeDevice).order_by(BridgeDevice.created_at.desc()))).scalars().all()
    return [_device_public(d) for d in rows]


@router.delete("/devices/{device_id}", status_code=204)
async def revoke_device(device_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    await db.execute(delete(BridgeDevice).where(BridgeDevice.id == device_id))
    await db.commit()
    await hub.drop_device(str(device_id), "device revoked -- pair this app again")


# ------------------------------------------------------------ connection ----
@router.websocket("/device/ws")
async def bridge_ws(websocket: WebSocket):
    # The http middleware never sees a websocket scope. Accept first, then
    # close with a reason, so the plugin can tell "expired" from "offline".
    await websocket.accept()
    async with async_session_maker() as db:
        device, why = await _device_for_key(db, _extract_token(websocket.headers.get("authorization"), None))
        if device is not None:
            device.last_seen_at = _utcnow()
            await db.commit()
            device_id, app, name = str(device.id), device.app, device.name
    if device is None:
        await websocket.close(code=4401, reason=why)
        return
    client = None
    try:
        hello = json.loads(await websocket.receive_text())
        if hello.get("type") != "hello":
            await websocket.close(code=4400, reason="expected hello")
            return
        client = hub.register(device_id, app, name, str(hello.get("version") or "")[:40], websocket)
        await websocket.send_text(json.dumps({"type": "welcome", "client_id": client.id}))
        while True:
            try:
                msg = json.loads(await websocket.receive_text())
            except ValueError:
                continue
            if msg.get("type") == "ping":
                await websocket.send_text('{"type":"pong"}')
            else:
                hub.handle_message(client, msg)
    except (WebSocketDisconnect, RuntimeError, ValueError):
        pass
    finally:
        if client is not None:
            hub.unregister(client)


@router.get("/device/me")
async def device_me(device: BridgeDevice = Depends(_device)):
    return _device_public(device)


@router.get("/device/assets/{asset_id}/file")
async def device_asset_file(asset_id: uuid.UUID, request: Request, device: BridgeDevice = Depends(_device),
                            db: AsyncSession = Depends(get_db)):
    """Only what was sent to this device -- a device key is not a key to the library."""
    if str(asset_id) not in hub.offered.get(str(device.id), set()):
        raise HTTPException(403, "this asset was not sent to this device")
    return await get_asset_file(request, asset_id, db)


@router.get("/clients")
async def list_clients():
    return [c.public() for c in hub.clients()]


class BridgeSend(BaseModel):
    asset_id: uuid.UUID
    client_id: str


async def _asset_project(db: AsyncSession, asset: Asset) -> Project | None:
    """A library asset names its project directly; a cell's asset through its
    node's track."""
    project_id = asset.project_id
    if project_id is None and asset.node_id is not None:
        node = await db.get(Node, asset.node_id)
        track = await db.get(Track, node.track_id) if node else None
        project_id = track.project_id if track else None
    return await db.get(Project, project_id) if project_id else None


@router.post("/send")
async def send_to_app(payload: BridgeSend, db: AsyncSession = Depends(get_db)):
    client = hub.get(payload.client_id)
    if client is None:
        raise HTTPException(409, "That app is not connected any more -- open it (with the Orchestrator plugin) and try again")
    asset = await db.get(Asset, payload.asset_id)
    if asset is None:
        raise HTTPException(404, "Asset not found")
    storage = get_storage()
    path = storage.path_of(asset.storage_key)
    if not path.is_file():
        raise HTTPException(404, "Asset file missing")
    project = await _asset_project(db, asset)
    filename = await resolve_download_name(db, asset)
    stem, _, ext = filename.rpartition(".")
    item = {
        "id": str(asset.id),
        "name": stem or filename,
        "filename": filename,
        "format": ext.lower() if stem else "",
        "kind": getattr(asset.kind, "value", asset.kind),
        "mime_type": asset.mime_type,
        "size": max(0, path.stat().st_size - storage.payload_offset(asset.storage_key)),
        "url": f"/api/bridge/device/assets/{asset.id}/file",
        "project_id": str(project.id) if project else None,
        "project_name": project.name if project else None,
    }
    try:
        result = await hub.send_import(client, item, SEND_WAIT_S)
    except BridgeError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"client": client.public(), **result}


@router.get("/device/targets")
async def list_targets(_: BridgeDevice = Depends(_device), db: AsyncSession = Depends(get_db)):
    """Every project with its graphs: the main grid (id null) first, then the
    sub-dashboards. An unnamed sub-dashboard is shown by a short id -- there is
    nothing better to call it (a pointer cell has no name of its own)."""
    projects = (await db.execute(select(Project).order_by(Project.name))).scalars().all()
    dashboards = (await db.execute(select(Dashboard))).scalars().all()
    by_project: dict[uuid.UUID, list[Dashboard]] = {}
    for d in dashboards:
        by_project.setdefault(d.project_id, []).append(d)
    out = []
    for p in projects:
        subs = sorted(by_project.get(p.id, []), key=lambda d: ((d.name or "").strip() == "", (d.name or "").lower()))
        graphs = [{"id": None, "name": "Main"}]
        graphs += [{"id": str(d.id), "name": (d.name or "").strip() or f"Sub-dashboard {str(d.id)[:8]}"} for d in subs]
        out.append({"id": str(p.id), "name": p.name, "graphs": graphs})
    return out


async def _new_row_cell(project_id: uuid.UUID, dashboard_id: uuid.UUID | None, db: AsyncSession) -> dict:
    """A new row at the bottom of the graph with an empty `asset.single` cell in its first
    asset column. Through the real routes (in-process), so ordering, column parity and the
    page's live update are theirs, not re-derived here."""
    start = await scope_start_kind(db, project_id, dashboard_id)
    step = 1 if start == NodeKind.workflow else 0
    async with get_client(agent=False) as http:
        r = await http.post("/api/tracks", json={"project_id": str(project_id),
                                                 "dashboard_id": str(dashboard_id) if dashboard_id else None})
        if not r.is_success:
            raise HTTPException(r.status_code, f"creating the row failed: {r.text}")
        track = r.json()
        r = await http.post("/api/nodes", json={"track_id": track["id"], "step_index": step, "kind": "asset",
                                                "node_type": "asset.single"})
        if not r.is_success:
            await http.delete(f"/api/tracks/{track['id']}")
            raise HTTPException(r.status_code, f"placing the cell failed: {r.text}")
        return {"track_id": track["id"], "node_id": r.json()["id"]}


async def _drop_row(track_id: str) -> None:
    async with get_client(agent=False) as http:
        await http.delete(f"/api/tracks/{track_id}")


@router.post("/device/upload", status_code=201)
async def upload_from_app(
    file: UploadFile,
    request: Request,
    project_id: uuid.UUID = Form(...),
    dashboard_id: str = Form(""),
    place: bool = Form(True),
    source_path: str = Form(""),
    groom: bool = Form(False),
    diagnostics: str = Form(""),
    material_fixups: str = Form(""),
    device: BridgeDevice = Depends(_device),
    db: AsyncSession = Depends(get_db),
):
    project = await db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "Project not found")
    dash_id = uuid.UUID(dashboard_id) if dashboard_id.strip() else None
    if dash_id is not None:
        dash = await db.get(Dashboard, dash_id)
        if dash is None or dash.project_id != project_id:
            raise HTTPException(404, "That graph is not in this project")
    meta = {"source": "bridge", "bridge_app": device.app, "bridge_device": device.name}
    if source_path:
        meta["bridge_path"] = source_path[:500]
    if diagnostics:
        # what the plugin saw (e.g. Unreal's materials), so a bad export can be read here, not copied by hand
        meta["bridge_diag"] = diagnostics[:20000]
    if groom:
        meta["groom"] = True  # an Unreal Groom .abc (Groom Exporter add-on), not just a hair cache
    data = await file.read()
    if material_fixups:
        # Textures the exporter couldn't bake (Unreal's glTF exporter + a Substrate material =
        # black), sent as tex_* parts and written into the GLB's materials (core/glb_patch.py).
        form = await request.form()
        images = {k: await v.read() for k, v in form.multi_items() if k.startswith("tex_") and hasattr(v, "read")}
        try:
            data, changed = await asyncio.to_thread(add_material_textures, data, json.loads(material_fixups), images)
        except (ValueError, KeyError) as exc:
            changed = [f"failed: {exc}"]
        meta["bridge_textures"] = changed
    if not place:
        asset = await create_library_asset(db, project_id, data, file.content_type, file.filename, meta)
        placed = None
    else:
        # The cell OWNS the file (an asset.single, like an upload made in the cell itself), not a
        # reference to a library asset: node_id and project_id are alternatives, never both.
        placed = await _new_row_cell(project_id, dash_id, db)
        try:
            mime_type, kind = resolve_upload(data, file.content_type)
            key = await get_storage().put_object(data, mime_type, prefix="uploads", kind=kind)
            node = await db.get(Node, uuid.UUID(placed["node_id"]))
            asset = Asset(node_id=node.id, storage_key=key, mime_type=mime_type, kind=kind, selected=True,
                          meta=meta, name=upload_name(file.filename))
            db.add(asset)
            node.status = NodeStatus.done
            await db.commit()
            await db.refresh(asset)
        except Exception:
            await db.rollback()
            await _drop_row(placed["track_id"])
            raise
    log.info("bridge: %s '%s' uploaded %s into project %s%s", device.app, device.name, asset.id, project_id,
             f" (cell {placed['node_id']})" if placed else "")
    return {"asset": to_asset_read(asset).model_dump(mode="json"), "placed": placed}


PLUGINS_DIR = Path(__file__).resolve().parents[2] / "bridge_plugins"


@router.get("/plugins/{app}.zip")
async def download_plugin(app: str):
    """The plugin itself, zipped on the fly from bridge_plugins/ so the page's
    download is always the deployed version. Blender: files at the zip root
    (an extension, Blender 4.2+: Install from Disk). Unreal: the
    OrchestratorBridge folder, to drop into a project's Plugins/."""
    roots = {"blender": (PLUGINS_DIR / "blender" / "orchestrator_bridge", False),
             "unreal": (PLUGINS_DIR / "unreal" / "OrchestratorBridge", True)}
    if app not in roots:
        raise HTTPException(404, "Unknown plugin")
    root, keep_folder = roots[app]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(root.rglob("*")):
            if path.is_dir() or "__pycache__" in path.parts:
                continue
            rel = path.relative_to(root.parent if keep_folder else root)
            zf.write(path, rel.as_posix())
        if app == "blender":
            # one copy of the groom pre-check for the scenes and the add-on (see its header)
            zf.write(PLUGINS_DIR.parent / "blender_addons" / "groom_precheck.py", "groom_precheck.py")
    name = "orchestrator_bridge_blender.zip" if app == "blender" else "OrchestratorBridge_unreal.zip"
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})
