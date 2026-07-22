"""Backend-computed grid layout -- the authoritative source for the derived
numbers the frontend used to compute (and repeatedly drift on): each workflow
node's row-span and the set of cells its spanning card blocks.

Moving these here is the thin-client step: the frontend stops mirroring the
span formula (desiredRowSpanByNode / rowSpanByNode / blockingNodeByCell in
Grid.tsx, the source of several span-drift incidents) and just renders what
this returns. The formula lives once, next to the enforcement that already
uses it (_ensure_output_binding, worker/_actual_row_span).
"""
from sqlalchemy import func, select

from app.core.node_types import resolve_effective_template
from app.core.track_order import ordered_tracks
from app.db.models import Node, NodeKind, NodeStatus, Track


async def compute_layout(db, project_id) -> dict:
    """Returns {spans, blocked_cells}:

    - spans: {node_id: {desired, achieved}} for every live workflow node.
      desired = max(image/file input slots, 1 + spawned tracks). achieved =
      desired capped at the first row below whose own column is already taken
      (a spanning card can't overlap an unrelated node in its own column).
    - blocked_cells: [[row, col], ...] -- the cells a spanning card covers in
      its OWN column below its anchor row (row is a position in list order).
      An unrelated track sharing that column must treat these as occupied.

    row/column of each node are NOT returned: they're trivially the node's
    track position in list order + its step_index, which the client already
    has from the ordered track list -- only the drift-prone derived numbers
    move here.
    """
    ordered = await ordered_tracks(db, project_id)
    pos = {t.id: i for i, t in enumerate(ordered)}

    result = await db.execute(
        select(Node)
        .join(Track, Track.id == Node.track_id)
        .where(Track.project_id == project_id, Node.status != NodeStatus.discarded)
    )
    nodes = list(result.scalars().all())

    by_id = {n.id: n for n in nodes}
    occupied: set[tuple[int, int]] = set()
    for n in nodes:
        r = pos.get(n.track_id)
        if r is not None:
            occupied.add((r, n.step_index))

    # How far below its own row each workflow's furthest materialized output
    # actually sits: the worker (_locate_output_row) can place an output beyond
    # the creator's input-slot span (e.g. a 3rd generation lands at offset 2
    # when offsets 0-1 are taken), so the card has to stretch to reach it or it
    # renders too short (reported 2026-07-22: Copy Pose with 3 outputs stayed
    # 2 rows). Safe to fold into the span here because compute_layout is
    # READ-ONLY -- the old infinite-growth trap was the reactive auto-expand
    # effect inserting tracks in response to this, which no longer exists
    # (growth is imperative on the backend now).
    max_output_offset: dict = {}
    for n in nodes:
        if n.created_by_node_id is None:
            continue
        creator = by_id.get(n.created_by_node_id)
        if creator is None:
            continue
        cp = pos.get(creator.track_id)
        op = pos.get(n.track_id)
        if cp is None or op is None:
            continue
        offset = op - cp
        if offset > max_output_offset.get(creator.id, 0):
            max_output_offset[creator.id] = offset

    spans: dict[str, dict] = {}
    for n in nodes:
        if n.kind != NodeKind.workflow:
            continue
        effective = await resolve_effective_template(db, n)
        fields = (effective.param_schema if effective else {}).get("fields", [])
        slot_count = len([f for f in fields if f.get("type") in ("image", "file")])
        spawned = await db.execute(select(func.count()).select_from(Track).where(Track.spawned_from_node_id == n.id))
        spawned_count = spawned.scalar_one()
        desired = max(slot_count, 1 + spawned_count, max_output_offset.get(n.id, 0) + 1, 1)

        # Mirrors the OLD frontend rowSpanByNode exactly: grow toward desired,
        # stopping only at the first row below whose own column is already
        # occupied by an unrelated node -- NOT capped at the current track
        # count (it may grow into not-yet-created rows; the auto-expand effect
        # then materializes them). Deliberately different from the move/split
        # helpers' _actual_span, which DO cap at the track count.
        start = pos.get(n.track_id)
        achieved = 1
        if start is not None:
            while achieved < desired and (start + achieved, n.step_index) not in occupied:
                achieved += 1
        spans[str(n.id)] = {"desired": desired, "achieved": achieved}

    blocked_cells: list[list[int]] = []
    for n in nodes:
        if n.kind != NodeKind.workflow:
            continue
        start = pos.get(n.track_id)
        if start is None:
            continue
        achieved = spans[str(n.id)]["achieved"]
        for r in range(start + 1, start + achieved):
            blocked_cells.append([r, n.step_index])

    return {"spans": spans, "blocked_cells": blocked_cells}
