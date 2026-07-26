import uuid

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.node_types import resolve_effective_template, sync_legacy_fields
from app.core.queue import job_queue
from app.core.track_order import ordered_tracks, splice_after
from app.core.storage import build_asset_url, get_storage
from app.db.base import get_db
from app.db.models import Asset, AssetKind, Job, Node, NodeKind, NodeStatus, Project, Track
from app.schemas.schemas import AssetRead, JobRead, NodeCreate, NodeMove, NodeRead, NodeUpdate, PickCandidate
from app.worker.tasks import (
    _splice_after_would_split_a_span,
    enqueue_node_job,
    has_room_for_output,
    own_output_nodes,
    selected_or_latest_output,
)

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


async def _ensure_output_binding(db: AsyncSession, node: Node, target_track_id, target_step: int, is_move: bool = False) -> None:
    """A materialized output (Node.created_by_node_id set) stays in its
    creator's OWN output column (creator.step_index + 1), at the creator's row
    or BELOW it -- i.e. anywhere in the creator's output area, wherever the
    user drags it.

    It is NOT capped to a narrow max(image slots, 1 + spawned) span: the
    rendered card span follows its outputs (core/grid_layout.py), so any row in
    the output column below the creator is a legitimate spot; the old cap
    rejected pulling a bug-flung output back toward its creator (2026-07-22).

    And the rule ONLY applies to an output that's CURRENTLY in a valid spot
    (is_move only): if it's already OUT of its creator's area (something
    upstream misplaced it -- a worker quirk, an old bug), the user dragging it
    is trying to FIX that, so any target is allowed -- don't trap a broken node
    with the tidiness rule meant for a good one (2026-07-23).

    A node with no creator (manual upload, "+ asset", RefAsset) is unconstrained;
    a locked creator (collapsed chain, _is_locked) waives the rule entirely
    since it no longer renders a card to stay under. is_move=False (create) has
    no prior position, so it's validated as-is."""
    if node.created_by_node_id is None:
        return
    creator = await db.get(Node, node.created_by_node_id)
    if creator is None:
        return
    if await _is_locked(db, creator.id):
        return
    creator_track = await db.get(Track, creator.track_id)
    if creator_track is None:
        return
    ordered = await ordered_tracks(db, creator_track.project_id)
    pos = {t.id: i for i, t in enumerate(ordered)}
    creator_pos = pos.get(creator_track.id)
    if creator_pos is None:
        return

    # Already misplaced? Then this move is the user fixing it -- allow anywhere.
    if is_move:
        cur_pos = pos.get(node.track_id)
        currently_ok = node.step_index == creator.step_index + 1 and cur_pos is not None and cur_pos >= creator_pos
        if not currently_ok:
            return

    denied = HTTPException(409, "A workflow output can only move within its creator's output column, at or below it.")
    if target_step != creator.step_index + 1:
        raise denied
    target_track = await db.get(Track, target_track_id)
    if target_track is None:
        raise denied
    target_pos = pos.get(target_track.id)
    if target_pos is None or target_pos < creator_pos:
        raise denied


async def _is_locked(db: AsyncSession, node_id) -> bool:
    """True once a node is part of a collapsed chain -- either it's the
    creator of some collapsed asset or that asset's collapse target (see
    db/models.py's Node.collapse_target_id docstring)."""
    result = await db.execute(
        select(Node.id).where(
            Node.kind == NodeKind.asset,
            Node.collapse_target_id.isnot(None),
            or_(Node.created_by_node_id == node_id, Node.collapse_target_id == node_id),
        )
    )
    return result.first() is not None


async def _reject_if_locked(db: AsyncSession, node: Node) -> None:
    """No generate/reroll/discard once a workflow node is locked (see
    _is_locked) -- collapsing is meant for finished history the user doesn't
    intend to touch again, so this is enforced here, not just by hiding the
    buttons in NodeCell.tsx."""
    if await _is_locked(db, node.id):
        raise HTTPException(409, "This node is part of a collapsed chain -- expand it first before regenerating.")


# A column index no real node ever uses -- a scratch parking spot so a
# two-node position swap never makes the (track, step) unique index (migration
# 0011) see a transient duplicate mid-swap.
_PARK_STEP = 1_000_000


async def _node_at(db: AsyncSession, track_id, step_index: int) -> Node | None:
    result = await db.execute(
        select(Node).where(Node.track_id == track_id, Node.step_index == step_index, Node.status != NodeStatus.discarded)
    )
    return result.scalars().first()


