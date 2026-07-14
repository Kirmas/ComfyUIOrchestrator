import uuid

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.queue import job_queue
from app.core.storage import build_asset_url, get_storage
from app.db.base import get_db
from app.db.models import Asset, AssetKind, Job, Node, NodeKind, NodeStatus, Project, Track
from app.schemas.schemas import AssetRead, JobRead, NodeCreate, NodeRead, NodeUpdate
from app.worker.tasks import enqueue_node_job, find_output_asset_node

router = APIRouter(prefix="/api/nodes", tags=["nodes"])


def _opposite_kind(kind: NodeKind) -> NodeKind:
    return NodeKind.asset if kind == NodeKind.workflow else NodeKind.workflow


def _kind_for_step(start_kind: NodeKind, step_index: int) -> NodeKind:
    return start_kind if step_index % 2 == 0 else _opposite_kind(start_kind)


@router.post("", response_model=NodeRead, status_code=201)
async def create_node(payload: NodeCreate, db: AsyncSession = Depends(get_db)):
    """Column kind (asset/workflow) is a project-wide pattern, not a free choice
    per node: only the very first node ever created in a project picks it (via
    payload.kind); every node after that gets its kind computed from its
    step_index's position in the alternating pattern, regardless of what the
    client sent."""
    track = await db.get(Track, payload.track_id)
    if not track:
        raise HTTPException(404, "Track not found")
    project = await db.get(Project, track.project_id)

    if project.start_kind is None:
        project.start_kind = payload.kind if payload.step_index % 2 == 0 else _opposite_kind(payload.kind)
    resolved_kind = _kind_for_step(project.start_kind, payload.step_index)

    data = payload.model_dump(mode="json")
    data["kind"] = resolved_kind
    node = Node(**data)
    db.add(node)
    await db.commit()
    await db.refresh(node)
    return node


