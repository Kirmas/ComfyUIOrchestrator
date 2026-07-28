"""Comment blocks: free text attached to a set of nodes, drawn as a frame.

The frame's geometry is not stored (see db/models.py's Annotation docstring) --
these routes only ever move membership and text around, and the client derives
the box from where the member nodes currently are.
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import get_db
from app.db.models import Annotation, AnnotationNode, Node, Project, Track
from app.schemas.schemas import AnnotationCreate, AnnotationRead, AnnotationUpdate

router = APIRouter(prefix="/api/annotations", tags=["annotations"])


def _read(annotation: Annotation) -> AnnotationRead:
    return AnnotationRead(
        id=annotation.id,
        project_id=annotation.project_id,
        text=annotation.text,
        source=annotation.source,
        node_ids=[m.node_id for m in annotation.members],
        created_at=annotation.created_at,
        updated_at=annotation.updated_at,
    )


async def _validate_members(db: AsyncSession, project_id: uuid.UUID, node_ids: list[uuid.UUID]) -> list[uuid.UUID]:
    """Members must be distinct and all live in the annotation's own project --
    a frame spanning two projects has no meaning, and silently accepting a
    foreign node id would leave a member that never renders anywhere."""
    unique = list(dict.fromkeys(node_ids))
    if not unique:
        return []
    result = await db.execute(
        select(Node.id).join(Track, Track.id == Node.track_id).where(Node.id.in_(unique), Track.project_id == project_id)
    )
    found = set(result.scalars().all())
    missing = [str(n) for n in unique if n not in found]
    if missing:
        raise HTTPException(400, f"Nodes not found in this project: {', '.join(missing)}")
    return unique


@router.post("", response_model=AnnotationRead, status_code=201)
async def create_annotation(payload: AnnotationCreate, db: AsyncSession = Depends(get_db)):
    project = await db.get(Project, payload.project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    node_ids = await _validate_members(db, payload.project_id, payload.node_ids)

    annotation = Annotation(project_id=payload.project_id, text=payload.text, source=payload.source)
    db.add(annotation)
    await db.flush()
    for node_id in node_ids:
        db.add(AnnotationNode(annotation_id=annotation.id, node_id=node_id))
    await db.commit()

    result = await db.execute(
        select(Annotation).options(selectinload(Annotation.members)).where(Annotation.id == annotation.id)
    )
    return _read(result.scalar_one())


@router.patch("/{annotation_id}", response_model=AnnotationRead)
async def update_annotation(annotation_id: uuid.UUID, payload: AnnotationUpdate, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Annotation).options(selectinload(Annotation.members)).where(Annotation.id == annotation_id)
    )
    annotation = result.scalar_one_or_none()
    if not annotation:
        raise HTTPException(404, "Annotation not found")

    if payload.text is not None:
        annotation.text = payload.text
    if payload.node_ids is not None:
        node_ids = await _validate_members(db, annotation.project_id, payload.node_ids)
        annotation.members.clear()
        await db.flush()
        for node_id in node_ids:
            db.add(AnnotationNode(annotation_id=annotation.id, node_id=node_id))
    await db.commit()

    result = await db.execute(
        select(Annotation).options(selectinload(Annotation.members)).where(Annotation.id == annotation_id)
    )
    return _read(result.scalar_one())


@router.delete("/{annotation_id}", status_code=204)
async def delete_annotation(annotation_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    annotation = await db.get(Annotation, annotation_id)
    if not annotation:
        raise HTTPException(404, "Annotation not found")
    await db.delete(annotation)
    await db.commit()