async def _actual_span(db: AsyncSession, node: Node, ordered: list[Track], pos: dict) -> int:
    """This workflow node's real row-span, computed authoritatively here (no
    longer mirrored on the client): desired = max(image/file input slots,
    1 + spawned tracks), capped at the first row below whose own column is
    already taken. Same formula as worker/tasks.py's _actual_row_span."""
    effective = await resolve_effective_template(db, node)
    fields = (effective.param_schema if effective else {}).get("fields", [])
    slot_count = len([f for f in fields if f.get("type") in ("image", "file")])
    spawned_result = await db.execute(select(func.count()).select_from(Track).where(Track.spawned_from_node_id == node.id))
    spawned_count = spawned_result.scalar_one()
    desired = max(slot_count, 1 + spawned_count, 1)
    start = pos.get(node.track_id)
    if start is None or desired <= 1:
        return 1
    span = 1
    while span < desired and start + span < len(ordered):
        if await _node_at(db, ordered[start + span].id, node.step_index) is not None:
            break
        span += 1
    return span


async def ensure_span_rows(db: AsyncSession, workflow_node: Node) -> None:
    """Imperative backend growth (replaces Grid.tsx's reactive auto-expand
    useEffect): make sure the empty input-slot rows a workflow needs actually
    exist as tracks right below it. Called when a workflow's template is
    assigned/changed (create/update_node), so a multi-input node (e.g.
    native.character_chart, 8 image slots) gets its 7 rows created up front
    instead of rendering tall into non-existent rows the user can't fill.

    desired = max(image/file input slots, 1 + spawned tracks). For each offset
    1..desired-1 below the node's own row: if a track already sits there with
    this workflow's own column free, it's fine; otherwise splice a fresh empty
    track in at that position -- unless doing so would tear some OTHER
    workflow's span apart (then stop, best-effort). Idempotent: re-running when
    the rows already exist splices nothing. Does not commit -- the caller does."""
    effective = await resolve_effective_template(db, workflow_node)
    if effective is None:
        return
    fields = (effective.param_schema or {}).get("fields", [])
    slot_count = len([f for f in fields if f.get("type") in ("image", "file")])
    spawned = await db.execute(select(func.count()).select_from(Track).where(Track.spawned_from_node_id == workflow_node.id))
    desired = max(slot_count, 1 + spawned.scalar_one(), 1)
    if desired <= 1:
        return

    home = await db.get(Track, workflow_node.track_id)
    if home is None:
        return
    project_id = home.project_id
    step = workflow_node.step_index

    offset = 1
    while offset < desired:
        ordered = await ordered_tracks(db, project_id)
        pos = {t.id: i for i, t in enumerate(ordered)}
        start = pos.get(workflow_node.track_id)
        if start is None:
            return
        target = start + offset
        if target < len(ordered):
            occupant = await _node_at(db, ordered[target].id, step)
            if occupant is None:
                offset += 1
                continue
            # Blocked: a fresh row must go in at `target`. Refuse only if that
            # shift would split an unrelated workflow's span (excluding this
            # one, whose span we're deliberately growing).
            if await _splice_after_would_split_a_span(db, project_id, target - 1, ordered, pos, exclude_node_id=workflow_node.id):
                return
        anchor = ordered[target - 1] if 0 <= target - 1 < len(ordered) else ordered[-1]
        new_track = Track(project_id=project_id)
        db.add(new_track)
        await db.flush()
        await splice_after(db, project_id, new_track, anchor)
        await db.flush()
        offset += 1


async def _move_asset(db: AsyncSession, node: Node, ordered: list[Track], target_row: int, target_step: int) -> None:
    target_track = ordered[target_row]
    occupant = await _node_at(db, target_track.id, target_step)
    if occupant is not None and occupant.id != node.id:
        if occupant.kind != NodeKind.asset:
            raise HTTPException(409, "Can't swap with that cell.")
        old_track_id, old_step = node.track_id, node.step_index
        # Both nodes must land in a spot allowed for them (an output stays in
        # its creator's output column, at or below it -- see _ensure_output_binding).
        await _ensure_output_binding(db, node, target_track.id, target_step, is_move=True)
        await _ensure_output_binding(db, occupant, old_track_id, old_step, is_move=True)
        # Swap through a scratch column so the (track, step) unique index never
        # sees a transient duplicate.
        node.step_index = _PARK_STEP
        await db.flush()
        occupant.track_id, occupant.step_index = old_track_id, old_step
        await db.flush()
        node.track_id, node.step_index = target_track.id, target_step
        await db.flush()
    else:
        await _ensure_slot_free(db, target_track.id, target_step, exclude_node_id=node.id)
        await _ensure_output_binding(db, node, target_track.id, target_step, is_move=True)
        node.track_id, node.step_index = target_track.id, target_step
    await db.commit()


