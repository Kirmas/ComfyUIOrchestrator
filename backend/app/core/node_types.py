"""Workflow-node node_type resolution -- the single place that turns the
namespaced discriminator ("native.<slug>" / "template.<slug>") into whatever
a caller actually needs: a param_schema to validate against, or a JobBackend
instance to run.

The `asset.*` half of the discriminator is resolved by core/asset_types.py
instead -- one class per asset kind, keyed the same way.

node_type is now the *only* thing consulted. The mirrored nodes.is_picker /
nodes.template_id columns this module used to keep in sync were written for
nobody's benefit and were dropped in migration 0017: "is this a picker" is
is_picker_type(node_type), "which template" is resolve_effective_template.

"native" node types are resolved via NATIVE_NODE_TYPES, a plain code registry --
no DB row, no FK, because (per memory/node_model_refactor_plan.md) they're a
closed, developer-authored set that only ever grows when someone writes a new
NativeBackend subclass, unlike "template" node types which are genuinely
open-ended, user-created-at-runtime data and stay in node_templates.
"""
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.native_backend import (
    DEFAULT_TRANSPLANT_FEATHER,
    CharacterChartBackend,
    CropBackend,
    MaskBackend,
    NativeBackend,
    TransplantBackend,
)
from app.db.models import Node, NodeTemplate


@dataclass
class NativeNodeType:
    slug: str
    name: str
    param_schema: dict[str, Any]
    defaults: dict[str, Any]
    backend_cls: type[NativeBackend]


NATIVE_NODE_TYPES: dict[str, NativeNodeType] = {
    "character_chart": NativeNodeType(
        slug="character_chart",
        name="Character Chart",
        param_schema={
            "fields": [
                {"name": "head_1", "type": "image", "label": "Head 1", "required": True},
                {"name": "head_2", "type": "image", "label": "Head 2", "required": True},
                {"name": "head_3", "type": "image", "label": "Head 3", "required": True},
                {"name": "head_4", "type": "image", "label": "Head 4", "required": True},
                {"name": "body_1", "type": "image", "label": "Body 1", "required": True},
                {"name": "body_2", "type": "image", "label": "Body 2", "required": True},
                {"name": "body_3", "type": "image", "label": "Body 3", "required": True},
                {"name": "body_4", "type": "image", "label": "Body 4", "required": True},
                {"name": "width", "type": "int", "label": "Chart Width", "default": 7680},
                {"name": "height", "type": "int", "label": "Chart Height", "default": 4320},
                {"name": "head_height_factor", "type": "float", "label": "Head Height Factor", "default": 1.0, "optional": True},
            ]
        },
        defaults={"width": 7680, "height": 4320, "head_height_factor": 1.0},
        backend_cls=CharacterChartBackend,
    ),
    "crop": NativeNodeType(
        slug="crop",
        name="Crop",
        param_schema={
            "fields": [
                {"name": "image", "type": "image", "label": "Image", "required": True},
                {"name": "crop_x", "type": "int", "label": "Crop X", "default": 0},
                {"name": "crop_y", "type": "int", "label": "Crop Y", "default": 0},
                {"name": "crop_width", "type": "int", "label": "Crop Width", "default": 512},
                {"name": "crop_height", "type": "int", "label": "Crop Height", "default": 512},
            ]
        },
        defaults={"crop_x": 0, "crop_y": 0, "crop_width": 512, "crop_height": 512},
        backend_cls=CropBackend,
    ),
    "transplant": NativeNodeType(
        slug="transplant",
        name="Transplant",
        param_schema={
            "fields": [
                # Slot order is the layer order the editor stacks them in:
                # "target" on top (the image being fixed, kept everywhere the
                # user doesn't paint), "source" underneath (the older image
                # whose detail shows through where they do).
                {"name": "target", "type": "image", "label": "Target (top layer)", "required": True},
                {"name": "source", "type": "image", "label": "Source (underneath)", "required": True},
                {"name": "transplant_png", "type": "layer_mask", "label": "Transplanted area", "optional": True},
                {"name": "transplant_feather", "type": "float", "label": "Edge Feather (px)", "default": DEFAULT_TRANSPLANT_FEATHER, "optional": True},
            ]
        },
        defaults={"transplant_feather": DEFAULT_TRANSPLANT_FEATHER},
        backend_cls=TransplantBackend,
    ),
    "mask": NativeNodeType(
        slug="mask",
        name="Paint Mask",
        param_schema={
            "fields": [
                {"name": "image", "type": "image", "label": "Image", "required": True},
                {"name": "mask_png", "type": "mask", "label": "Mask", "optional": True},
            ]
        },
        defaults={},
        backend_cls=MaskBackend,
    ),
}


