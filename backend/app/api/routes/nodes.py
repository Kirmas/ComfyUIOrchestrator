import uuid

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.node_types import resolve_effective_template, sync_legacy_fields
from app.core.queue import job_queue
from app.core.storage import build_asset_url, get_storage
from app.db.base import get_db
from app.db.models import Asset, AssetKind, Job, Node, NodeKind, NodeStatus, Project, Track
from app.schemas.schemas import AssetRead, JobRead, NodeCreate, NodeRead, NodeUpdate
from app.worker.tasks import enqueue_node_job, has_room_for_output, own_output_nodes, selected_or_latest_output

router = APIRouter(prefix="/api/nodes", tags=["nodes"])


def _opposite_kind(kind: NodeKind) -> NodeKind:
    return NodeKind.asset if kind == NodeKind.workflow else NodeKind.workflow


def _kind_for_step(start_kind: NodeKind, step_index: int) -> NodeKind:
    return start_kind if step_index % 2 == 0 else _opposite_kind(start_kind)


async def _ensure_slot_free(db: AsyncSession, track_id, step_index: int, exclude_node_id=None) -> None:
    """Guards the (track_id, step_index) uniqueness invariant everything that
    reads a track's step sequence relies on (self_prev's "nearest asset
    before step_index" scan, cell_index's position lookup,
    _claim_new_output_cell's empty-cell search, ...) -- a second live node claiming a slot
    already taken produces two nodes with no way to tell apart (2026-07-17
    incident, first found via CREATE). update_node needs this same guard:
    a node's row/column now only ever changes by reassigning track_id/
    step_index (there's no cosmetic-only position anymore -- see
    Grid.tsx's dropAssetAt/applyRowMove), so the identical collision is
    just as reachable through PATCH. A discarded node doesn't count --
    that's the re-roll pattern (reroll_node below), a fresh node
    deliberately taking its place."""
    stmt = select(Node).where(
        Node.track_id == track_id,
        Node.step_index == step_index,
        Node.status != NodeStatus.discarded,
    )
    if exclude_node_id is not None:
        stmt = stmt.where(Node.id != exclude_node_id)
    existing = await db.execute(stmt)
    if existing.scalars().first() is not None:
        raise HTTPException(409, "A node already exists at this track/step")