async def _move_workflow(db: AsyncSession, node: Node, ordered: list[Track], pos: dict, target_row: int, target_step: int) -> None:
    cur_row = pos[node.track_id]
    cur_step = node.step_index
    row_delta = target_row - cur_row
    step_delta = target_step - cur_step
    if step_delta % 2 != 0:
        raise HTTPException(409, "A workflow node can only move left/right in even column steps (keeps the asset/workflow column pattern intact).")

    span = await _actual_span(db, node, ordered, pos)
    # Everything currently aligned to this workflow's span (its input column
    # step-1 and output column step+1) rides along by the same delta.
    dependents: list[Node] = []
    for r in range(cur_row, cur_row + span):
        for step in (cur_step - 1, cur_step + 1):
            if step < 0:
                continue
            n = await _node_at(db, ordered[r].id, step)
            if n is not None and n.id != node.id:
                dependents.append(n)

    moves = [(node, target_row, target_step)]
    for d in dependents:
        moves.append((d, pos[d.track_id] + row_delta, d.step_index + step_delta))
    moving_ids = {m[0].id for m in moves}

    # Every landing cell must be on the grid and free of any non-mover. No
    # auto-insert-to-make-room here (yet) -- a clear 409 beats a surprise shove.
    for _n, tr, ts in moves:
        if not (0 <= tr < len(ordered)) or ts < 0:
            raise HTTPException(409, "That move would land a cell off the grid.")
        occ = await _node_at(db, ordered[tr].id, ts)
        if occ is not None and occ.id not in moving_ids:
            raise HTTPException(409, "Target area is occupied -- drop onto an empty area, or clear it first.")

    # Apply furthest-along-the-shift-first so each target is already vacated by
    # the time its own write runs, flushing after each so the (track, step)
    # unique index sees them one at a time in this safe order.
    def sort_key(m):
        n = m[0]
        return -(pos[n.track_id] * row_delta + n.step_index * step_delta)

    for n, tr, ts in sorted(moves, key=sort_key):
        n.track_id = ordered[tr].id
        n.step_index = ts
        await db.flush()
    await db.commit()


@router.post("/{node_id}/move", response_model=list[NodeRead])
async def move_node(node_id: uuid.UUID, payload: NodeMove, db: AsyncSession = Depends(get_db)):
    """Backend-authoritative node move. The client sends only the intent (which
    grid cell the user dropped onto); ALL placement logic lives here -- resolve
    the target row from the project's track order, validate output-binding /
    slot rules, carry a workflow node's dependents along, and reject (409) if
    the target isn't free. The client re-fetches the authoritative layout
    after; it does no placement math of its own anymore."""
    node = await db.get(Node, node_id)
    if not node:
        raise HTTPException(404, "Node not found")
    home = await db.get(Track, node.track_id)
    if home is None:
        raise HTTPException(409, "Node has no track.")
    ordered = await ordered_tracks(db, home.project_id)
    pos = {t.id: i for i, t in enumerate(ordered)}
    cur_row = pos.get(node.track_id)
    if cur_row is None:
        raise HTTPException(409, "Node's track is not in the project order.")
    if payload.target_row == cur_row and payload.target_step == node.step_index:
        return [node]
    if not (0 <= payload.target_row < len(ordered)) or payload.target_step < 0:
        raise HTTPException(409, "Target cell is off the grid.")

    if node.kind == NodeKind.asset:
        await _move_asset(db, node, ordered, payload.target_row, payload.target_step)
    else:
        await _move_workflow(db, node, ordered, pos, payload.target_row, payload.target_step)

    await db.refresh(node)
    return [node]


