import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.grid_layout import compute_layout
from app.core.grid_scope import scope_start_kind, set_scope_start_kind
from app.core.track_order import ordered_tracks, scope_of, splice_after, unlink_track
from app.db.base import get_db
from app.db.models import Dashboard, Node, NodeStatus, Track
from app.schemas.schemas import NodeRead, TrackCreate, TrackRead, TracksMove

router = APIRouter(prefix="/api/tracks", tags=["tracks"])


@router.post("", response_model=TrackRead, status_code=201)
async def create_track(payload: TrackCreate, db: AsyncSession = Depends(get_db)):
    """Ordering is a per-scope linked list (core/track_order.py), so a new
    track is placed *relative* to the others -- after a given track, at the
    head, or (default) appended at the tail -- never at a numeric row_index
    the caller has to compute and keep contiguous."""
    dashboard_id = payload.dashboard_id
    after: Track | None = None
    if payload.after_track_id is not None:
        after = await db.get(Track, payload.after_track_id)
        if after is None:
            raise HTTPException(404, "after_track_id not found")
        # The anchor decides the scope: splicing into a list means joining it,
        # so a mismatched dashboard_id from the client can't split a chain
        # across two grids.
        dashboard_id = after.dashboard_id
    elif not payload.place_at_head:
        # No anchor given and not head -> append at this scope's current tail.
        ordered = await ordered_tracks(db, payload.project_id, dashboard_id)
        after = ordered[-1] if ordered else None

    track = Track(
        project_id=payload.project_id,
        dashboard_id=dashboard_id,
        spawned_from_node_id=payload.spawned_from_node_id,
        spawned_from_output_id=payload.spawned_from_output_id,
    )
    db.add(track)
    await db.flush()
    await splice_after(db, payload.project_id, track, after, at_head=payload.place_at_head, dashboard_id=dashboard_id)
    await db.commit()
    await db.refresh(track)
    return track


@router.get("/{track_id}", response_model=TrackRead)
async def get_track(track_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    track = await db.get(Track, track_id)
    if not track:
        raise HTTPException(404, "Track not found")
    return track


@router.get("/{track_id}/nodes", response_model=list[NodeRead])
async def list_track_nodes(track_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Node).where(Node.track_id == track_id).order_by(Node.step_index))
    return result.scalars().all()


