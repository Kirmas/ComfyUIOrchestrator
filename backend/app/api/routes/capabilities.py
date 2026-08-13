import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.workflow_analyzer import find_editable_text_fields
from app.db.base import get_db
from app.db.models import Capability, NodeTemplate
from app.schemas.schemas import (
    CapabilityCreate,
    CapabilityPromptLink,
    CapabilityRead,
    CapabilityTextFieldUpdate,
    CapabilityUpdate,
    DetectedFieldOut,
)

router = APIRouter(prefix="/api/capabilities", tags=["capabilities"])


def _apply_text_value(config: dict, node_id: str, input_key: str, value) -> dict | None:
    """A new config with workflow_json[node_id].inputs[input_key] set to value,
    or None if that node/input isn't present (so a simplified follower workflow
    that lacks the field is just skipped). config is a plain JSON column with no
    Mutable* tracking, so we rebuild it wholesale rather than mutate in place."""
    wf = config.get("workflow_json")
    if not isinstance(wf, dict) or node_id not in wf:
        return None
    node = wf[node_id]
    if input_key not in node.get("inputs", {}):
        return None
    new_wf = {**wf, node_id: {**node, "inputs": {**node["inputs"], input_key: value}}}
    return {**config, "workflow_json": new_wf}


async def _sync_template_default(db: AsyncSession, capability: Capability, node_id: str, input_key: str, value) -> None:
    """Keep NodeTemplate.defaults in step with a capability's own literal for
    any field that's also promoted to a param_schema variable (is_variable).

    NodeTypeWizard.tsx's submitCreate seeds NodeTemplate.defaults[field] from
    the *wizard-time* snapshot of this same value, and NodeCell.tsx's
    chooseTemplate seeds a brand-new node's own Node.params straight from that
    defaults dict -- so without this, editing the field here (which only ever
    touched Capability.config.workflow_json) left every freshly created node
    seeded with a stale, pre-edit prompt that then permanently outranked the
    live literal (Node.params always wins once the key exists, see
    template_engine.build_workflow) -- a fresh node looked like it had "saved"
    a prompt nobody ever typed into it (2026-08-14 report). One shared
    NodeTemplate row backs every backend instance of a node type, so this is
    a single write regardless of which capability/backend was edited -- same
    "one editor, one value" model as the text field itself."""
    param_mapping = capability.config.get("param_mapping") or {}
    field_name = next(
        (k for k, target in param_mapping.items() if target.get("node_id") == node_id and target.get("input_key") == input_key),
        None,
    )
    if field_name is None:
        return
    result = await db.execute(select(NodeTemplate).where(NodeTemplate.node_type_slug == capability.node_type_slug))
    template = result.scalar_one_or_none()
    if template is None:
        return
    template.defaults = {**template.defaults, field_name: value}


async def _followers_of(db: AsyncSession, capability: Capability) -> list[Capability]:
    """Capabilities whose config.prompt_leader_id points at this one. Filtered
    in Python (over the same node type's rows) rather than via a JSON-path query
    so it stays portable across the JSON column's SQLite/Postgres backing."""
    result = await db.execute(select(Capability).where(Capability.node_type_slug == capability.node_type_slug))
    return [c for c in result.scalars().all() if c.config.get("prompt_leader_id") == str(capability.id)]


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
    """Every prompt-shaped text field worth editing for this capability --
    literal values baked into its own workflow_json (see
    find_editable_text_fields), including ones already promoted to a
    param_schema variable (is_variable=true; still edited the same way here,
    see find_editable_text_fields' own docstring for why there's no separate
    path for those anymore)."""
    capability = await db.get(Capability, capability_id)
    if not capability:
        raise HTTPException(404, "Capability not found")
    workflow_json = capability.config.get("workflow_json")
    param_mapping = capability.config.get("param_mapping") or {}
    return find_editable_text_fields(workflow_json, param_mapping) if isinstance(workflow_json, dict) else []