async def _desired_span(db: AsyncSession, workflow_node: Node) -> int:
    """max(image/file input slots, 1 + spawned tracks) -- what the workflow's
    card wants to be tall. Shared by the growth / span-shift / pick helpers."""
    effective = await resolve_effective_template(db, workflow_node)
    fields = (effective.param_schema if effective else {}).get("fields", [])
    slot_count = len([f for f in fields if f.get("type") in ("image", "file")])
    spawned = await db.execute(select(func.count()).select_from(Track).where(Track.spawned_from_node_id == workflow_node.id))
    return max(slot_count, 1 + spawned.scalar_one(), 1)


async def _ensure_next_workflow_step(db: AsyncSession, track_id, step: int) -> None:
    """After a candidate settles, make sure the workflow cell that continues the
    pipeline (this asset cell's step+1, always a workflow-parity column) exists."""
    if await _node_at(db, track_id, step + 1) is None:
        db.add(Node(track_id=track_id, step_index=step + 1, kind=NodeKind.workflow))
        await db.flush()


async def _pick_candidate(db: AsyncSession, picker: Node, kept: Asset) -> Node | None:
    """The whole candidate fork, server-side (ports Grid.tsx's onSelectCandidate):
    settle `kept` in the picker's own cell as a fresh asset.single node, and
    relocate the picker (still holding the remaining candidates) to a reused
    empty row inside the producing workflow's span, or a freshly spliced spawned
    track. Returns the relocated picker, or None if `kept` was the sole
    remaining candidate (then the picker is just settled in place). Does not
    commit."""
    result = await db.execute(select(Asset).where(Asset.node_id == picker.id))
    others = [a for a in result.scalars().all() if a.id != kept.id]

    original_track_id = picker.track_id
    original_step = picker.step_index

    if not others:
        picker.node_type = "asset.single"
        picker.is_picker = False
        await db.flush()
        await _ensure_next_workflow_step(db, original_track_id, original_step)
        return None

    home = await db.get(Track, original_track_id)
    project_id = home.project_id
    ordered = await ordered_tracks(db, project_id)
    pos = {t.id: i for i, t in enumerate(ordered)}
    original_row = pos.get(original_track_id, 0)

    preceding = await _node_at(db, original_track_id, original_step - 1) if original_step >= 1 else None
    if preceding is not None and preceding.kind != NodeKind.workflow:
        preceding = None
    # Attribute the branch to whatever produced these candidates, not the picker
    # itself: the preceding workflow, else (a cascade's intermediate picker) the
    # cause its own track was already spawned from, else itself.
    cause_node_id = preceding.id if preceding is not None else (home.spawned_from_node_id or picker.id)

    # Place the leftover picker on a fresh spawned track right AFTER the
    # producing workflow's current lowest output, so every candidate line stays
    # contiguous just below the producer -- never flung to the bottom of the
    # project (the old fallback tail-appended whenever an adjacent insert
    # "would break an output binding", which in a dense project fired
    # constantly and sent the line far away; a far output then also bloated the
    # producer's rendered span over unrelated rows -- 2026-07-22). Inserting
    # just past the producer's lowest output only shifts whole workflow blocks
    # below it down as units, so it can't split anyone's input span.
    producer = await db.get(Node, picker.created_by_node_id) if picker.created_by_node_id else preceding
    insert_after = original_row
    if producer is not None:
        ppos = pos.get(producer.track_id)
        if ppos is not None:
            insert_after = ppos
            result = await db.execute(
                select(Node).where(Node.created_by_node_id == producer.id, Node.status != NodeStatus.discarded)
            )
            for out in result.scalars().all():
                opos = pos.get(out.track_id)
                if opos is not None and opos > insert_after:
                    insert_after = opos

    new_track = Track(project_id=project_id, spawned_from_node_id=cause_node_id, spawned_from_output_id=kept.id)
    db.add(new_track)
    await db.flush()
    anchor = ordered[insert_after] if 0 <= insert_after < len(ordered) else (ordered[-1] if ordered else None)
    await splice_after(db, project_id, new_track, anchor)
    await db.flush()
    picker.track_id = new_track.id
    await db.flush()

    # The settled node takes over the picker's EXACT cell -- the picker was
    # placed there by the worker (_locate_output_row, which can legitimately
    # place an output beyond the creator's input-slot span, unlike the stricter
    # _ensure_output_binding used for MOVES). So it's not re-validated here: a
    # node replacing another at the same cell is valid wherever that cell
    # already was (this is what made pick silently 409 when the worker had put
    # the picker one row past the creator's span -- 2026-07-22).
    settled = Node(
        track_id=original_track_id,
        step_index=original_step,
        kind=NodeKind.asset,
        node_type="asset.single",
        created_by_node_id=picker.created_by_node_id,
        status=NodeStatus.done,
    )
    db.add(settled)
    await db.flush()
    kept.node_id = settled.id
    await db.flush()

    await _ensure_next_workflow_step(db, original_track_id, original_step)
    # Spawning this leftover picker grew the producing workflow's span (its
    # desired = 1 + spawned tracks) -- materialize the rows so its card
    # actually stretches to reach the new spawned track. This is the imperative
    # replacement for the old reactive auto-expand effect, at the mutation that
    # changes the span (a candidate fork), same as template assignment.
    cause_workflow = await db.get(Node, cause_node_id)
    if cause_workflow is not None and cause_workflow.kind == NodeKind.workflow:
        await ensure_span_rows(db, cause_workflow)
    return picker