@router.delete("/{track_id}", status_code=204)
async def delete_track(track_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Splice the track out of its chain (stitching its neighbours together)
    then delete it -- a couple of pointer writes, no renumbering of anything
    else. The old reindex-every-track-below pass (and its non-atomic partial
    failures) is gone entirely; the visible row numbers just re-derive from
    the shorter list on the next load.

    Refused if the row is one of a multi-input workflow's own span rows (an
    input-slot row below the workflow's anchor): deleting it silently shrank
    the workflow's span and dropped an input. A row is a span row exactly when
    it shows up in compute_layout's blocked_cells (the cells a spanning card
    covers in its own column). The workflow's own anchor row isn't a span row,
    so deleting that -- which cascades the workflow itself away -- is still
    allowed."""
    track = await db.get(Track, track_id)
    if not track:
        raise HTTPException(404, "Track not found")
    ordered = await ordered_tracks(db, *scope_of(track))
    pos = {t.id: i for i, t in enumerate(ordered)}
    row = pos.get(track_id)
    if row is not None:
        layout = await compute_layout(db, track.project_id)
        if any(r == row for r, _col in layout["blocked_cells"]):
            raise HTTPException(
                409,
                "This row is part of a multi-input workflow's span -- move or remove that workflow first, then delete the row.",
            )
    await unlink_track(db, track)
    await db.delete(track)
    await db.commit()


@router.post("/move-to-dashboard", response_model=list[TrackRead])
async def move_tracks_to_dashboard(payload: TracksMove, db: AsyncSession = Depends(get_db)):
    """Move a contiguous run of tracks into another grid scope, as one unit.

    This is how existing work gets *into* a sub-dashboard -- the only supported
    way, because a position means nothing outside its own grid. Single nodes
    deliberately can't cross (see nodes.py's _ensure_same_scope); a whole track
    can, provided the selection is closed under everything that reads position:

    - **Row-spans.** A workflow resolves its image slots by counting rows below
      its own (`cell_index`), so every row its card actually spans has to travel
      with it. Moving 5 of a 6-row block would silently re-point slots at
      whatever ends up adjacent afterwards -- the same hazard
      _splice_after_would_split_a_span guards for insertion.
    - **Creator/output pairs.** An output is pinned to its creator's column and
      row range (_ensure_output_binding); split them across two grids and that
      relationship no longer has a coordinate system to be expressed in.
    - **Spawned tracks.** A candidate line branched off a node in the selection
      sits outside that node's span, so it has to be named explicitly or it
      would be left behind pointing at a node in another grid.
    - **Smart pointers don't travel at all.** A selection containing one is
      refused outright rather than analysed: a pointer is pinned to the grid it
      was created in, and that pinning is exactly what makes the main pointers a
      spanning tree. To have a pointer somewhere else, create a new one there
      and transfer ownership to it.

    Contiguity is required for the same reason: the tracks keep their relative
    order, so offsets between them survive the move only if there was no gap to
    close.

    Column parity is realigned rather than refused. If the destination's
    start_kind differs, every moved node shifts one column right -- the same
    arithmetic collapse_node already does -- so an asset column stays an asset
    column. An empty destination simply adopts the source's start_kind.
    """
    if not payload.track_ids:
        raise HTTPException(400, "No tracks given.")

    tracks = []
    for tid in payload.track_ids:
        track = await db.get(Track, tid)
        if track is None:
            raise HTTPException(404, f"Track {tid} not found")
        tracks.append(track)

    project_id = tracks[0].project_id
    source_dashboard = tracks[0].dashboard_id
    if any(t.project_id != project_id for t in tracks):
        raise HTTPException(409, "All tracks must belong to one project.")
    if any(t.dashboard_id != source_dashboard for t in tracks):
        raise HTTPException(409, "All tracks must currently be in the same grid.")

    target_dashboard = payload.dashboard_id
    if target_dashboard == source_dashboard:
        raise HTTPException(409, "Those tracks are already in that grid.")
    if target_dashboard is not None:
        dest = await db.get(Dashboard, target_dashboard)
        if dest is None:
            raise HTTPException(404, "Dashboard not found")
        if dest.project_id != project_id:
            raise HTTPException(409, "A track can only move between grids of its own project.")

    ordered = await ordered_tracks(db, project_id, source_dashboard)
    pos = {t.id: i for i, t in enumerate(ordered)}
    moving = {t.id for t in tracks}
    indices = sorted(pos[t.id] for t in tracks if t.id in pos)
    if len(indices) != len(tracks):
        raise HTTPException(409, "A track isn't in its own grid's order -- refusing to move a broken chain.")
    if indices != list(range(indices[0], indices[0] + len(indices))):
        raise HTTPException(409, "Select a contiguous run of tracks -- a gap would change the spacing they rely on.")

    # In source list order, so the destination sees them exactly as they were.
    tracks = [ordered[i] for i in indices]

    result = await db.execute(
        select(Node).where(Node.track_id.in_([t.id for t in ordered]), Node.status != NodeStatus.discarded)
    )
    scope_nodes = list(result.scalars().all())
    node_by_id = {n.id: n for n in scope_nodes}

    layout = await compute_layout(db, project_id, source_dashboard)
    for node in scope_nodes:
        if node.track_id not in moving:
            continue
        span = layout["spans"].get(str(node.id), {}).get("achieved", 1)
        start = pos.get(node.track_id)
        if start is None:
            continue
        for row in range(start, min(start + span, len(ordered))):
            if ordered[row].id not in moving:
                raise HTTPException(
                    409,
                    "A workflow node's row-span reaches past the selection -- include every row its card covers.",
                )

    for node in scope_nodes:
        inside = node.track_id in moving
        if node.created_by_node_id is not None:
            creator = node_by_id.get(node.created_by_node_id)
            if creator is not None and (creator.track_id in moving) != inside:
                raise HTTPException(
                    409,
                    "A workflow and its own output would end up in different grids -- move them together.",
                )

    spawned = await db.execute(
        select(Track).where(Track.spawned_from_node_id.in_([n.id for n in scope_nodes if n.track_id in moving]))
    )
    for track in spawned.scalars().all():
        if track.id not in moving and track.dashboard_id == source_dashboard:
            raise HTTPException(
                409,
                "A candidate line spawned from this selection would be left behind -- include its track too.",
            )

    # A smart pointer never rides along -- flat refusal, no reachability
    # analysis. A pointer is pinned to the dashboard it was created in (that
    # pinning is what makes the main pointers a spanning tree), and letting a
    # track carry one across would be a way around it: a pointer moved into its
    # own subgraph takes the only route in with it. The supported way to have a
    # pointer somewhere else is to make a new one there and transfer ownership
    # to it, which is checked properly on its own endpoint.
    for node in scope_nodes:
        if node.track_id in moving and node.subgraph_dashboard_id is not None:
            raise HTTPException(
                409,
                "That selection contains a subgraph pointer, which can't be moved between grids -- create a new pointer where you want it and transfer ownership to it instead.",
            )

    source_kind = await scope_start_kind(db, project_id, source_dashboard)
    target_kind = await scope_start_kind(db, project_id, target_dashboard)
    shift = 0
    if source_kind is not None:
        if target_kind is None:
            await set_scope_start_kind(db, project_id, target_dashboard, source_kind)
        elif target_kind != source_kind:
            # Parities are opposite, so one column right puts every node back in
            # a column of its own kind.
            shift = 1

    if shift:
        for node in scope_nodes:
            if node.track_id in moving:
                node.step_index += shift
        await db.flush()

    for track in tracks:
        await unlink_track(db, track)
    await db.flush()

    dest_ordered = await ordered_tracks(db, project_id, target_dashboard)
    anchor = dest_ordered[-1] if dest_ordered else None
    for track in tracks:
        track.dashboard_id = target_dashboard
        await db.flush()
        await splice_after(db, project_id, track, anchor, at_head=anchor is None, dashboard_id=target_dashboard)
        await db.flush()
        anchor = track

    await db.commit()
    return await ordered_tracks(db, project_id, target_dashboard)
