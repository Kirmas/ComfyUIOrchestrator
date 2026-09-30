"""Design docs (models.DesignDoc): markdown, a Ukrainian and an English
version each, owned either by a project (its design doc) or by a folder (a
*global* doc -- a world's lore, not any one project's). Ownership moves
between the two, it is never shared: attaching a global doc to a project takes
it out of its folder, making a project's doc global detaches it.

A doc points at the rest of the work by scheme rather than by URL, in
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
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes.assets import to_asset_read
from app.core.asset_types import resolve_asset_node
from app.db.base import get_db
from app.db.models import Asset, BoardItem, Dashboard, DesignDoc, DesignDocText, Node, Project, ProjectCategory
from app.schemas.schemas import (
    DesignDocAttach,
    DesignDocCreate,
    DesignDocMakeGlobal,
    DesignDocMetaUpdate,
    DesignDocNewProject,
    DesignDocRead,
    DesignDocRef,
    DesignDocRefsRequest,
    DesignDocSummary,
    DesignDocUpdate,
    ProjectRead,
)

# A project's own doc is addressed through its project; a global one by its id.
project_router = APIRouter(prefix="/api/projects", tags=["design-docs"])
router = APIRouter(prefix="/api/design-docs", tags=["design-docs"])

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


async def _doc_or_404(db: AsyncSession, doc_id: uuid.UUID) -> DesignDoc:
    doc = await db.get(DesignDoc, doc_id)
    if not doc:
        raise HTTPException(404, "Design doc not found")
    return doc


async def _global_doc_or_404(db: AsyncSession, doc_id: uuid.UUID) -> DesignDoc:
    doc = await _doc_or_404(db, doc_id)
    if doc.project_id is not None:
        raise HTTPException(409, "That's a project's design doc, not a global one")
    return doc


async def _ensure_category(db: AsyncSession, category_id: uuid.UUID | None) -> None:
    if category_id is not None and not await db.get(ProjectCategory, category_id):
        raise HTTPException(404, "Folder not found")


def _clean_title(title: str) -> str:
    if not title.strip():
        raise HTTPException(422, "A design doc needs a title")
    return title.strip()


async def project_doc(db: AsyncSession, project_id: uuid.UUID) -> DesignDoc | None:
    return await db.scalar(select(DesignDoc).where(DesignDoc.project_id == project_id))


async def doc_is_empty(db: AsyncSession, doc_id: uuid.UUID) -> bool:
    """No text in any language -- what a project that merely opened its doc tab
    once is left with. Such a doc doesn't count as "having a design doc"."""
    return not await db.scalar(
        select(DesignDocText.doc_id).where(DesignDocText.doc_id == doc_id, func.length(func.trim(DesignDocText.content)) > 0).limit(1)
    )


async def _read(db: AsyncSession, doc: DesignDoc | None, lang: str) -> DesignDocRead:
    text = await db.get(DesignDocText, (doc.id, lang)) if doc else None
    content = text.content if text else ""
    return DesignDocRead(
        doc_id=doc.id if doc else None,
        lang=lang,
        content=content,
        updated_at=text.updated_at if text else None,
        refs=await _resolve_refs(db, REF_PATTERN.findall(content)),
    )


async def _write(db: AsyncSession, doc: DesignDoc, lang: str, content: str) -> DesignDocRead:
    text = await db.get(DesignDocText, (doc.id, lang))
    if text is None:
        db.add(DesignDocText(doc_id=doc.id, lang=lang, content=content))
    else:
        text.content = content
        # Set explicitly: onupdate only fires when SQLAlchemy sees a change, and
        # a save of identical text should still read as "saved just now".
        text.updated_at = datetime.now(timezone.utc)
    await db.commit()
    return await _read(db, doc, lang)


# ---------- a project's own doc ----------
@project_router.get("/{project_id}/design-doc", response_model=DesignDocRead)
async def get_project_design_doc(project_id: uuid.UUID, lang: Lang = "uk", db: AsyncSession = Depends(get_db)):
    await _project_or_404(db, project_id)
    return await _read(db, await project_doc(db, project_id), lang)


@project_router.put("/{project_id}/design-doc", response_model=DesignDocRead)
async def save_project_design_doc(
    project_id: uuid.UUID, payload: DesignDocUpdate, lang: Lang = "uk", db: AsyncSession = Depends(get_db)
):
    await _project_or_404(db, project_id)
    doc = await project_doc(db, project_id)
    if doc is None:
        doc = DesignDoc(project_id=project_id)
        db.add(doc)
        await db.flush()
    return await _write(db, doc, lang, payload.content)