@router.post("/{node_id}/pick-candidate", response_model=NodeRead)
async def pick_candidate(node_id: uuid.UUID, payload: PickCandidate, db: AsyncSession = Depends(get_db)):
    """Keep one candidate, fork the rest onto their own line -- backend owns the
    whole reshuffle atomically (see _pick_candidate). The client just names the
    kept asset and re-fetches the layout."""
    picker = await db.get(Node, node_id)
    if not picker:
        raise HTTPException(404, "Node not found")
    if picker.kind != NodeKind.asset:
        raise HTTPException(400, "Only an asset picker node can settle a candidate")
    kept = await db.get(Asset, payload.kept_asset_id)
    if kept is None or kept.node_id != picker.id:
        raise HTTPException(400, "That asset isn't one of this picker's candidates")
    await _pick_candidate(db, picker, kept)
    await db.commit()
    await db.refresh(picker)
    return picker


@router.post("/{node_id}/pick-all-candidates", status_code=204)
async def pick_all_candidates(node_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Cascade every remaining candidate into its own line (the "Select all"
    action) -- repeatedly settle the oldest asset and follow the leftover
    picker, all in one transaction."""
    picker = await db.get(Node, node_id)
    if not picker:
        raise HTTPException(404, "Node not found")
    if picker.kind != NodeKind.asset:
        raise HTTPException(400, "Only an asset picker node can settle candidates")
    while True:
        result = await db.execute(select(Asset).where(Asset.node_id == picker.id).order_by(Asset.created_at))
        assets = list(result.scalars().all())
        if not assets:
            break
        leftover = await _pick_candidate(db, picker, assets[0])
        if leftover is None:
            break
        picker = leftover
    await db.commit()


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
    await db.flush()
    # A workflow created already carrying a template needs its input-slot rows
    # materialized too (see ensure_span_rows) -- same as the update path.
    if node.kind == NodeKind.workflow and node.node_type:
        await ensure_span_rows(db, node)
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
        await _ensure_output_binding(db, node, target_track_id, target_step, is_move=True)

    data = payload.model_dump(mode="json", exclude_unset=True)
    for field, value in data.items():
        setattr(node, field, value)
    if "node_type" in data:
        effective = await resolve_effective_template(db, node)
        sync_legacy_fields(node, effective)
        # Template (re)assigned -- materialize the input-slot rows this
        # workflow now needs, right below it (see ensure_span_rows). Replaces
        # the frontend's reactive auto-expand effect.
        if node.kind == NodeKind.workflow:
            await ensure_span_rows(db, node)
    await db.commit()
    await db.refresh(node)
    return node


@router.delete("/{node_id}", status_code=204)
async def delete_node(node_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Deletes this one node -- an asset node never cascades to anything
    else, not even something sitting after it in the same track: in the
    row-span paradigm, inputs/outputs are addressed by
    Node.created_by_node_id / cell_index, not by "whatever's next in this
    track", so nothing genuinely depends on a plain asset that way anymore
    (2026-07-21 incident: deleting an asset in column 0 also deleted an
    unrelated, later asset the user had separately placed in column 2 of the
    same track, purely because it came after it positionally -- the old
    step_index >= this one sweep below didn't check kind or any real
    relationship at all).

    A workflow node DOES cascade, but only to its own VERIFIED outputs
    (Node.created_by_node_id == this node's id, see worker/tasks.py's
    own_output_nodes, which finds them regardless of which track they
    actually live in) -- never to whatever else happens to sit after it
    positionally.

    Deliberately does NOT touch any sibling spawned track. A previous version
    "promoted" a leftover-candidate sibling track into the freed-up slot by
    re-parenting all its nodes into this node's track and deleting the sibling
    track -- but that re-parent skipped the (track_id, step_index) uniqueness
    guard _ensure_slot_free enforces everywhere else, so if the sibling had
    any downstream content in a column this track already occupied, two live
    nodes collided in one cell (one then silently unreachable in the grid),
    and the sibling track deletion risked cascading its own nodes away. Both
    are real data-loss paths, and the convenience never justified them: the
    leftover branch simply stays where it is now, exactly as the user left it.
    """
    node = await db.get(Node, node_id)
    if not node:
        raise HTTPException(404, "Node not found")

    nodes_to_delete = [node]
    if node.kind == NodeKind.workflow:
        nodes_to_delete.extend(await own_output_nodes(db, node.id))

    node_ids = [n.id for n in nodes_to_delete]

    if node_ids:
        result = await db.execute(select(Asset).where(Asset.node_id.in_(node_ids)))
        storage = get_storage()
        for asset in result.scalars().all():
            storage.delete_object(asset.storage_key)

    for n in nodes_to_delete:
        await db.delete(n)

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
    """Manual fill for an asset-kind node (the grid's start cell) -- no
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


@router.post("/{node_id}/collapse", response_model=NodeRead)
async def collapse_node(node_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Folds a workflow -> asset -> workflow chain (e.g. crop then upscale)
    into one card at the first workflow's cell -- for a passthrough asset the
    user cares about the lineage of but not individually. node_id here is
    that passthrough asset's own id (the one that ends up holding
    collapse_target_id), not either workflow node's id -- see db/models.py's
    Node.collapse_target_id docstring and NodeCell.tsx's "Collapse chain"
    button on BaseAssetNodeView."""
    asset = await db.get(Node, node_id)
    if asset is None:
        raise HTTPException(404, "Node not found")
    if asset.kind != NodeKind.asset or asset.created_by_node_id is None:
        raise HTTPException(409, "Only a workflow's own materialized output asset can be collapsed")
    if asset.collapse_target_id is not None:
        raise HTTPException(409, "This asset is already collapsed")

    creator_outputs = await own_output_nodes(db, asset.created_by_node_id)
    if len(creator_outputs) != 1 or creator_outputs[0].id != asset.id:
        raise HTTPException(409, "This asset isn't the sole current output of its creator workflow")

    result = await db.execute(
        select(Node).where(
            Node.track_id == asset.track_id,
            Node.step_index == asset.step_index + 1,
            Node.kind == NodeKind.workflow,
            Node.status != NodeStatus.discarded,
        )
    )
    target = result.scalars().first()
    # InputRef serializes with every union field present (node_id/asset_id/
    # output_id/value all null for a cell_index ref) -- match by the two keys
    # that matter, not exact dict equality, or every stored ref (a superset
    # of {"type","index"}) fails this check even for the simple default case.
    slot0 = target.inputs[0] if target and target.inputs else None
    if target is None or slot0 is None or slot0.get("type") != "cell_index" or slot0.get("index") != 0:
        raise HTTPException(
            409,
            "This asset isn't consumed at slot 0 by exactly one workflow node right after it in this track -- "
            "collapse only supports that simple chain shape today.",
        )

    # Sole-input safety: nothing else in the project (e.g. a "+ ref
    # elsewhere" cell) explicitly points at this asset -- collapsing it would
    # hide something still genuinely in use elsewhere.
    track = await db.get(Track, asset.track_id)
    result = await db.execute(
        select(Node.inputs).join(Track, Track.id == Node.track_id).where(Track.project_id == track.project_id, Node.id != target.id)
    )
    for (other_inputs,) in result.all():
        for ref in other_inputs or []:
            if ref.get("type") == "explicit" and ref.get("node_id") == str(asset.id):
                raise HTTPException(
                    409,
                    "This asset is referenced elsewhere (e.g. a reference cell) -- collapsing would hide something still in use.",
                )

    # Actually vacate this asset's and target's own two columns (S+1, S+2 --
    # not just hide their rendering) by parking both past whatever this track
    # currently uses, so S+1/S+2 become genuinely free for new content --
    # e.g. dragging the consumer's own real output closer, now that
    # _ensure_output_binding no longer pins it near a creator that doesn't
    # render (see that function's _is_locked check). Only step_index moves,
    # never track_id -- same row, just further out where nothing renders
    # (hiddenChainNodeIds) so it's invisible regardless of exact column.
    # Neither move touches _ensure_output_binding (a direct column write, not
    # a PATCH) -- that's fine, both nodes already count as locked the moment
    # collapse_target_id below is set, which is exactly the condition that
    # function itself now waives the position constraint for.
    project = await db.get(Project, track.project_id)
    result = await db.execute(
        select(Node.step_index).where(Node.track_id == asset.track_id, Node.status != NodeStatus.discarded)
    )
    max_step = max((s for (s,) in result.all()), default=-1)
    park_asset_step = max_step + 1
    if _kind_for_step(project.start_kind, park_asset_step) != NodeKind.asset:
        park_asset_step += 1
    park_target_step = park_asset_step + 1

    asset.step_index = park_asset_step
    target.step_index = park_target_step
    asset.collapse_target_id = target.id
    await db.commit()
    await db.refresh(asset)
    return asset


@router.post("/{node_id}/expand", response_model=NodeRead)
async def expand_node(node_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Reverses collapse_node's parking move -- best-effort: if the user put
    something new in this chain's old S+1/S+2 columns since collapsing (the
    whole point of freeing them), there's nowhere left to restore this asset
    and its consumer to, and this 409s explaining that rather than silently
    colliding with it or picking some other spot the user never asked for."""
    asset = await db.get(Node, node_id)
    if asset is None:
        raise HTTPException(404, "Node not found")
    if asset.collapse_target_id is None:
        raise HTTPException(409, "This asset isn't collapsed")
    creator = await db.get(Node, asset.created_by_node_id)
    target = await db.get(Node, asset.collapse_target_id)
    if creator is None or target is None:
        # Creator or consumer got deleted out from under this collapse --
        # nothing coherent to restore a position relative to; just unlock.
        asset.collapse_target_id = None
        await db.commit()
        await db.refresh(asset)
        return asset

    restore_asset_step = creator.step_index + 1
    restore_target_step = restore_asset_step + 1
    result = await db.execute(
        select(Node).where(
            Node.track_id == asset.track_id,
            Node.step_index.in_([restore_asset_step, restore_target_step]),
            Node.status != NodeStatus.discarded,
            Node.id.notin_([asset.id, target.id]),
        )
    )
    blocker = result.scalars().first()
    if blocker is not None:
        raise HTTPException(
            409,
            "Something else now occupies this chain's original cells -- move or remove it first, then expand again.",
        )

    asset.step_index = restore_asset_step
    target.step_index = restore_target_step
    asset.collapse_target_id = None
    await db.commit()
    await db.refresh(asset)
    return asset


@router.post("/{node_id}/generate", response_model=NodeRead)
async def generate_node(node_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    node = await db.get(Node, node_id)
    if not node:
        raise HTTPException(404, "Node not found")
    if node.kind != NodeKind.workflow:
        raise HTTPException(400, "Only workflow-kind nodes can be generated -- asset nodes are filled by upload or by their source workflow")
    await _reject_if_locked(db, node)
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
    if node.kind == NodeKind.asset and node.collapse_target_id is not None:
        raise HTTPException(409, "This asset is collapsed -- expand it first before discarding.")
    if node.kind == NodeKind.workflow:
        await _reject_if_locked(db, node)
    node.status = NodeStatus.discarded
    await db.commit()
    await db.refresh(node)
    return node


@router.post("/{node_id}/reroll", response_model=NodeRead, status_code=201)
async def reroll_node(node_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Re-roll: none of the N variants were good -> the old step is marked
    discarded (kept for history) and a new step with the same inputs is created
    and immediately queued for a fresh generation."""
    old = await db.get(Node, node_id)
    if not old:
        raise HTTPException(404, "Node not found")
    if old.kind != NodeKind.workflow:
        raise HTTPException(400, "Only workflow-kind nodes can be re-rolled -- re-upload to an asset node instead")
    await _reject_if_locked(db, old)

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
    # Flush the discard BEFORE inserting new_node at the same (track_id,
    # step_index): the partial unique index uq_nodes_live_cell (migration
    # 0011) only excludes discarded rows, and SQLAlchemy's unit of work can
    # otherwise order this INSERT ahead of the UPDATE within one flush,
    # tripping the constraint on a transient duplicate that resolves a
    # statement later. Flushing here makes old already-discarded in the DB
    # when new_node inserts.
    await db.flush()

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
