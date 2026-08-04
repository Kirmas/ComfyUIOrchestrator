"""Creating node types (and extra backend bindings for them) from a workflow.

The human wizard walks someone through this a click at a time and lets them
fix a mistake on the next screen. An agent has no next screen, so everything
is validated up front and a bad call creates nothing at all rather than
leaving a half-built node type nobody can correct.
"""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.node_types import image_field_count
from app.core.workflow_analyzer import WorkflowAnalysis, analyze_workflow
from app.db.models import Capability, ExecutionType, NodeTemplate


class AuthoringError(ValueError):
    """Rejected before anything was written."""


# param_schema field type per detected field type, for building a schema out of
# a mapping without the caller hand-writing one.
_FIELD_TYPES = {"seed": "seed", "int": "int", "float": "float", "text": "text", "bool": "bool", "enum": "enum"}


def validate_param_mapping(analysis: WorkflowAnalysis, param_mapping: dict) -> None:
    """Check a mapping against what the workflow really contains.

    param_mapping is {field_name: {"node_id": ..., "input_key": ...}} -- the
    same shape build_workflow() substitutes into at run time.
    """
    if not isinstance(param_mapping, dict) or not param_mapping:
        raise AuthoringError("param_mapping is required: name every field that should be settable per cell")

    known = {(f.node_id, f.input_key) for f in analysis.detected_fields}
    for field_name, target in param_mapping.items():
        if not isinstance(target, dict) or "node_id" not in target or "input_key" not in target:
            raise AuthoringError(f"param_mapping['{field_name}'] must be {{'node_id': ..., 'input_key': ...}}")
        pair = (str(target["node_id"]), str(target["input_key"]))
        if pair not in known:
            raise AuthoringError(
                f"param_mapping['{field_name}'] points at {pair[0]}.{pair[1]}, which this workflow doesn't expose. "
                f"Available: {sorted(f'{f.node_id}.{f.input_key}' for f in analysis.detected_fields)}"
            )

    # Without a seed among the mapped fields there is nothing to vary between
    # variants, so asking for N candidates would return N identical images.
    # The human wizard doesn't force this; a node type built for unattended
    # fan-out has to have it.
    if "seed" not in param_mapping:
        seeds = [f"{f.node_id}.{f.input_key}" for f in analysis.detected_fields if f.type == "seed" or f.key == "seed"]
        raise AuthoringError(
            "param_mapping must include 'seed' -- without it every requested variant would come out identical. "
            + (f"This workflow's seed input is {seeds[0]}." if seeds else "This workflow exposes no seed input.")
        )


def build_param_schema(analysis: WorkflowAnalysis, param_mapping: dict) -> dict:
    """Derive the param_schema from what the mapping actually names."""
    by_target = {(f.node_id, f.input_key): f for f in analysis.detected_fields}
    fields = []
    for field_name, target in param_mapping.items():
        detected = by_target.get((str(target["node_id"]), str(target["input_key"])))
        field_type = _FIELD_TYPES.get(detected.type if detected else "text", "text")
        if field_name == "seed":
            field_type = "seed"
        fields.append(
            {
                "name": field_name,
                "type": field_type,
                "label": field_name.replace("_", " ").title(),
                "default": detected.default if detected else None,
            }
        )
    # One image field per LoadImage in the graph. These are what decide a
    # node's row span, so the count has to match the workflow exactly.
    for index, _ in enumerate(analysis.input_image_nodes):
        fields.append({"name": f"image_{index + 1}", "type": "image", "label": f"Image {index + 1}", "required": True})
    return {"fields": fields}


async def create_validated_node_type(
    db: AsyncSession,
    *,
    workflow_json: dict,
    name: str,
    slug: str,
    backend_id,
    param_mapping: dict,
) -> tuple[NodeTemplate, Capability]:
    """Create the node type and its first backend binding, or nothing at all."""
    try:
        analysis = analyze_workflow(workflow_json)
    except ValueError as exc:
        raise AuthoringError(str(exc)) from exc
    validate_param_mapping(analysis, param_mapping)

    existing = await db.execute(select(NodeTemplate).where(NodeTemplate.node_type_slug == slug))
    if existing.scalar_one_or_none() is not None:
        raise AuthoringError(f"A node type with slug '{slug}' already exists")

    template = NodeTemplate(
        node_type_slug=slug,
        name=name,
        param_schema=build_param_schema(analysis, param_mapping),
        defaults={},
    )
    db.add(template)
    capability = Capability(
        backend_id=backend_id,
        node_type_slug=slug,
        enabled=True,
        execution_type=ExecutionType.comfyui_workflow,
        config={"workflow_json": workflow_json, "param_mapping": param_mapping},
    )
    db.add(capability)
    await db.commit()
    await db.refresh(template)
    await db.refresh(capability)
    return template, capability


async def add_validated_capability(
    db: AsyncSession,
    *,
    template: NodeTemplate,
    backend_id,
    workflow_json: dict,
    param_mapping: dict,
) -> Capability:
    """Bind an existing node type to another backend.

    Same validation as creating one, plus a check creating one can't need: the
    new workflow must take the same number of images as the node type already
    declares -- image_field_count(), not slot_count(), since a fixed image
    field (wizard-only today, see core/node_types.is_slot_field) still needs
    its own LoadImage node and param_mapping entry on every backend even
    though it doesn't occupy a row. param_schema is shared by every backend
    and its slot_count() is what decides how many rows a node's card covers,
    so a binding that wants a different *slot* count would make the card's
    height wrong on whichever machine happens to run it.
    """
    try:
        analysis = analyze_workflow(workflow_json)
    except ValueError as exc:
        raise AuthoringError(str(exc)) from exc
    validate_param_mapping(analysis, param_mapping)

    expected = image_field_count(template.param_schema)
    incoming = len(analysis.input_image_nodes)
    if incoming != expected:
        raise AuthoringError(
            f"This workflow takes {incoming} image input(s) but '{template.node_type_slug}' declares {expected}. "
            "All backends of a node type must agree on the total number of image inputs."
        )

    existing = await db.execute(
        select(Capability).where(Capability.node_type_slug == template.node_type_slug, Capability.backend_id == backend_id)
    )
    if existing.scalar_one_or_none() is not None:
        raise AuthoringError(f"'{template.node_type_slug}' already has a capability on this backend")

    capability = Capability(
        backend_id=backend_id,
        node_type_slug=template.node_type_slug,
        enabled=True,
        execution_type=ExecutionType.comfyui_workflow,
        config={"workflow_json": workflow_json, "param_mapping": param_mapping},
    )
    db.add(capability)
    await db.commit()
    await db.refresh(capability)
    return capability