async def _ensure_output_binding(db: AsyncSession, node: Node, target_track_id, target_step: int) -> None:
    """A node with Node.created_by_node_id set (a workflow's own materialized
    output, see worker/tasks.py's _get_or_create_output_asset_node -- the
    only writer of that column, and it's never touched again after) stays
    rigidly bound to its creator: reachable only at the creator's own output
    step (creator.step_index + 1), in a row that's either within the
    creator's own row-span (its home row through home row + however many
    image/file input slots its template declares, the same span Grid.tsx
    grows real tracks for -- see rowSpanByNode/spanOf) or a track spawned
    from it (Track.spawned_from_node_id == creator.id, the set
    onSelectCandidate moves a settled/leftover candidate into once there are
    more results than the creator's own span has room for). A node with no
    creator (manual upload, "+ asset", RefAsset) has nothing to check here.
    Mirrors Grid.tsx's isPositionAllowedFor so a rejected drag and this
    guard agree on what's allowed; this is the enforced copy, not just an
    assist for the UI to grey cells out."""
    if node.created_by_node_id is None:
        return
    creator = await db.get(Node, node.created_by_node_id)
    if creator is None:
        return
    denied = HTTPException(409, "This asset is a workflow output and can only move among its own creator's positions")
    if target_step != creator.step_index + 1:
        raise denied
    target_track = await db.get(Track, target_track_id)
    if target_track is None:
        raise denied
    if target_track.spawned_from_node_id == creator.id:
        return

    creator_track = await db.get(Track, creator.track_id)
    if creator_track is None:
        raise denied
    effective = await resolve_effective_template(db, creator)
    fields = (effective.param_schema if effective else {}).get("fields", [])
    slot_count = len([f for f in fields if f.get("type") in ("image", "file")])
    span = max(slot_count, 1)
    if not (creator_track.row_index <= target_track.row_index < creator_track.row_index + span):
        raise denied


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

    await _ensure_slot_free(db, payload.track_id, payload.step_index)

    if project.start_kind is None:
        project.start_kind = payload.kind if payload.step_index % 2 == 0 else _opposite_kind(payload.kind)
    resolved_kind = _kind_for_step(project.start_kind, payload.step_index)

    data = payload.model_dump(mode="json")
    data["kind"] = resolved_kind
    node = Node(**data)
    if node.created_by_node_id is not None:
        await _ensure_output_binding(db, node, payload.track_id, payload.step_index)
    if node.node_type:
        effective = await resolve_effective_template(db, node)
        sync_legacy_fields(node, effective)
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

    if payload.track_id is not None or payload.step_index is not None:
        target_track_id = payload.track_id if payload.track_id is not None else node.track_id
        target_step = payload.step_index if payload.step_index is not None else node.step_index
        await _ensure_slot_free(db, target_track_id, target_step, exclude_node_id=node.id)
        await _ensure_output_binding(db, node, target_track_id, target_step)

    data = payload.model_dump(mode="json", exclude_unset=True)
    for field, value in data.items():
        setattr(node, field, value)
    if "node_type" in data:
        effective = await resolve_effective_template(db, node)
        sync_legacy_fields(node, effective)
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
    straight re-parent.

    For a workflow node, also sweeps every one of its own outputs
    (created_by_node_id, see worker/tasks.py's own_output_nodes) even if
    some of them live in a DIFFERENT track -- the same-track step_index
    sweep above only catches an output that's still sitting right where it
    was first materialized; _claim_new_output_cell can grow one into another
    track entirely when that home cell was already taken by something else,
    and a workflow's delete has to reach those too rather than leaving them
    behind as orphans no longer pointing at anything live."""
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
    nodes_to_delete = list(result.scalars().all())

    if node.kind == NodeKind.workflow:
        already = {n.id for n in nodes_to_delete}
        for out in await own_output_nodes(db, node.id):
            if out.id not in already:
                nodes_to_delete.append(out)

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
    # asset.refasset owns no Asset row of its own -- see selected_or_latest_output's
    # docstring -- so its "outputs" are whatever real asset its explicit ref points at.
    node = await db.get(Node, node_id)
    if node is not None and node.node_type == "asset.refasset":
        pointed = await selected_or_latest_output(db, node)
        assets = [pointed] if pointed else []
    else:
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

    # Checked here, before ever touching a backend -- has_room_for_output
    # mirrors the same search/insert-or-raise _get_or_create_output_asset_node
    # runs once a job finishes (worker/tasks.py's _locate_output_row), so a
    # generation that would end up unable to place its output (because doing
    # so would split some OTHER node's own row-span apart) gets rejected
    # up front instead of wasting a real backend generation on it. Skipped
    # only if there's no template to resolve is_native from -- that already
    # fails generation its own way below.
    effective = await resolve_effective_template(db, node)
    if effective is not None and not await has_room_for_output(db, node, effective.is_native):
        raise HTTPException(
            409,
            "This node's output cell is blocked in a way that can't be resolved automatically -- inserting a row here would "
            "split another node's own row-span apart. Move things around manually first.",
        )

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

    # Discard every output the old node ever produced too (own_output_nodes,
    # not just whatever's positionally at its old cell -- see
    # worker/tasks.py's own_output_nodes/_get_or_create_output_asset_node,
    # a workflow can accumulate more than one over several regenerate
    # cycles), so the re-rolled run gets an entirely fresh start instead of
    # appending its results next to any of them. new_node below gets a
    # brand-new id, so own_output_nodes(new_node.id) is empty regardless --
    # nothing further to do to make that true.
    for old_output in await own_output_nodes(db, old.id):
        old_output.status = NodeStatus.discarded

    # Checked here, before creating anything -- same reasoning and same
    # failure mode as generate_node's own pre-check (see its comment,
    # 2026-07-20 incident): old's own prior outputs were just discarded
    # above, so they don't count against this.
    effective = await resolve_effective_template(db, old)
    if effective is not None and not await has_room_for_output(db, old, effective.is_native):
        raise HTTPException(
            409,
            "This node's output cell is blocked in a way that can't be resolved automatically -- inserting a row here would "
            "split another node's own row-span apart. Move things around manually first.",
        )

    old.status = NodeStatus.discarded

    new_node = Node(
        track_id=old.track_id,
        step_index=old.step_index,
        kind=NodeKind.workflow,
        node_type=old.node_type,
        template_id=old.template_id,
        inputs=old.inputs,
        params=old.params,
        requested_variants=old.requested_variants,
        backend_mode=old.backend_mode,
        manual_backend_id=old.manual_backend_id,
        use_api=old.use_api,
        status=NodeStatus.queued,
    )
    db.add(new_node)
    await db.commit()
    await db.refresh(new_node)

    await job_queue.enqueue(enqueue_node_job, str(new_node.id))
    return new_node
