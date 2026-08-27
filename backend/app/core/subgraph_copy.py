"""Copying one grid scope into a fresh one -- "copy this subgraph".

The point is to reuse the *shape* of a chart (its tracks, its columns, its
configured workflow nodes) as the starting point for a variant of it, without
producing a single new byte on disk. So the copy is split three ways, by what
the thing being copied actually is:

  * **structure** (tracks, their order, step indices) -- copied. Slot inputs
    are positional (`cell_index`: "whatever asset node sits at my row offset,
    one column back" -- see worker/tasks.py's resolve_node_inputs), so the
    relative order of tracks and the step indices ARE the wiring. Preserving
    them is what makes the copy connected; there is no id remapping to do.
  * **settings** (a workflow node's template, params, slot refs, variants,
    backend, use_api) -- copied, genuinely and independently, by
    workflow_node_copy below. Two cells can share one picture but never one
    set of parameters (CLAUDE.md).
  * **content** (pictures) -- never copied. An asset cell that owns a picture
    comes across as a reference to that same picture, a smart pointer as a
    second pointer at the same sub-dashboard. Each kind answers for itself,
    via AssetNodeBackend.copy_spec (core/asset_types.py).

And one thing is left out entirely: **a workflow's own materialized output**
(an asset node with `created_by_node_id` set) is not reproduced at all, in any
form. It leaves a hole -- a genuinely empty cell -- which is what the user
asked for: the copy exists to be re-generated or re-wired from that point on,
left to right, and a picture sitting in the output slot would be something to
clear away first rather than a head start. Because slot resolution is
positional and tolerates an empty cell (it resolves to "no image", loudly, at
run time), a hole costs nothing structurally: generate into it later and the
node that reads it picks the result up with no rewiring.

Nothing recurses. Copying a grid that points at other grids produces pointers
at those same grids, not copies of them.
"""

import copy
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.asset_types import resolve_asset_node
from app.core.track_order import ordered_tracks, splice_after
from app.db.models import Dashboard, Node, NodeKind, NodeStatus, Track


def workflow_node_copy(old: Node, *, track_id, step_index: int) -> Node:
    """A second, independent workflow node carrying every one of `old`'s local
    settings and none of its results: status starts at draft with no jobs and
    no outputs, and created_by_node_id / collapse_target_id stay NULL -- the
    copy is nobody's materialized output and isn't part of any collapsed chain.

    Shared by the single-cell "⧉" gesture (nodes.py's duplicate_node) and the
    whole-grid copy below, so the two can't drift about what "every local
    setting" means.

    inputs/params are deep-copied, not handed over as the same list/dict
    object: they're JSON columns held as plain Python structures, so sharing
    the object would make an in-place edit of one node's params (updateParam in
    NodeCell.tsx builds a new dict, but nothing guarantees every writer does)
    surface on the other too.
    """
    return Node(
        track_id=track_id,
        step_index=step_index,
        kind=NodeKind.workflow,
        node_type=old.node_type,
        inputs=copy.deepcopy(old.inputs),
        params=copy.deepcopy(old.params),
        requested_variants=old.requested_variants,
        backend_mode=old.backend_mode,
        manual_backend_id=old.manual_backend_id,
        use_api=old.use_api,
    )


async def copy_dashboard_contents(db: AsyncSession, source: Dashboard, dest: Dashboard) -> None:
    """Fill the (empty, freshly created) `dest` dashboard with a copy of
    `source`'s grid, per the rules in this module's docstring.

    `dest` must already carry `source`'s start_kind: column parity is per
    scope, and step indices are copied verbatim, so a differing origin would
    land every workflow node in an asset column. The caller sets it when it
    creates the dashboard rather than this function patching it, since an
    origin is written once and never revised (core/grid_scope.py).

    Does not commit -- the caller does.
    """
    src_tracks = await ordered_tracks(db, source.project_id, source.id)
    if not src_tracks:
        return

    # Same order, same scope, appended one by one -- "row N of the copy" has to
    # mean the same thing as "row N of the original" for positional slot refs
    # to keep pointing where they did.
    track_map: dict[uuid.UUID, Track] = {}
    anchor: Track | None = None
    for src_track in src_tracks:
        new_track = Track(project_id=dest.project_id, dashboard_id=dest.id)
        db.add(new_track)
        await db.flush()
        await splice_after(db, dest.project_id, new_track, anchor, at_head=anchor is None, dashboard_id=dest.id)
        await db.flush()
        track_map[src_track.id] = new_track
        anchor = new_track

    result = await db.execute(
        select(Node).where(
            Node.track_id.in_([t.id for t in src_tracks]),
            Node.status != NodeStatus.discarded,
        )
    )
    node_map: dict[uuid.UUID, Node] = {}
    for src_node in result.scalars().all():
        target_track = track_map.get(src_node.track_id)
        if target_track is None:
            continue
        if src_node.kind == NodeKind.workflow:
            new_node = workflow_node_copy(src_node, track_id=target_track.id, step_index=src_node.step_index)
        else:
            # A workflow's own result: left as a hole, deliberately (see module
            # docstring). created_by_node_id is written in exactly one place
            # (worker/tasks.py materializing an output), so it is precisely the
            # "this cell is a generated result" flag -- nothing else sets it.
            if src_node.created_by_node_id is not None:
                continue
            backend = resolve_asset_node(src_node)
            assert backend is not None  # kind == asset
            new_node = Node(
                track_id=target_track.id,
                step_index=src_node.step_index,
                kind=NodeKind.asset,
                params=copy.deepcopy(src_node.params),
                **await backend.copy_spec(db, src_node),
            )
        db.add(new_node)
        node_map[src_node.id] = new_node
    await db.flush()

    # Provenance for the "branched off there" arrow, and a term in the
    # row-span formula (_actual_span counts a workflow's spawned tracks), so it
    # has to follow the copy or a copied multi-output workflow renders shorter
    # than the original. Both ends are inside the copy or it's dropped -- the
    # same rule tracks.py applies when a move would split the two apart.
    for src_track in src_tracks:
        origin = node_map.get(src_track.spawned_from_node_id) if src_track.spawned_from_node_id else None
        if origin is not None:
            track_map[src_track.id].spawned_from_node_id = origin.id
    await db.flush()
