import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.workflow_analyzer import find_editable_text_fields
from app.db.base import get_db
from app.db.models import Capability
from app.schemas.schemas import CapabilityCreate, CapabilityRead, CapabilityTextFieldUpdate, CapabilityUpdate, DetectedFieldOut

router = APIRouter(prefix="/api/capabilities", tags=["capabilities"])


@router.get("", response_model=list[CapabilityRead])
async def list_capabilities(
    node_type_slug: str | None = None, backend_id: uuid.UUID | None = None, db: AsyncSession = Depends(get_db)
):
    stmt = select(Capability)
    if node_type_slug:
        stmt = stmt.where(Capability.node_type_slug == node_type_slug)
    if backend_id:
        stmt = stmt.where(Capability.backend_id == backend_id)
    result = await db.execute(stmt.order_by(Capability.created_at))
    return result.scalars().all()


@router.post("", response_model=CapabilityRead, status_code=201)
async def create_capability(payload: CapabilityCreate, db: AsyncSession = Depends(get_db)):
    capability = Capability(**payload.model_dump())
    db.add(capability)
    await db.commit()
    await db.refresh(capability)
    return capability


@router.get("/{capability_id}", response_model=CapabilityRead)
async def get_capability(capability_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    capability = await db.get(Capability, capability_id)
    if not capability:
        raise HTTPException(404, "Capability not found")
    return capability


@router.patch("/{capability_id}", response_model=CapabilityRead)
async def update_capability(capability_id: uuid.UUID, payload: CapabilityUpdate, db: AsyncSession = Depends(get_db)):
    capability = await db.get(Capability, capability_id)
    if not capability:
        raise HTTPException(404, "Capability not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(capability, field, value)
    await db.commit()
    await db.refresh(capability)
    return capability


@router.get("/{capability_id}/text-fields", response_model=list[DetectedFieldOut])
async def list_capability_text_fields(capability_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Prompt-shaped literal text values baked into this capability's own
    workflow_json (see find_editable_text_fields) -- the ones a user would
    otherwise have to re-upload the whole workflow to change."""
    capability = await db.get(Capability, capability_id)
    if not capability:
        raise HTTPException(404, "Capability not found")
    workflow_json = capability.config.get("workflow_json")
    if not isinstance(workflow_json, dict):
        return []
    param_mapping = capability.config.get("param_mapping") or {}
    return find_editable_text_fields(workflow_json, param_mapping)


@router.patch("/{capability_id}/text-fields", response_model=CapabilityRead)
async def update_capability_text_field(capability_id: uuid.UUID, payload: CapabilityTextFieldUpdate, db: AsyncSession = Depends(get_db)):
    capability = await db.get(Capability, capability_id)
    if not capability:
        raise HTTPException(404, "Capability not found")
    workflow_json = capability.config.get("workflow_json")
    if not isinstance(workflow_json, dict) or payload.node_id not in workflow_json:
        raise HTTPException(400, f"workflow has no node '{payload.node_id}'")
    node = workflow_json[payload.node_id]
    if payload.input_key not in node.get("inputs", {}):
        raise HTTPException(400, f"node '{payload.node_id}' has no input '{payload.input_key}'")

    # config is a plain JSON column (no Mutable* tracking) -- an in-place
    # nested mutation wouldn't be seen as a change by the ORM, so build a
    # whole new config dict and reassign the attribute, same as a full
    # PATCH /capabilities/{id} with a replaced config already does.
    new_workflow_json = {**workflow_json, payload.node_id: {**node, "inputs": {**node["inputs"], payload.input_key: payload.value}}}
    capability.config = {**capability.config, "workflow_json": new_workflow_json}
    await db.commit()
    await db.refresh(capability)
    return capability


@router.delete("/{capability_id}", status_code=204)
async def delete_capability(capability_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    capability = await db.get(Capability, capability_id)
    if not capability:
        raise HTTPException(404, "Capability not found")
    await db.delete(capability)
    await db.commit()
