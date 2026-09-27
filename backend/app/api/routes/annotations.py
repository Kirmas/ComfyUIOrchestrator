"""Comment threads: a conversation attached to a set of nodes, drawn as a frame.

The frame's geometry is not stored (see db/models.py's Annotation docstring) --
these routes only ever move membership, messages and the open/resolved state
around, and the client derives the box from where the member nodes currently
are.

Every write carries `source` -- who is writing, "user" from the UI or "agent"
from the MCP tools -- and every write is pushed to the project's websocket as
{"type": "annotations"}, so a reply made through MCP shows up in an open grid
without a reload.
"""
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.ws_manager import ws_manager
from app.db.base import get_db
from app.db.models import Annotation, AnnotationMessage, AnnotationNode, AnnotationSource, Node, Project, Track
from app.schemas.schemas import (
    AnnotationCreate,
    AnnotationMessageCreate,
    AnnotationMessageRead,
    AnnotationMessageUpdate,
    AnnotationRead,
    AnnotationUpdate,
)

router = APIRouter(prefix="/api", tags=["annotations"])

# What every read needs loaded; the one shape both this module and the
# project-level list in projects.py return.
LOAD_THREAD = (selectinload(Annotation.members), selectinload(Annotation.messages))


def annotation_read(annotation: Annotation) -> AnnotationRead:
    return AnnotationRead(
        id=annotation.id,
        project_id=annotation.project_id,
        node_ids=[m.node_id for m in annotation.members],
        resolved=annotation.resolved_at is not None,
        resolved_at=annotation.resolved_at,
        resolved_by=annotation.resolved_by,
        messages=[AnnotationMessageRead.model_validate(m) for m in annotation.messages],
        created_at=annotation.created_at,
        updated_at=annotation.updated_at,
    )


async def _load(db: AsyncSession, annotation_id: uuid.UUID) -> Annotation:
    # populate_existing: a thread this same session just changed must come
    # back with the new message list, not the identity-map copy from before.
    result = await db.execute(
        select(Annotation).options(*LOAD_THREAD).where(Annotation.id == annotation_id).execution_options(populate_existing=True)
    )
    annotation = result.scalar_one_or_none()
    if annotation is None:
        raise HTTPException(404, "Annotation not found")
    return annotation


async def _notify(project_id: uuid.UUID) -> None:
    await ws_manager.broadcast(str(project_id), {"type": "annotations"})


def _text(value: str) -> str:
    text = value.strip()
    if not text:
        raise HTTPException(400, "A comment needs some text")
    return text


def _append(annotation: Annotation, text: str, source: AnnotationSource) -> None:
    """Add a message. Saying something in a resolved thread reopens it -- a
    reply is by definition something that still needs reading."""
    now = datetime.now(UTC)
    annotation.messages.append(AnnotationMessage(source=source, text=text, created_at=now, updated_at=now))
    annotation.resolved_at = None
    annotation.resolved_by = None


async def _validate_members(db: AsyncSession, project_id: uuid.UUID, node_ids: list[uuid.UUID]) -> list[uuid.UUID]:
    """Members must be distinct, non-empty and all live in the annotation's own
    project -- a frame spanning two projects has no meaning, a frame around
    nothing never renders anywhere, and silently accepting a foreign node id
    would leave a member that never renders either."""
    unique = list(dict.fromkeys(node_ids))
    if not unique:
        raise HTTPException(400, "A comment needs at least one cell")
    result = await db.execute(
        select(Node.id).join(Track, Track.id == Node.track_id).where(Node.id.in_(unique), Track.project_id == project_id)
    )
    found = set(result.scalars().all())
    missing = [str(n) for n in unique if n not in found]
    if missing:
        raise HTTPException(400, f"Nodes not found in this project: {', '.join(missing)}")
    return unique


async def _thread_on(db: AsyncSession, project_id: uuid.UUID, node_ids: list[uuid.UUID]) -> Annotation | None:
    """The thread these exact cells already have, if any. One conversation per
    member set: a second frame on the same cells stacks its label right on top
    of the first one's, and splits one conversation in two. Should deleting
    nodes ever leave two threads on the same set, the most recently active one
    carries on."""
    result = await db.execute(
        select(Annotation)
        .options(*LOAD_THREAD)
        .join(AnnotationNode, AnnotationNode.annotation_id == Annotation.id)
        .where(Annotation.project_id == project_id, AnnotationNode.node_id.in_(node_ids))
        .distinct()
    )
    wanted = set(node_ids)
    same = [a for a in result.scalars().all() if {m.node_id for m in a.members} == wanted]
    return max(same, key=lambda a: a.messages[-1].created_at if a.messages else a.created_at, default=None)


