"""Sub-dashboards and the smart pointers that reach them.

A **smart pointer** is an `asset.subgraph` node whose `subgraph_dashboard_id`
names a dashboard. It is a *reference*, not containment: several pointers may
name the same dashboard, and a pointer loop (A -> B -> A) is perfectly legal --
diving in is one deliberate click and you come back through navigation history,
not through structure. There is deliberately no menu listing every dashboard.

Reachability is guaranteed structurally instead, by two rules:

1. A subgraph node can never be moved out of the dashboard it was created in
   (enforced in nodes.py's move/update paths).
2. The dashboard's *main* pointer -- `Dashboard.owner_node_id` -- cannot be
   deleted while the dashboard still holds anything.

Together those make the main pointers a spanning tree rooted at the project's
main grid, so every non-empty dashboard has a path home. Every additional
pointer is a non-tree edge, which is exactly why deleting one is always safe.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import get_db
from app.core.storage import build_asset_url
from app.db.models import Asset, Dashboard, Node, NodeKind, NodeStatus, Track
from app.schemas.schemas import (
    DashboardCreate,
    DashboardRead,
    DashboardRename,
    PointerCreate,
    SetDashboardResult,
    TransferOwnership,
)

router = APIRouter(prefix="/api", tags=["dashboards"])

SUBGRAPH_NODE_TYPE = "asset.subgraph"


async def _dashboard_of_node(db: AsyncSession, node: Node) -> uuid.UUID | None:
    """Which grid scope a node sits in (None = the project's main grid)."""
    track = await db.get(Track, node.track_id)
    return track.dashboard_id if track else None


async def _live_node_count(db: AsyncSession, dashboard_id) -> int:
    result = await db.execute(
        select(Node.id)
        .join(Track, Track.id == Node.track_id)
        .where(Track.dashboard_id == dashboard_id, Node.status != NodeStatus.discarded)
    )
    return len(result.all())


async def _pointers_to(db: AsyncSession, dashboard_id) -> list[Node]:
    result = await db.execute(
        select(Node).where(Node.subgraph_dashboard_id == dashboard_id, Node.status != NodeStatus.discarded)
    )
    return list(result.scalars().all())


async def _owner_chain_reaches_root(db: AsyncSession, start_dashboard_id, *, forbidden_id) -> bool:
    """Walk the ownership chain up from `start_dashboard_id` and report whether
    it reaches the project's main grid without passing through `forbidden_id`.

    This is the transfer guard, and it is cheap on purpose: ownership is a
    *tree* (one owner per dashboard), so this follows one `owner_node_id` per
    hop rather than exploring the pointer graph, which may contain loops.

    The case it exists to reject: handing dashboard B's ownership to a pointer
    that lives somewhere only reachable *through* B. The chain would then close
    into a ring -- B owned from inside its own subtree -- and every dashboard in
    that ring becomes unreachable from the main grid while still holding work.

    Legitimate cross-links are unaffected. If A1 owns B, A2 owns C, and C holds
    a second pointer C3 into B, then moving B's ownership to C3 walks C -> A ->
    main, never meets B, and is allowed (as is moving it back).
    """
    seen: set[uuid.UUID] = set()
    current = start_dashboard_id
    while current is not None:
        if current == forbidden_id:
            return False
        if current in seen:
            # Only reachable if the ownership tree is already corrupt; treat it
            # as "does not reach root" rather than spinning.
            return False
        seen.add(current)
        dashboard = await db.get(Dashboard, current)
        if dashboard is None or dashboard.owner_node_id is None:
            return False
        owner = await db.get(Node, dashboard.owner_node_id)
        if owner is None:
            return False
        current = await _dashboard_of_node(db, owner)
    # Fell out at None -- the main grid, which is always reachable.
    return True


def _read(dashboard: Dashboard, node_count: int, pointer_count: int) -> DashboardRead:
    item = DashboardRead.model_validate(dashboard)
    item.node_count = node_count
    item.pointer_count = pointer_count
    if dashboard.result_asset_id is not None:
        item.result_asset_url = build_asset_url(dashboard.result_asset_id)
    return item


@router.post("/dashboards", response_model=DashboardRead, status_code=201)
async def create_dashboard(payload: DashboardCreate, db: AsyncSession = Depends(get_db)):
    """Turn an existing asset cell into a smart pointer on a brand-new,
    empty dashboard, and make it that dashboard's owner.

    Done in one call rather than "create node, then create dashboard" so a
    dashboard can never exist without the pointer that keeps it reachable.
    """
    node = await db.get(Node, payload.node_id)
    if node is None:
        raise HTTPException(404, "Node not found")
    if node.kind != NodeKind.asset:
        raise HTTPException(409, "Only an asset cell can become a subgraph pointer.")
    if node.subgraph_dashboard_id is not None:
        raise HTTPException(409, "This cell already points at a dashboard.")
    if node.created_by_node_id is not None:
        raise HTTPException(409, "A workflow's own output can't become a subgraph pointer -- use a free asset cell.")

    track = await db.get(Track, node.track_id)
    if track is None:
        raise HTTPException(409, "Node has no track.")

    dashboard = Dashboard(project_id=track.project_id, name=payload.name or "")
    db.add(dashboard)
    await db.flush()

    node.node_type = SUBGRAPH_NODE_TYPE
    node.subgraph_dashboard_id = dashboard.id
    dashboard.owner_node_id = node.id
    await db.commit()
    await db.refresh(dashboard)
    return _read(dashboard, 0, 1)


@router.post("/dashboards/{dashboard_id}/pointers", response_model=DashboardRead, status_code=201)
async def add_pointer(dashboard_id: uuid.UUID, payload: PointerCreate, db: AsyncSession = Depends(get_db)):
    """Point an additional asset cell at an existing dashboard. These extra
    pointers are non-tree edges: they never affect reachability, so unlike the
    owner they can be deleted at any time."""
    dashboard = await db.get(Dashboard, dashboard_id)
    if dashboard is None:
        raise HTTPException(404, "Dashboard not found")
    node = await db.get(Node, payload.node_id)
    if node is None:
        raise HTTPException(404, "Node not found")
    if node.kind != NodeKind.asset:
        raise HTTPException(409, "Only an asset cell can become a subgraph pointer.")
    if node.subgraph_dashboard_id is not None:
        raise HTTPException(409, "This cell already points at a dashboard.")

    track = await db.get(Track, node.track_id)
    if track is None or track.project_id != dashboard.project_id:
        raise HTTPException(409, "A pointer must live in the same project as the dashboard it opens.")

    node.node_type = SUBGRAPH_NODE_TYPE
    node.subgraph_dashboard_id = dashboard.id
    await db.commit()
    await db.refresh(dashboard)
    return _read(dashboard, await _live_node_count(db, dashboard.id), len(await _pointers_to(db, dashboard.id)))


@router.get("/dashboards/{dashboard_id}", response_model=DashboardRead)
async def get_dashboard(dashboard_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    dashboard = await db.get(Dashboard, dashboard_id)
    if dashboard is None:
        raise HTTPException(404, "Dashboard not found")
    return _read(dashboard, await _live_node_count(db, dashboard.id), len(await _pointers_to(db, dashboard.id)))


@router.patch("/dashboards/{dashboard_id}", response_model=DashboardRead)
async def rename_dashboard(dashboard_id: uuid.UUID, payload: DashboardRename, db: AsyncSession = Depends(get_db)):
    dashboard = await db.get(Dashboard, dashboard_id)
    if dashboard is None:
        raise HTTPException(404, "Dashboard not found")
    dashboard.name = payload.name
    await db.commit()
    await db.refresh(dashboard)
    return _read(dashboard, await _live_node_count(db, dashboard.id), len(await _pointers_to(db, dashboard.id)))


@router.post("/dashboards/{dashboard_id}/result", response_model=DashboardRead)
async def set_dashboard_result(dashboard_id: uuid.UUID, payload: SetDashboardResult, db: AsyncSession = Depends(get_db)):
    """Choose which asset inside this subgraph is its result -- the picture
    every pointer into it shows.

    Stored on the dashboard rather than on the pointer so two pointers can't
    drift to different faces. The asset must actually live in this subgraph:
    a face pointing at something in another grid would be a reference the user
    never made and can't see the origin of from here.
    """
    dashboard = await db.get(Dashboard, dashboard_id)
    if dashboard is None:
        raise HTTPException(404, "Dashboard not found")

    if payload.asset_id is None:
        dashboard.result_asset_id = None
    else:
        asset = await db.get(Asset, payload.asset_id)
        if asset is None:
            raise HTTPException(404, "Asset not found")
        owner = await db.get(Node, asset.node_id) if asset.node_id else None
        track = await db.get(Track, owner.track_id) if owner else None
        if track is None or track.dashboard_id != dashboard.id:
            raise HTTPException(409, "Pick an asset that lives inside this subgraph.")
        dashboard.result_asset_id = asset.id

    await db.commit()
    await db.refresh(dashboard)
    return _read(dashboard, await _live_node_count(db, dashboard.id), len(await _pointers_to(db, dashboard.id)))


@router.post("/dashboards/{dashboard_id}/transfer-ownership", response_model=DashboardRead)
async def transfer_ownership(dashboard_id: uuid.UUID, payload: TransferOwnership, db: AsyncSession = Depends(get_db)):
    """Hand the main-pointer role to another pointer at the same dashboard.

    Guarded by _owner_chain_reaches_root: the receiving pointer must sit
    somewhere that still reaches the main grid without going through this
    dashboard, or the ownership tree would close into a detached ring.
    """
    dashboard = await db.get(Dashboard, dashboard_id)
    if dashboard is None:
        raise HTTPException(404, "Dashboard not found")
    new_owner = await db.get(Node, payload.node_id)
    if new_owner is None:
        raise HTTPException(404, "Node not found")
    if new_owner.subgraph_dashboard_id != dashboard.id:
        raise HTTPException(409, "That node doesn't point at this dashboard.")
    if dashboard.owner_node_id == new_owner.id:
        return _read(dashboard, await _live_node_count(db, dashboard.id), len(await _pointers_to(db, dashboard.id)))

    host = await _dashboard_of_node(db, new_owner)
    if not await _owner_chain_reaches_root(db, host, forbidden_id=dashboard.id):
        raise HTTPException(
            409,
            "That pointer is only reachable through this dashboard -- handing ownership there would cut both off from the main grid.",
        )

    dashboard.owner_node_id = new_owner.id
    await db.commit()
    await db.refresh(dashboard)
    return _read(dashboard, await _live_node_count(db, dashboard.id), len(await _pointers_to(db, dashboard.id)))


async def enforce_pointer_deletion(db: AsyncSession, node: Node) -> None:
    """Called from delete_node before a node goes away.

    - Not a pointer, or not the owner -> nothing to do; deleting a non-tree
      edge can't strand anything.
    - Owner of a dashboard that still holds work -> refuse. The user is told to
      go in and clear it first, which is what keeps "delete" from ever being a
      one-click way to lose a whole chart.
    - Owner of an *empty* dashboard -> allowed. If other pointers remain, one is
      auto-promoted to owner (chosen by creation order, so it's deterministic);
      the user's rule was that promotion should never block them. If this was
      the last pointer, the now-unreachable empty dashboard is deleted with it.
    """
    if node.subgraph_dashboard_id is None:
        return
    dashboard = await db.get(Dashboard, node.subgraph_dashboard_id)
    if dashboard is None:
        return
    if dashboard.owner_node_id != node.id:
        return

    if await _live_node_count(db, dashboard.id) > 0:
        raise HTTPException(
            409,
            "This is the main pointer of a subgraph that still has content -- open it and clear it out before deleting.",
        )

    others = [p for p in await _pointers_to(db, dashboard.id) if p.id != node.id]
    if others:
        others.sort(key=lambda n: (n.created_at is None, n.created_at, str(n.id)))
        dashboard.owner_node_id = others[0].id
        await db.flush()
        return

    dashboard.owner_node_id = None
    await db.flush()
    await db.delete(dashboard)
    await db.flush()
