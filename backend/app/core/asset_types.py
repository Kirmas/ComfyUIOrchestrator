"""Asset-kind node behavior, one class per `asset.*` node_type.

The workflow half of the grid already resolves its node_type through a code
registry (`node_types.py`'s NATIVE_NODE_TYPES -> a NativeBackend subclass).
The asset half didn't: every question about an asset cell was answered by an
`if node.node_type == "asset.refasset" / "asset.subgraph"` written out again
at each call site (worker/tasks.py, api/routes/nodes.py, core/storage_gc.py,
plus their frontend mirrors). Those chains drifted -- `list_node_outputs`
grew a refasset branch but never a subgraph one, so a smart pointer reported
zero outputs and silently couldn't be compared -- which is the failure mode
this module exists to prevent.

The four kinds differ in exactly three things, and this is the whole interface:

  * **face** -- which single Asset the cell stands for. Only `asset.single`
    and `asset.select` own `Asset` rows (`Asset.node_id`); `asset.refasset`
    borrows one through an "explicit" InputRef and `asset.subgraph` borrows
    whichever asset its sub-dashboard was given as its result. Everything
    position-based (slot resolution, compare, zoom, thumbnails) wants the
    face, never the raw `Asset.node_id` query.
  * **whether it owns those rows at all** (`owns_assets`) -- which is what
    deletion, storage GC and "list this node's outputs" actually key on.
  * **how the cell is reproduced in a copy of its grid** (`copy_spec`, see
    core/subgraph_copy.py). Content is never duplicated: a cell that owns a
    picture comes across as a *reference* to that same picture, and a smart
    pointer as a second pointer at the same sub-dashboard. This is the asset
    half of "copying vs referencing is split by kind" (CLAUDE.md) -- the
    workflow half is a genuine independent copy of the settings.

Deliberately NOT a plugin framework: it's a closed four-element set that only
grows when someone writes a new asset node type by hand, same as
NATIVE_NODE_TYPES. Mirrored on the frontend by `src/assetNodes.ts` -- the two
are separate implementations of the same rules on purpose (see CLAUDE.md);
change both together.
"""
from __future__ import annotations

from typing import ClassVar

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Asset, Dashboard, Node, NodeKind, NodeStatus


async def explicit_ref_asset(db: AsyncSession, ref: dict) -> Asset | None:
    """Resolves an "explicit" InputRef to its Asset -- by asset id alone, never
    through the node it may also name. A board-library reference carries no
    `node_id` at all (see api/routes/boards.py), and a grid one's `node_id` is
    only there so Grid.tsx can draw the arrow back to the source cell."""
    asset_id = ref.get("asset_id") or ref.get("output_id")
    return await db.get(Asset, asset_id) if asset_id else None


class AssetNodeBackend:
    """Base behavior: a cell that owns its own Asset rows."""

    node_type: ClassVar[str]
    # True if Asset rows hang off this node (Asset.node_id). False for the
    # pointer kinds, which borrow someone else's -- so deleting one must
    # never touch storage, and its "outputs" can't come from a node_id query.
    owns_assets: ClassVar[bool] = True
    # The undecided-candidates picker: several variants, none chosen yet.
    is_picker: ClassVar[bool] = False
    # True if this kind points at an asset through an "explicit" InputRef, so
    # storage GC has to treat that asset as still referenced.
    references_by_input: ClassVar[bool] = False

    async def outputs(self, db: AsyncSession, node: Node) -> list[Asset]:
        result = await db.execute(select(Asset).where(Asset.node_id == node.id).order_by(Asset.created_at))
        return list(result.scalars().all())

    async def face(self, db: AsyncSession, node: Node) -> Asset | None:
        """The one asset this cell stands for: the flagged-selected candidate
        if there is one, else the most recently created."""
        assets = await self.outputs(db, node)
        if not assets:
            return None
        selected = [a for a in assets if a.selected]
        return selected[0] if selected else assets[-1]

    async def copy_spec(self, db: AsyncSession, node: Node) -> dict:
        """The fields this cell's counterpart carries in a copy of the grid it
        lives in (core/subgraph_copy.py builds the Node from them).

        A cell that owns a picture is reproduced as a *reference* to that same
        picture, never as a second copy of the file -- the same rule as "+ ref
        elsewhere", applied in bulk. An empty cell stays an empty cell of its
        own kind. No `node_id` goes into the ref (unlike Grid.tsx's own
        "+ ref elsewhere", which adds one for the provenance arrow): the source
        cell is in a different grid, where no arrow can be drawn to it, so this
        is shaped like a board-library reference instead -- resolved by asset
        id alone on both ends (explicit_ref_asset above, resolveSlotAsset in
        slotResolution.ts)."""
        face = await self.face(db, node)
        if face is None:
            return {"node_type": node.node_type, "inputs": [], "status": NodeStatus.draft}
        return {
            "node_type": RefAssetNode.node_type,
            "inputs": [{"type": "explicit", "output_id": str(face.id)}],
            "status": NodeStatus.done,
        }


