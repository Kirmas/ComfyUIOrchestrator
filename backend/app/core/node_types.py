"""Node.node_type resolution -- the single place that turns the namespaced
discriminator ("asset.select" / "asset.single" / "native.<slug>" /
"template.<slug>") into whatever a caller actually needs: a param_schema to
validate against, a JobBackend instance to run, or which of asset/single vs
select behavior applies.

"native"/"asset" are resolved via NATIVE_NODE_TYPES, a plain code registry --
no DB row, no FK, because (per memory/node_model_refactor_plan.md) they're a
closed, developer-authored set that only ever grows when someone writes a new
NativeBackend subclass, unlike "template" node types which are genuinely
open-ended, user-created-at-runtime data and stay in node_templates.
"""
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.native_backend import CharacterChartBackend, NativeBackend
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
}


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


def is_picker_type(node_type: str | None) -> bool:
    return node_type == "asset.select"


def sync_legacy_fields(node: Node, effective: EffectiveTemplate | None) -> None:
    """Keeps is_picker/template_id consistent with node.node_type after it
    changes, purely as a safety net for any code path that still reads them --
    new logic should resolve through resolve_effective_template/is_picker_type
    instead. Callers that already resolved an EffectiveTemplate (to validate
    params, dispatch a job, etc.) pass it along so this doesn't need its own
    DB round trip; pass None for asset-kind nodes."""
    parsed = parse_node_type(node.node_type)
    if parsed is None:
        return
    prefix, _ = parsed
    node.is_picker = prefix == "asset" and is_picker_type(node.node_type)
    node.template_id = effective.db_template.id if effective and effective.db_template else None