@router.get("/{node_id}", response_model=NodeRead)
async def get_node(node_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    node = await db.get(Node, node_id)
    if not node:
        raise HTTPException(404, "Node not found")
    return node


@router.patch("/{node_id}", response_model=NodeRead)
async def update_node(node_id: uuid.UUID, payload: NodeUpdate, db: AsyncSession = Depends(get_db)):
    node = await db.get(Node, node_id)
    if not node:
        raise HTTPException(404, "Node not found")
    data = payload.model_dump(mode="json", exclude_unset=True)
    for field, value in data.items():
        setattr(node, field, value)
    await db.commit()
    await db.refresh(node)
    return node


@router.delete("/{node_id}", status_code=204)
async def delete_node(node_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Deletes this cell and everything after it in the same track (they
    depend on it through the linear self_prev chain, so leaving them behind
    would just dangle). If a sibling track was spawned from the same cause as
    this one (Grid.tsx's onSelectCandidate -- picking a different candidate
    from the same generation), one of those siblings moves into the freed-up
    slot instead of leaving a gap: its nodes already sit at matching
    step_index values (spawned tracks are laid out to align), so it's a
    straight re-parent."""
    node = await db.get(Node, node_id)
    if not node:
        raise HTTPException(404, "Node not found")

    track_id = node.track_id
    step_index = node.step_index

    result = await db.execute(
        select(Node).where(Node.track_id == track_id, Node.step_index == step_index - 1, Node.kind == NodeKind.workflow)
    )
    preceding = result.scalars().first()

    sibling_track = None
    if preceding is not None:
        result = await db.execute(select(Track).where(Track.spawned_from_node_id == preceding.id, Track.id != track_id))
        sibling_track = result.scalars().first()

    result = await db.execute(select(Node).where(Node.track_id == track_id, Node.step_index >= step_index))
    nodes_to_delete = result.scalars().all()
    node_ids = [n.id for n in nodes_to_delete]

    if node_ids:
        result = await db.execute(select(Asset).where(Asset.node_id.in_(node_ids)))
        storage = get_storage()
        for asset in result.scalars().all():
            storage.delete_object(asset.storage_key)

    for n in nodes_to_delete:
        await db.delete(n)

    if sibling_track is not None:
        result = await db.execute(select(Node).where(Node.track_id == sibling_track.id))
        for n in result.scalars().all():
            n.track_id = track_id
        await db.delete(sibling_track)

    await db.commit()


@router.get("/{node_id}/outputs", response_model=list[AssetRead])
async def list_node_outputs(node_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Asset).where(Asset.node_id == node_id).order_by(Asset.created_at))
    assets = result.scalars().all()
    out = []
    for asset in assets:
        item = AssetRead.model_validate(asset)
        item.url = build_asset_url(asset.id)
        out.append(item)
    return out


@router.get("/{node_id}/jobs", response_model=list[JobRead])
async def list_node_jobs(node_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Job).where(Job.node_id == node_id).order_by(Job.variant_index))
    return result.scalars().all()


@router.post("/{node_id}/upload-asset", response_model=AssetRead, status_code=201)
async def upload_asset_to_node(node_id: uuid.UUID, file: UploadFile, db: AsyncSession = Depends(get_db)):
    """Manual fill for an asset-kind node (SPEC section 5's start cell) -- no
    workflow/backend involved. Can be called repeatedly to add more lines to
    the same asset cell."""
    node = await db.get(Node, node_id)
    if not node:
        raise HTTPException(404, "Node not found")
    if node.kind != NodeKind.asset:
        raise HTTPException(400, "Only asset-kind nodes accept manual uploads")

    data = await file.read()
    mime_type = file.content_type or "application/octet-stream"
    kind = AssetKind.image if mime_type.startswith("image/") else AssetKind.other
    storage = get_storage()
    key = storage.put_object(data, mime_type, prefix=f"nodes/{node.id}")
    asset = Asset(node_id=node.id, storage_key=key, mime_type=mime_type, kind=kind, selected=True, meta={})
    db.add(asset)
    node.status = NodeStatus.done
    node.error = None
    await db.commit()
    await db.refresh(asset)
    item = AssetRead.model_validate(asset)
    item.url = build_asset_url(asset.id)
    return item


@router.post("/{node_id}/generate", response_model=NodeRead)
async def generate_node(node_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    node = await db.get(Node, node_id)
    if not node:
        raise HTTPException(404, "Node not found")
    if node.kind != NodeKind.workflow:
        raise HTTPException(400, "Only workflow-kind nodes can be generated -- asset nodes are filled by upload or by their source workflow")
    if node.status in (NodeStatus.queued, NodeStatus.running):
        raise HTTPException(409, "Node is already generating")
    await job_queue.enqueue(enqueue_node_job, str(node.id))
    node.status = NodeStatus.queued
    await db.commit()
    await db.refresh(node)
    return node


@router.post("/{node_id}/discard", response_model=NodeRead)
async def discard_node(node_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    node = await db.get(Node, node_id)
    if not node:
        raise HTTPException(404, "Node not found")
    node.status = NodeStatus.discarded
    await db.commit()
    await db.refresh(node)
    return node


@router.post("/{node_id}/reroll", response_model=NodeRead, status_code=201)
async def reroll_node(node_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """SPEC section 4: none of the N variants were good -> the old step is marked
    discarded (kept for history) and a new step with the same inputs is created
    and immediately queued for a fresh generation."""
    old = await db.get(Node, node_id)
    if not old:
        raise HTTPException(404, "Node not found")
    if old.kind != NodeKind.workflow:
        raise HTTPException(400, "Only workflow-kind nodes can be re-rolled -- re-upload to an asset node instead")
    old.status = NodeStatus.discarded

    # Discard the old paired output asset-node too, so the re-rolled run gets a
    # fresh one instead of appending its results next to the discarded outputs.
    old_asset_node = await find_output_asset_node(db, old.track_id, old.step_index)
    if old_asset_node is not None:
        old_asset_node.status = NodeStatus.discarded

    new_node = Node(
        track_id=old.track_id,
        step_index=old.step_index,
        kind=NodeKind.workflow,
        template_id=old.template_id,
        inputs=old.inputs,
        params=old.params,
        requested_variants=old.requested_variants,
        backend_mode=old.backend_mode,
        manual_backend_id=old.manual_backend_id,
        status=NodeStatus.queued,
    )
    db.add(new_node)
    await db.commit()
    await db.refresh(new_node)

    await job_queue.enqueue(enqueue_node_job, str(new_node.id))
    return new_node