class _BorrowedFaceAssetNode(AssetNodeBackend):
    """Shared by the two pointer kinds: no Asset row of its own, so its
    "outputs" are just its borrowed face, and there is nothing to delete."""

    owns_assets = False

    async def outputs(self, db: AsyncSession, node: Node) -> list[Asset]:
        face = await self.face(db, node)
        return [face] if face is not None else []


class SingleAssetNode(AssetNodeBackend):
    """A settled asset with exactly one resolved output."""

    node_type = "asset.single"


class SelectAssetNode(AssetNodeBackend):
    """An undecided candidates picker -- several generated variants, none
    chosen yet. Has no single "the" picture, which is why the UI refuses it
    as a compare/pick target (see assetNodes.ts's `pickable`)."""

    node_type = "asset.select"
    is_picker = True


class RefAssetNode(_BorrowedFaceAssetNode):
    """A lightweight pointer to another node's (or the board library's) asset,
    made via "+ ref elsewhere" -- the way one picture sits in more than one
    grid cell without the file being duplicated."""

    node_type = "asset.refasset"
    references_by_input = True

    async def face(self, db: AsyncSession, node: Node) -> Asset | None:
        return await explicit_ref_asset(db, (node.inputs or [{}])[0])


class SubgraphAssetNode(_BorrowedFaceAssetNode):
    """A smart pointer: an asset cell that also opens a sub-dashboard. Its
    face is whichever asset inside that dashboard was chosen to represent it
    (Dashboard.result_asset_id), not anything hung off the pointer node."""

    node_type = "asset.subgraph"

    async def face(self, db: AsyncSession, node: Node) -> Asset | None:
        if node.subgraph_dashboard_id is None:
            return None
        dashboard = await db.get(Dashboard, node.subgraph_dashboard_id)
        if dashboard is None or dashboard.result_asset_id is None:
            return None
        return await db.get(Asset, dashboard.result_asset_id)

    async def copy_spec(self, db: AsyncSession, node: Node) -> dict:
        """A second pointer at the *same* sub-dashboard -- copying a grid never
        recurses into the grids it points at. The copy is an extra pointer, not
        an owner (a non-tree edge, hence always safe to delete), so the nested
        dashboard keeps the owner it already had."""
        return {
            "node_type": self.node_type,
            "inputs": [],
            "subgraph_dashboard_id": node.subgraph_dashboard_id,
            "status": node.status,
        }


# Stateless singletons -- these carry no per-node state, so there's nothing to
# instantiate per call (unlike NativeBackend, which is handed a job's params).
ASSET_NODE_TYPES: dict[str, AssetNodeBackend] = {
    cls.node_type: cls() for cls in (SingleAssetNode, SelectAssetNode, RefAssetNode, SubgraphAssetNode)
}

SUBGRAPH_NODE_TYPE = SubgraphAssetNode.node_type

# node_type strings whose nodes point at an asset through an explicit InputRef
# -- storage GC needs this as a SQL filter, not as a per-node dispatch.
REFERENCING_ASSET_NODE_TYPES: list[str] = [t for t, b in ASSET_NODE_TYPES.items() if b.references_by_input]

_DEFAULT_ASSET_NODE = ASSET_NODE_TYPES[SingleAssetNode.node_type]


def resolve_asset_node(node: Node) -> AssetNodeBackend | None:
    """The asset-kind counterpart of resolve_effective_template: None for a
    workflow node, else the behavior for this cell. An asset node whose
    node_type is unset or unrecognized falls back to the plain owns-its-own-
    assets behavior, which is what every call site did implicitly before."""
    if node.kind != NodeKind.asset:
        return None
    return ASSET_NODE_TYPES.get(node.node_type or "", _DEFAULT_ASSET_NODE)


def is_picker_type(node_type: str | None) -> bool:
    backend = ASSET_NODE_TYPES.get(node_type or "")
    return backend is not None and backend.is_picker
