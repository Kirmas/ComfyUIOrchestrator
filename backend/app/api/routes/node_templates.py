import json
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.node_types import NATIVE_NODE_TYPES
from app.core.workflow_analyzer import analyze_workflow
from app.db.base import get_db
from app.db.models import NodeTemplate
from app.schemas.schemas import NodeTemplateCreate, NodeTemplateRead, NodeTemplateUpdate, WorkflowAnalysisOut

router = APIRouter(prefix="/api/node-templates", tags=["node_templates"])

# Deterministic namespace so a native type's synthetic id is stable across
# requests/restarts (nothing persists it -- it's derived fresh every time).
_NATIVE_ID_NAMESPACE = uuid.UUID("6f6e6f34-6e61-7469-7665-6e6f64657301")


def _native_template_read(native) -> NodeTemplateRead:
    return NodeTemplateRead(
        id=uuid.uuid5(_NATIVE_ID_NAMESPACE, native.slug),
        node_type_slug=native.slug,
        name=native.name,
        param_schema=native.param_schema,
        defaults=native.defaults,
        created_at=datetime.now(UTC),
        node_type=f"native.{native.slug}",
    )


@router.post("/analyze-workflow", response_model=WorkflowAnalysisOut)
async def analyze_workflow_endpoint(file: UploadFile):
    """Template creation wizard, step 2: parse an uploaded ComfyUI API-format
    workflow.json and detect input/output nodes + standard fields (seed,
    prompts, steps, cfg...) so the wizard can build param_schema/param_mapping
    without the user hand-writing JSON."""
    data = await file.read()
    try:
        workflow_json = json.loads(data)
    except json.JSONDecodeError as exc:
        raise HTTPException(400, f"not valid JSON: {exc}") from exc
    try:
        return analyze_workflow(workflow_json)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("", response_model=list[NodeTemplateRead])
async def list_node_templates(db: AsyncSession = Depends(get_db)):
    """Merges real DB-backed templates (ComfyUI/API node types, genuinely
    user-created via the wizard) with the native registry (code-only, no DB
    row -- see core/node_types.py) into one list, so the frontend's "choose
    node type" flow doesn't need to know which is which."""
    result = await db.execute(select(NodeTemplate).order_by(NodeTemplate.created_at))
    out: list[NodeTemplateRead] = []
    for t in result.scalars().all():
        item = NodeTemplateRead.model_validate(t)
        item.node_type = f"template.{t.node_type_slug}"
        out.append(item)
    for native in NATIVE_NODE_TYPES.values():
        out.append(_native_template_read(native))
    return out


@router.post("", response_model=NodeTemplateRead, status_code=201)
async def create_node_template(payload: NodeTemplateCreate, db: AsyncSession = Depends(get_db)):
    template = NodeTemplate(**payload.model_dump())
    db.add(template)
    await db.commit()
    await db.refresh(template)
    return template


@router.get("/{template_id}", response_model=NodeTemplateRead)
async def get_node_template(template_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    template = await db.get(NodeTemplate, template_id)
    if not template:
        raise HTTPException(404, "Node template not found")
    return template


@router.patch("/{template_id}", response_model=NodeTemplateRead)
async def update_node_template(template_id: uuid.UUID, payload: NodeTemplateUpdate, db: AsyncSession = Depends(get_db)):
    template = await db.get(NodeTemplate, template_id)
    if not template:
        raise HTTPException(404, "Node template not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(template, field, value)
    await db.commit()
    await db.refresh(template)
    return template


@router.delete("/{template_id}", status_code=204)
async def delete_node_template(template_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    template = await db.get(NodeTemplate, template_id)
    if not template:
        raise HTTPException(404, "Node template not found")
    await db.delete(template)
    await db.commit()