def is_slot_field(field: dict[str, Any]) -> bool:
    """True for an image/file field that draws its value from a grid cell each
    run (positionally, via Node.inputs -- see resolve_node_inputs in
    worker/tasks.py) and therefore needs a row of its own in the workflow
    node's span. False for one marked "fixed": its value is a constant
    (base64 bytes in NodeTemplate.defaults[field["name"]]) baked onto the
    node type itself, resolved the same way on every instance, so it never
    occupies a row or shows a per-cell picker."""
    return field.get("type") in ("image", "file") and not field.get("fixed")


def slot_fields(schema: dict[str, Any] | None) -> list[dict[str, Any]]:
    return [f for f in (schema or {}).get("fields", []) if is_slot_field(f)]


def slot_count(schema: dict[str, Any] | None) -> int:
    """How many rows a workflow node's card needs for per-cell image/file
    inputs -- the single source both the backend's row-span/growth logic
    (api/routes/nodes.py, core/grid_layout.py) and the frontend's
    slotFields() (templateUtils.ts) mirror. Excludes fixed image fields."""
    return len(slot_fields(schema))


def image_field_count(schema: dict[str, Any] | None) -> int:
    """Every image/file field, fixed or not. Used only where the question is
    "how many LoadImage-shaped inputs does this node type's workflow.json
    need" (add_validated_capability's cross-backend consistency check) --
    a fixed field still needs its own param_mapping entry on every backend
    even though it doesn't affect row-span, so that check must not use
    slot_count()."""
    return len([f for f in (schema or {}).get("fields", []) if f.get("type") in ("image", "file")])


@dataclass
class EffectiveTemplate:
    """Whatever the rest of the app needs about "what kind of workflow node is
    this", regardless of whether it came from the native registry or the DB."""

    node_type_slug: str
    param_schema: dict[str, Any]
    defaults: dict[str, Any]
    is_native: bool
    native: NativeNodeType | None = None
    db_template: NodeTemplate | None = None

    @property
    def is_deterministic(self) -> bool:
        """True if requesting more than one variant would just produce N
        pixel-identical results at N times the cost -- native execution
        always is (no seed concept at all, is_native alone already covered
        it), and so is any comfyui_workflow template whose baked graph
        exposes no seed-type field for a variant to vary (e.g. a SAM3
        mask-extraction graph with no sampler in it at all -- see
        workflow_analyzer.py's SAMPLER_LITERAL_FIELDS/KNOWN_NODE_LITERAL_FIELDS,
        the only things that ever produce a "seed" field)."""
        if self.is_native:
            return True
        return not any(f.get("type") == "seed" for f in (self.param_schema or {}).get("fields", []))


def parse_node_type(node_type: str | None) -> tuple[str, str] | None:
    if not node_type or "." not in node_type:
        return None
    prefix, _, key = node_type.partition(".")
    return prefix, key


async def resolve_effective_template(db: AsyncSession, node: Node) -> EffectiveTemplate | None:
    """Node.node_type -> whatever describes its schema/execution, or None for
    an asset-kind node (no template concept applies) or an unset/draft
    workflow cell (node_type is still None, template not chosen yet)."""
    parsed = parse_node_type(node.node_type)
    if parsed is None:
        return None
    prefix, key = parsed

    if prefix == "native":
        native = NATIVE_NODE_TYPES.get(key)
        if native is None:
            return None
        return EffectiveTemplate(
            node_type_slug=native.slug, param_schema=native.param_schema, defaults=native.defaults, is_native=True, native=native
        )

    if prefix == "template":
        result = await db.execute(select(NodeTemplate).where(NodeTemplate.node_type_slug == key))
        template = result.scalars().first()
        if template is None:
            return None
        return EffectiveTemplate(
            node_type_slug=template.node_type_slug,
            param_schema=template.param_schema,
            defaults=template.defaults,
            is_native=False,
            db_template=template,
        )

    return None


# Optional, gitignored extension point (see .gitignore) for native node types
# that must never end up in this repo's history -- nothing in this file
# depends on it existing, and a checkout without it behaves exactly as if
# this block weren't here at all.
try:
    from app.core.native_local import LOCAL_NATIVE_NODE_TYPES

    NATIVE_NODE_TYPES.update(LOCAL_NATIVE_NODE_TYPES)
except ImportError:
    pass