@project_router.post("/{project_id}/design-doc/make-global", response_model=DesignDocSummary)
async def make_project_design_doc_global(
    project_id: uuid.UUID, payload: DesignDocMakeGlobal, db: AsyncSession = Depends(get_db)
):
    """Detaches the project's doc into a folder -- the project's own folder
    unless told otherwise -- titled after the project. The project is left
    with no doc, and can take another."""
    project = await _project_or_404(db, project_id)
    doc = await project_doc(db, project_id)
    if doc is None or await doc_is_empty(db, doc.id):
        raise HTTPException(409, "This project has no design doc to make global")
    category_id = payload.category_id if "category_id" in payload.model_fields_set else project.category_id
    await _ensure_category(db, category_id)
    doc.project_id = None
    doc.category_id = category_id
    doc.title = _clean_title(payload.title) if payload.title is not None else project.name
    await db.commit()
    await db.refresh(doc)
    return doc


# ---------- global docs ----------
@router.get("", response_model=list[DesignDocSummary])
async def list_global_design_docs(db: AsyncSession = Depends(get_db)):
    """Every global doc, whatever folder -- the projects page filters by the
    folder it's showing, same as it does for projects."""
    return (await db.scalars(select(DesignDoc).where(DesignDoc.project_id.is_(None)).order_by(DesignDoc.title))).all()


@router.post("", response_model=DesignDocSummary, status_code=201)
async def create_global_design_doc(payload: DesignDocCreate, db: AsyncSession = Depends(get_db)):
    await _ensure_category(db, payload.category_id)
    doc = DesignDoc(title=_clean_title(payload.title), category_id=payload.category_id)
    db.add(doc)
    await db.commit()
    await db.refresh(doc)
    return doc


@router.post("/refs", response_model=dict[str, DesignDocRef])
async def resolve_design_doc_refs(payload: DesignDocRefsRequest, db: AsyncSession = Depends(get_db)):
    """For the editor's live preview: references typed or inserted since the
    last save, resolved the same way a saved doc's are."""
    return await _resolve_refs(db, payload.refs[:200])


@router.get("/{doc_id}", response_model=DesignDocSummary)
async def get_design_doc_summary(doc_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    return await _doc_or_404(db, doc_id)


@router.patch("/{doc_id}", response_model=DesignDocSummary)
async def update_global_design_doc(doc_id: uuid.UUID, payload: DesignDocMetaUpdate, db: AsyncSession = Depends(get_db)):
    doc = await _global_doc_or_404(db, doc_id)
    if payload.title is not None:
        doc.title = _clean_title(payload.title)
    if "category_id" in payload.model_fields_set:
        await _ensure_category(db, payload.category_id)
        doc.category_id = payload.category_id
    await db.commit()
    await db.refresh(doc)
    return doc


@router.delete("/{doc_id}", status_code=204)
async def delete_global_design_doc(doc_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Global docs only. A project's doc goes with its project, or is made
    global first -- nothing here deletes a project's doc out from under it."""
    doc = await _global_doc_or_404(db, doc_id)
    await db.delete(doc)
    await db.commit()


@router.get("/{doc_id}/text", response_model=DesignDocRead)
async def get_design_doc_text(doc_id: uuid.UUID, lang: Lang = "uk", db: AsyncSession = Depends(get_db)):
    return await _read(db, await _doc_or_404(db, doc_id), lang)


@router.put("/{doc_id}/text", response_model=DesignDocRead)
async def save_design_doc_text(
    doc_id: uuid.UUID, payload: DesignDocUpdate, lang: Lang = "uk", db: AsyncSession = Depends(get_db)
):
    return await _write(db, await _doc_or_404(db, doc_id), lang, payload.content)


async def _attach(db: AsyncSession, doc: DesignDoc, project: Project) -> None:
    """Moves a global doc into a project. A project that already has a doc
    with text refuses; an empty one (a tab opened once) is simply replaced."""
    existing = await project_doc(db, project.id)
    if existing is not None:
        if not await doc_is_empty(db, existing.id):
            raise HTTPException(409, f"Project '{project.name}' already has its own design doc")
        await db.delete(existing)
        await db.flush()
    doc.project_id = project.id
    doc.category_id = None


@router.post("/{doc_id}/attach", response_model=DesignDocSummary)
async def attach_design_doc(doc_id: uuid.UUID, payload: DesignDocAttach, db: AsyncSession = Depends(get_db)):
    doc = await _global_doc_or_404(db, doc_id)
    await _attach(db, doc, await _project_or_404(db, payload.project_id))
    await db.commit()
    await db.refresh(doc)
    return doc


@router.post("/{doc_id}/create-project", response_model=ProjectRead, status_code=201)
async def create_project_from_design_doc(
    doc_id: uuid.UUID, payload: DesignDocNewProject, db: AsyncSession = Depends(get_db)
):
    """A new project in the doc's folder, named after it, with the doc as its
    design doc."""
    doc = await _global_doc_or_404(db, doc_id)
    name = (payload.name or doc.title).strip()
    if not name:
        raise HTTPException(422, "Project name can't be empty")
    project = Project(name=name, category_id=doc.category_id)
    db.add(project)
    await db.flush()
    await _attach(db, doc, project)
    await db.commit()
    await db.refresh(project)
    return project
