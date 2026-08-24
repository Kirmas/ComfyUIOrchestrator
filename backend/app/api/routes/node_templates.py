import json
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.comfyui_backend import ComfyUIBackend
from app.core.node_descriptions import resolve_descriptions, upsert_description
from app.core.node_types import NATIVE_NODE_TYPES
from app.core.workflow_analyzer import analyze_workflow, apply_combo_options
from app.db.base import get_db
from app.db.models import Backend, BackendKind, NodeTemplate
from app.schemas.schemas import (
    AgentDescriptionWrite,
    ManualDescriptionWrite,
    NodeTemplateCreate,
    NodeTemplateListRead,
    NodeTemplateRead,
    NodeTemplateUpdate,
    WorkflowAnalysisOut,
)

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
async def analyze_workflow_endpoint(
    file: UploadFile,
    backend_id: uuid.UUID | None = Form(None),
    db: AsyncSession = Depends(get_db),
):
    """Template creation wizard, step 2: parse an uploaded ComfyUI API-format
    workflow.json and detect input/output nodes + standard fields (seed,
    prompts, steps, cfg...) so the wizard can build param_schema/param_mapping
    without the user hand-writing JSON.

    backend_id is the ComfyUI instance this node type is being created for. It
    is optional and only used to turn combo widgets into enum fields with their
    real option lists (/object_info) -- a workflow.json carries the chosen
    value but never the choices. It has to be that specific instance rather
    than any reachable one, because a custom node can be installed on one
    backend and absent from another.
    """
    data = await file.read()
    try:
        workflow_json = json.loads(data)
    except json.JSONDecodeError as exc:
        raise HTTPException(400, f"not valid JSON: {exc}") from exc
    try:
        analysis = analyze_workflow(workflow_json)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    if backend_id is not None:
        backend = await db.get(Backend, backend_id)
        if backend is not None and backend.kind == BackendKind.comfyui and backend.base_url:
            class_types = {
                node.get("class_type")
                for node in (workflow_json.get(f.node_id) for f in analysis.detected_fields)
                if isinstance(node, dict) and node.get("class_type")
            }
            if class_types:
                object_info = await ComfyUIBackend(backend.base_url).fetch_object_info(sorted(class_types))
                apply_combo_options(analysis, workflow_json, object_info)

    return analysis


@router.get("", response_model=list[NodeTemplateListRead])
async def list_node_templates(db: AsyncSession = Depends(get_db)):
    """Merges real DB-backed templates (ComfyUI/API node types, genuinely
    user-created via the wizard) with the native registry (code-only, no DB
    row -- see core/node_types.py) into one list, so the frontend's "choose
    node type" flow doesn't need to know which is which.

    Returns NodeTemplateListRead -- no `defaults` field at all -- rather than
    the full NodeTemplateRead (see get_defaults below for where that lives).
    A single "fixed" image/file field bakes its whole value (base64) into
    NodeTemplate.defaults, and this list is fetched on every grid load (to
    populate the "choose node type" dropdown, which never reads defaults) and
    on every MCP list_node_types() call -- one template with a several-
    megabyte fixed image made every single one of those calls several
    megabytes too, agent-facing ones included (2026-08-13, the exact thing
    that made list_node_types() blow an MCP client's response-size limit).
    response_model_exclude={"defaults"} looked like the obvious fix but
    turned out not to actually filter fields on a `list[Model]` response in
    this FastAPI/Pydantic combo -- confirmed with a minimal repro, `defaults`
    kept showing up in the real JSON regardless -- so this validates into a
    model that never declares the field instead."""
    result = await db.execute(select(NodeTemplate).order_by(NodeTemplate.created_at))
    out: list[NodeTemplateListRead] = []
    overrides: dict[str, str] = {}
    for t in result.scalars().all():
        item = NodeTemplateListRead.model_validate(t)
        item.node_type = f"template.{t.node_type_slug}"
        if t.category_override:
            overrides[t.node_type_slug] = t.category_override
        out.append(item)
    for native in NATIVE_NODE_TYPES.values():
        out.append(NodeTemplateListRead.model_validate(_native_template_read(native)))

    resolved = await resolve_descriptions(db, [(item.node_type_slug, item.name) for item in out])
    for item in out:
        entry = resolved.get(item.node_type_slug)
        if entry:
            item.description = entry.description
            item.description_source = entry.source
            item.fingerprint = entry.fingerprint
            # A hand-set label wins over the derived model family; both are
            # resolved here rather than stored, so attaching a backend that
            # runs a different checkpoint re-groups the type on the next read.
            item.category = overrides.get(item.node_type_slug) or entry.auto_category
            item.category_source = "manual" if item.node_type_slug in overrides else "auto"
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
    data = payload.model_dump(exclude_unset=True)
    if "category_override" in data:
        # A cleared input arrives as "" and means the same as null -- back to
        # the derived category. Storing the empty string instead would read as
        # "overridden to nothing" everywhere it's checked.
        data["category_override"] = (data["category_override"] or "").strip() or None
    for field, value in data.items():
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


