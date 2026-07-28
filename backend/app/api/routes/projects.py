import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.grid_layout import compute_layout
from app.core.node_types import resolve_effective_template
from app.core.track_order import ordered_tracks
from app.db.base import get_db
from app.db.models import Annotation, Capability, Node, NodeKind, NodeStatus, Project
from app.schemas.schemas import AnnotationRead, ProjectCreate, ProjectRead, TrackRead

router = APIRouter(prefix="/api/projects", tags=["projects"])


@router.get("", response_model=list[ProjectRead])
async def list_projects(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Project).order_by(Project.created_at))
    return result.scalars().all()


@router.post("", response_model=ProjectRead, status_code=201)
async def create_project(payload: ProjectCreate, db: AsyncSession = Depends(get_db)):
    project = Project(**payload.model_dump())
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return project


@router.get("/{project_id}", response_model=ProjectRead)
async def get_project(project_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    project = await db.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    return project


@router.get("/{project_id}/tracks", response_model=list[TrackRead])
async def list_project_tracks(project_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    # Returned already in list order (head -> next -> ...) -- the frontend
    # derives each track's ephemeral row number from this position, so the
    # order the client renders is exactly the order established here.
    return await ordered_tracks(db, project_id)


@router.get("/{project_id}/layout")
async def project_layout(project_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Backend-computed derived layout (workflow row-spans + blocked cells) --
    see core/grid_layout.py. The client renders from this instead of
    recomputing the span formula itself."""
    return await compute_layout(db, project_id)


@router.get("/{project_id}/recipe")
async def project_recipe(project_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Step-by-step read of a project, for an agent driving it over MCP.

    Composed from the three existing authoritative reads -- ordered_tracks()
    for row order, the nodes themselves, and compute_layout() for spans and
    blocked cells -- rather than re-deriving any of it here.

    `occupied` and `blocked_cells` are the part an agent genuinely cannot do
    without: POST /api/nodes takes a concrete (track_id, step_index), and a
    partial unique index (migration 0011) makes a collision a hard error, so
    "which cells are free" has to be answerable before it writes anything.
    """
    project = await db.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")

    ordered = await ordered_tracks(db, project_id)
    order_of = {t.id: i for i, t in enumerate(ordered)}

    result = await db.execute(
        select(Node)
        .where(Node.track_id.in_([t.id for t in ordered]), Node.status != NodeStatus.discarded)
        .order_by(Node.step_index)
    )
    nodes = list(result.scalars().all())

    # Prompt roles belong to a node type's *capabilities*, not to the node: a
    # "follower" capability mirrors another instance's baked prompt via
    # config.prompt_leader_id (api/routes/capabilities.py). Leader and follower
    # are always capabilities of the same slug, so reporting a role per slug
    # would be meaningless -- what set_prompt actually needs is the ids.
    slugs = {n.node_type.split(".", 1)[1] for n in nodes if n.node_type and "." in n.node_type}
    prompt_links: dict[str, dict] = {}
    if slugs:
        caps = await db.execute(select(Capability).where(Capability.node_type_slug.in_(slugs)))
        for cap in caps.scalars().all():
            leader_id = (cap.config or {}).get("prompt_leader_id")
            if not leader_id:
                continue
            link = prompt_links.setdefault(cap.node_type_slug, {"leader_capability_id": str(leader_id), "follower_capability_ids": []})
            link["follower_capability_ids"].append(str(cap.id))
    layout = await compute_layout(db, project_id)

    steps: dict[int, list[dict]] = {}
    for node in nodes:
        effective = await resolve_effective_template(db, node)
        schema_fields = (effective.param_schema if effective else {}).get("fields", [])
        declared = {f.get("name") for f in schema_fields}
        slug = node.node_type.split(".", 1)[1] if node.node_type and "." in node.node_type else None
        steps.setdefault(node.step_index, []).append(
            {
                "node_id": str(node.id),
                "track_id": str(node.track_id),
                "row": order_of.get(node.track_id),
                "step_index": node.step_index,
                "kind": node.kind.value if isinstance(node.kind, NodeKind) else node.kind,
                "node_type": node.node_type,
                "status": node.status.value if isinstance(node.status, NodeStatus) else node.status,
                # Only params the type actually declares -- raw node.params can
                # also hold leftovers from a previous type assignment.
                "params": {k: v for k, v in (node.params or {}).items() if k in declared},
                "slot_count": len([f for f in schema_fields if f.get("type") in ("image", "file")]),
                "prompt_link": prompt_links.get(slug),
                "created_by_node_id": str(node.created_by_node_id) if node.created_by_node_id else None,
            }
        )

    return {
        "project": {"id": str(project.id), "name": project.name, "start_kind": project.start_kind},
        "tracks": [{"track_id": str(t.id), "row": i} for i, t in enumerate(ordered)],
        "steps": [{"step_index": s, "nodes": steps[s]} for s in sorted(steps)],
        "occupied": sorted([order_of[n.track_id], n.step_index] for n in nodes if n.track_id in order_of),
        "spans": layout["spans"],
        "blocked_cells": layout["blocked_cells"],
    }


@router.get("/{project_id}/annotations", response_model=list[AnnotationRead])
async def list_project_annotations(project_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Comment blocks for this project.

    An annotation whose every member node has been deleted is dropped here
    rather than returned: its frame has nothing left to wrap, so it would be
    invisible in the grid anyway and could never be selected to delete by hand.
    """
    result = await db.execute(
        select(Annotation).options(selectinload(Annotation.members)).where(Annotation.project_id == project_id)
    )
    annotations = list(result.scalars().all())

    orphaned = [a for a in annotations if not a.members]
    for a in orphaned:
        await db.delete(a)
    if orphaned:
        await db.commit()

    return [
        AnnotationRead(
            id=a.id,
            project_id=a.project_id,
            text=a.text,
            source=a.source,
            node_ids=[m.node_id for m in a.members],
            created_at=a.created_at,
            updated_at=a.updated_at,
        )
        for a in annotations
        if a.members
    ]


@router.delete("/{project_id}", status_code=204)
async def delete_project(project_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    project = await db.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    await db.delete(project)
    await db.commit()