@router.patch("/{capability_id}/text-fields", response_model=CapabilityRead)
async def update_capability_text_field(capability_id: uuid.UUID, payload: CapabilityTextFieldUpdate, db: AsyncSession = Depends(get_db)):
    capability = await db.get(Capability, capability_id)
    if not capability:
        raise HTTPException(404, "Capability not found")
    # A follower mirrors its leader's prompts -- editing them here would just be
    # overwritten on the leader's next edit, so it's read-only (the UI hides the
    # editor too). Edit the leader instead.
    if capability.config.get("prompt_leader_id"):
        raise HTTPException(409, "This instance's prompts are linked to a leader; edit them on the leader instance.")
    updated = _apply_text_value(capability.config, payload.node_id, payload.input_key, payload.value)
    if updated is None:
        raise HTTPException(400, f"workflow has no node '{payload.node_id}' with input '{payload.input_key}'")
    capability.config = updated
    await _sync_template_default(db, capability, payload.node_id, payload.input_key, payload.value)

    # Leader -> followers: mirror this same field into every instance following
    # it (skipping any whose workflow lacks the field -- a simplified variant).
    for follower in await _followers_of(db, capability):
        follower_updated = _apply_text_value(follower.config, payload.node_id, payload.input_key, payload.value)
        if follower_updated is not None:
            follower.config = follower_updated

    await db.commit()
    await db.refresh(capability)
    return capability


@router.patch("/{capability_id}/prompt-link", response_model=CapabilityRead)
async def set_prompt_link(capability_id: uuid.UUID, payload: CapabilityPromptLink, db: AsyncSession = Depends(get_db)):
    """Link this capability's prompts to follow another (leader) instance of the
    same node type, or unlink it (leader_id=None). On linking, the leader's
    current baked prompts are copied in immediately; subsequent leader edits
    propagate via update_capability_text_field."""
    follower = await db.get(Capability, capability_id)
    if not follower:
        raise HTTPException(404, "Capability not found")

    if payload.leader_id is None:
        follower.config = {k: v for k, v in follower.config.items() if k != "prompt_leader_id"}
        await db.commit()
        await db.refresh(follower)
        return follower

    if payload.leader_id == capability_id:
        raise HTTPException(400, "A capability can't follow itself")
    leader = await db.get(Capability, payload.leader_id)
    if not leader:
        raise HTTPException(404, "Leader capability not found")
    if leader.node_type_slug != follower.node_type_slug:
        raise HTTPException(400, "Leader must belong to the same node type")
    if leader.execution_type != "comfyui_workflow" or follower.execution_type != "comfyui_workflow":
        raise HTTPException(400, "Prompt linking is only for ComfyUI workflow instances")
    # No chains: the leader can't itself be a follower, and this capability
    # can't already be leading others.
    if leader.config.get("prompt_leader_id"):
        raise HTTPException(400, "The chosen leader is itself following another instance")
    if await _followers_of(db, follower):
        raise HTTPException(400, "This instance already leads other instances; unlink them first")

    config = {**follower.config, "prompt_leader_id": str(leader.id)}
    leader_wf = leader.config.get("workflow_json") or {}
    leader_mapping = leader.config.get("param_mapping") or {}
    for fld in find_editable_text_fields(leader_wf, leader_mapping):
        synced = _apply_text_value(config, fld.node_id, fld.input_key, fld.default)
        if synced is not None:
            config = synced
    follower.config = config

    await db.commit()
    await db.refresh(follower)
    return follower


@router.delete("/{capability_id}", status_code=204)
async def delete_capability(capability_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    capability = await db.get(Capability, capability_id)
    if not capability:
        raise HTTPException(404, "Capability not found")
    # Orphaned followers would keep a dead prompt_leader_id -- unlink them so
    # they fall back to editing their own prompts again.
    for follower in await _followers_of(db, capability):
        follower.config = {k: v for k, v in follower.config.items() if k != "prompt_leader_id"}
    await db.delete(capability)
    await db.commit()
