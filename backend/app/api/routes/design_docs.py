"""A project's design doc: one markdown text per project and language
(models.DesignDoc) -- a Ukrainian and an English version.

The doc points at the rest of the project by scheme rather than by URL, in
ordinary markdown link/image syntax -- `![](node:<id>)` embeds a grid cell,
`[see](board:<id>)` links a sticker, `dashboard:<id>` follows a
sub-dashboard's chosen result, `asset:<id>` pins one fixed file. They're
resolved here, on every read, so a cell referenced by the doc shows whatever
picture it stands for today (its *face*, core/asset_types.py -- the same answer
the grid and compare use), not a copy frozen when the link was written.
"""
import re
import uuid
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes.assets import to_asset_read
from app.core.asset_types import resolve_asset_node
from app.db.base import get_db
from app.db.models import Asset, BoardItem, Dashboard, DesignDoc, Node, Project
from app.schemas.schemas import DesignDocRead, DesignDocRef, DesignDocRefsRequest, DesignDocUpdate

router = APIRouter(prefix="/api/projects", tags=["design-docs"])

# The languages a doc exists in. Mirrored by DocLang in DesignDoc.tsx.
Lang = Literal["uk", "en"]

# Mirrors the scheme list in frontend/src/markdown.ts -- a link the renderer
# doesn't recognise as a reference is never sent here to be resolved.
REF_PATTERN = re.compile(r"\]\(((?:node|board|asset|dashboard):[0-9a-fA-F-]{36})\)")


async def _resolve_ref(db: AsyncSession, ref: str) -> DesignDocRef:
    """Resolved wherever the target lives, not only inside the doc's own
    project: the copy clipboard (clipboard.ts) crosses projects, and a doc
    quoting a cell from a sibling project is a legitimate thing to write --
    restricting it only turned such links into broken ones."""
    scheme, _, raw_id = ref.partition(":")
    try:
        target_id = uuid.UUID(raw_id)
    except ValueError:
        return DesignDocRef(missing=True)

    if scheme == "node":
        node = await db.get(Node, target_id)
        kind = resolve_asset_node(node) if node is not None else None
        if kind is None:
            return DesignDocRef(missing=True)
        face = await kind.face(db, node)
        return DesignDocRef(label=node.node_type, asset=to_asset_read(face) if face else None)

    if scheme == "board":
        item = await db.get(BoardItem, target_id)
        if item is None:
            return DesignDocRef(missing=True)
        asset = await db.get(Asset, item.asset_id) if item.asset_id else None
        first_line = next((line.strip("# ").strip() for line in item.text.splitlines() if line.strip()), "")
        return DesignDocRef(
            label=item.tag or first_line[:80] or item.kind,
            asset=to_asset_read(asset) if asset else None,
            text=item.text if item.text else None,
        )

    if scheme == "dashboard":
        # A sub-dashboard, not any one pointer cell at it: shows whatever the
        # dashboard currently has as its result, so choosing a new result
        # inside it updates the doc, and deleting one pointer doesn't break it.
        dashboard = await db.get(Dashboard, target_id)
        if dashboard is None:
            return DesignDocRef(missing=True)
        face = await db.get(Asset, dashboard.result_asset_id) if dashboard.result_asset_id else None
        return DesignDocRef(label=dashboard.name or None, asset=to_asset_read(face) if face else None)

    if scheme == "asset":
        asset = await db.get(Asset, target_id)
        if asset is None:
            return DesignDocRef(missing=True)
        return DesignDocRef(label=asset.kind, asset=to_asset_read(asset))

    return DesignDocRef(missing=True)


async def _resolve_refs(db: AsyncSession, refs: list[str]) -> dict[str, DesignDocRef]:
    return {ref: await _resolve_ref(db, ref) for ref in dict.fromkeys(refs)}


async def _project_or_404(db: AsyncSession, project_id: uuid.UUID) -> Project:
    project = await db.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    return project


@router.get("/{project_id}/design-doc", response_model=DesignDocRead)
async def get_design_doc(project_id: uuid.UUID, lang: Lang = "uk", db: AsyncSession = Depends(get_db)):
    await _project_or_404(db, project_id)
    doc = await db.get(DesignDoc, (project_id, lang))
    content = doc.content if doc else ""
    return DesignDocRead(
        lang=lang,
        content=content,
        updated_at=doc.updated_at if doc else None,
        refs=await _resolve_refs(db, REF_PATTERN.findall(content)),
    )


@router.put("/{project_id}/design-doc", response_model=DesignDocRead)
async def save_design_doc(
    project_id: uuid.UUID, payload: DesignDocUpdate, lang: Lang = "uk", db: AsyncSession = Depends(get_db)
):
    await _project_or_404(db, project_id)
    doc = await db.get(DesignDoc, (project_id, lang))
    if doc is None:
        doc = DesignDoc(project_id=project_id, lang=lang, content=payload.content)
        db.add(doc)
    else:
        doc.content = payload.content
        # Set explicitly: onupdate only fires when SQLAlchemy sees a change, and
        # a save of identical text should still read as "saved just now".
        doc.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(doc)
    return DesignDocRead(
        lang=lang,
        content=doc.content,
        updated_at=doc.updated_at,
        refs=await _resolve_refs(db, REF_PATTERN.findall(doc.content)),
    )


@router.post("/{project_id}/design-doc/refs", response_model=dict[str, DesignDocRef])
async def resolve_design_doc_refs(
    project_id: uuid.UUID, payload: DesignDocRefsRequest, db: AsyncSession = Depends(get_db)
):
    """For the editor's live preview: references typed or inserted since the
    last save, resolved the same way a saved doc's are."""
    await _project_or_404(db, project_id)
    return await _resolve_refs(db, payload.refs[:200])
