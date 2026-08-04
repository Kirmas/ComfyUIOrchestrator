"""Agent-facing node-type authoring and cross-backend bindings.

Separate from /api/node-templates (the human wizard's endpoints) because the
rules differ: everything here is validated up front and refuses on any
mismatch, since the caller has no wizard to correct itself in. The human flow
is untouched.
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.node_type_authoring import (
    AuthoringError,
    add_validated_capability,
    create_validated_node_type,
)
from app.core.node_types import slot_count
from app.db.base import get_db
from app.db.models import Backend, Capability, NodeTemplate
from app.schemas.schemas import AddCapabilityRequest, CreateNodeTypeRequest

router = APIRouter(prefix="/api/node-types", tags=["node_types"])


async def _template_or_404(db: AsyncSession, slug: str) -> NodeTemplate:
    result = await db.execute(select(NodeTemplate).where(NodeTemplate.node_type_slug == slug))
    template = result.scalar_one_or_none()
    if template is None:
        raise HTTPException(404, f"Unknown node type: {slug}")
    return template


async def _backend_or_404(db: AsyncSession, backend_id: uuid.UUID) -> Backend:
    backend = await db.get(Backend, backend_id)
    if backend is None:
        raise HTTPException(404, "Backend not found")
    return backend


@router.post("", status_code=201)
async def create_node_type(payload: CreateNodeTypeRequest, db: AsyncSession = Depends(get_db)):
    await _backend_or_404(db, payload.backend_id)
    try:
        template, capability = await create_validated_node_type(
            db,
            workflow_json=payload.workflow_json,
            name=payload.name,
            slug=payload.node_type_slug,
            backend_id=payload.backend_id,
            param_mapping=payload.param_mapping,
        )
    except AuthoringError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {
        "node_type_slug": template.node_type_slug,
        "node_type": f"template.{template.node_type_slug}",
        "name": template.name,
        "param_schema": template.param_schema,
        "capability_id": str(capability.id),
        "backend_id": str(capability.backend_id),
    }


@router.get("/{slug}/capability-exists")
async def capability_exists(slug: str, backend_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Whether this node type can actually run on this backend.

    Without this, a node type that exists but has no binding for the machine in
    question just never progresses, with nothing to say why.
    """
    await _template_or_404(db, slug)
    result = await db.execute(
        select(Capability).where(Capability.node_type_slug == slug, Capability.backend_id == backend_id)
    )
    capability = result.scalar_one_or_none()
    return {
        "node_type_slug": slug,
        "backend_id": str(backend_id),
        "exists": capability is not None,
        "enabled": bool(capability.enabled) if capability else False,
        "capability_id": str(capability.id) if capability else None,
    }


@router.get("/{slug}/reference-capability")
async def reference_capability(slug: str, exclude_backend_id: uuid.UUID | None = None, db: AsyncSession = Depends(get_db)):
    """A working binding of this node type from some other backend, to adapt.

    Answers "show me the recipe that already works elsewhere" -- the starting
    point for making the same node type run on a machine that lacks it.
    """
    await _template_or_404(db, slug)
    query = select(Capability).where(Capability.node_type_slug == slug)
    if exclude_backend_id is not None:
        query = query.where(Capability.backend_id != exclude_backend_id)
    result = await db.execute(query)
    capability = next((c for c in result.scalars().all() if (c.config or {}).get("workflow_json")), None)
    if capability is None:
        raise HTTPException(404, f"No other backend has a workflow-backed capability for '{slug}'")
    config = capability.config or {}
    return {
        "capability_id": str(capability.id),
        "backend_id": str(capability.backend_id),
        "execution_type": capability.execution_type,
        "workflow_json": config.get("workflow_json"),
        "param_mapping": config.get("param_mapping", {}),
    }


@router.post("/{slug}/capabilities", status_code=201)
async def add_capability(slug: str, payload: AddCapabilityRequest, db: AsyncSession = Depends(get_db)):
    """Make an existing node type runnable on another backend."""
    template = await _template_or_404(db, slug)
    await _backend_or_404(db, payload.backend_id)
    try:
        capability = await add_validated_capability(
            db,
            template=template,
            backend_id=payload.backend_id,
            workflow_json=payload.workflow_json,
            param_mapping=payload.param_mapping,
        )
    except AuthoringError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {
        "capability_id": str(capability.id),
        "node_type_slug": slug,
        "backend_id": str(capability.backend_id),
        "declared_image_slots": slot_count(template.param_schema),
    }