async def _resolve_one(db: AsyncSession, slug: str):
    """A slug's own display name, whether it's a DB template or a native type."""
    result = await db.execute(select(NodeTemplate).where(NodeTemplate.node_type_slug == slug))
    template = result.scalar_one_or_none()
    if template is not None:
        name = template.name
    elif slug in NATIVE_NODE_TYPES:
        name = NATIVE_NODE_TYPES[slug].name
    else:
        raise HTTPException(404, f"Unknown node type: {slug}")
    resolved = await resolve_descriptions(db, [(slug, name)])
    return resolved[slug]


@router.get("/by-slug/{slug}/defaults")
async def get_defaults(slug: str, db: AsyncSession = Depends(get_db)):
    """The one place a node type's defaults (including any baked "fixed"
    image/file bytes) actually live -- split out of list_node_templates on
    purpose (see that route's own docstring). Fetched only when something
    genuinely needs them: NodeCell.tsx's chooseTemplate (seeding a freshly
    assigned node's own params) and Settings.tsx's AddApiInstanceForm
    (merging new fields into an existing template's defaults) both used to
    read this off the already-loaded list; now they call here instead, right
    before they need it."""
    result = await db.execute(select(NodeTemplate).where(NodeTemplate.node_type_slug == slug))
    template = result.scalar_one_or_none()
    if template is not None:
        return {"node_type_slug": slug, "defaults": template.defaults}
    native = NATIVE_NODE_TYPES.get(slug)
    if native is not None:
        return {"node_type_slug": slug, "defaults": native.defaults}
    raise HTTPException(404, f"Unknown node type: {slug}")


@router.get("/by-slug/{slug}/description")
async def get_description(slug: str, db: AsyncSession = Depends(get_db)):
    entry = await _resolve_one(db, slug)
    return {
        "node_type_slug": slug,
        "description": entry.description,
        "description_source": entry.source,
        "fingerprint": entry.fingerprint,
        "config_hash": entry.config_hash,
    }


@router.patch("/by-slug/{slug}/description")
async def set_manual_description(slug: str, payload: ManualDescriptionWrite, db: AsyncSession = Depends(get_db)):
    """A hand-written description wins over everything else and stops being
    regenerated -- until it's reset (DELETE below)."""
    await _resolve_one(db, slug)  # 404s on an unknown slug before writing
    await upsert_description(db, slug, manual=payload.description)
    return await get_description(slug, db)


@router.delete("/by-slug/{slug}/description")
async def reset_description_to_auto(slug: str, db: AsyncSession = Depends(get_db)):
    """Drop the hand-written text and let the description follow the workflows
    again."""
    await _resolve_one(db, slug)
    await upsert_description(db, slug, reset_to_auto=True)
    return await get_description(slug, db)


@router.post("/by-slug/{slug}/agent-description")
async def write_agent_description(slug: str, payload: AgentDescriptionWrite, db: AsyncSession = Depends(get_db)):
    """Let an agent replace a long auto description with its own shorter
    reading of the same thing.

    The point is to save whoever reads this next from wading through the
    original, so a candidate that isn't actually shorter than what the agent
    read is refused. Length in characters, not words: JSON and graph dumps are
    almost no words at all but plenty to read.
    """
    entry = await _resolve_one(db, slug)
    candidate = payload.description.strip()
    if not candidate:
        raise HTTPException(422, "description must not be empty")

    ceiling = max(payload.source_length, len(entry.description))
    if len(candidate) >= ceiling:
        raise HTTPException(
            422,
            f"description must be shorter than what it summarises ({len(candidate)} >= {ceiling} characters)",
        )
    if entry.source == "manual":
        raise HTTPException(409, "this node type has a hand-written description; not overwriting it")

    await upsert_description(db, slug, agent=candidate, config_hash=entry.config_hash)
    return await get_description(slug, db)