@router.post("/annotations", response_model=AnnotationRead, status_code=201)
async def create_annotation(payload: AnnotationCreate, response: Response, db: AsyncSession = Depends(get_db)):
    """Comment on a set of cells: continues the thread they already have (200),
    or starts one (201)."""
    project = await db.get(Project, payload.project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    text = _text(payload.text)
    node_ids = await _validate_members(db, payload.project_id, payload.node_ids)

    annotation = await _thread_on(db, payload.project_id, node_ids)
    if annotation is None:
        annotation = Annotation(project_id=payload.project_id, members=[AnnotationNode(node_id=n) for n in node_ids], messages=[])
        db.add(annotation)
    else:
        response.status_code = 200
    _append(annotation, text, payload.source)
    await db.commit()

    await _notify(payload.project_id)
    return annotation_read(await _load(db, annotation.id))


@router.patch("/annotations/{annotation_id}", response_model=AnnotationRead)
async def update_annotation(annotation_id: uuid.UUID, payload: AnnotationUpdate, db: AsyncSession = Depends(get_db)):
    """Membership and the open/resolved state. What was said changes only
    through the message routes below."""
    annotation = await _load(db, annotation_id)

    if payload.node_ids is not None:
        node_ids = await _validate_members(db, annotation.project_id, payload.node_ids)
        annotation.members.clear()
        await db.flush()
        for node_id in node_ids:
            db.add(AnnotationNode(annotation_id=annotation.id, node_id=node_id))
    if payload.resolved is True and annotation.resolved_at is None:
        annotation.resolved_at = datetime.now(UTC)
        annotation.resolved_by = payload.source
    elif payload.resolved is False:
        annotation.resolved_at = None
        annotation.resolved_by = None
    await db.commit()

    await _notify(annotation.project_id)
    return annotation_read(await _load(db, annotation_id))


@router.delete("/annotations/{annotation_id}", status_code=204)
async def delete_annotation(annotation_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    annotation = await db.get(Annotation, annotation_id)
    if not annotation:
        raise HTTPException(404, "Annotation not found")
    project_id = annotation.project_id
    await db.delete(annotation)
    await db.commit()
    await _notify(project_id)


@router.post("/annotations/{annotation_id}/messages", response_model=AnnotationRead, status_code=201)
async def add_message(annotation_id: uuid.UUID, payload: AnnotationMessageCreate, db: AsyncSession = Depends(get_db)):
    annotation = await _load(db, annotation_id)
    _append(annotation, _text(payload.text), payload.source)
    await db.commit()

    await _notify(annotation.project_id)
    return annotation_read(await _load(db, annotation_id))


@router.patch("/annotation-messages/{message_id}", response_model=AnnotationRead)
async def edit_message(message_id: uuid.UUID, payload: AnnotationMessageUpdate, db: AsyncSession = Depends(get_db)):
    """Only the author may edit. Anything else would put words in someone
    else's mouth -- the exact problem this model replaced, where a person's
    reply typed over an agent's comment still read as the agent's."""
    message = await db.get(AnnotationMessage, message_id)
    if not message:
        raise HTTPException(404, "Message not found")
    if message.source != payload.source:
        raise HTTPException(403, f"Only its author ({message.source}) can edit this message -- reply instead.")
    message.text = _text(payload.text)
    await db.commit()

    annotation = await _load(db, message.annotation_id)
    await _notify(annotation.project_id)
    return annotation_read(annotation)


@router.delete("/annotation-messages/{message_id}", status_code=204)
async def delete_message(message_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Removing the last message removes the thread: a frame with nothing said
    in it is just noise on the grid."""
    message = await db.get(AnnotationMessage, message_id)
    if not message:
        raise HTTPException(404, "Message not found")
    annotation = await _load(db, message.annotation_id)
    if len(annotation.messages) <= 1:
        await db.delete(annotation)
    else:
        await db.delete(message)
    await db.commit()
    await _notify(annotation.project_id)
