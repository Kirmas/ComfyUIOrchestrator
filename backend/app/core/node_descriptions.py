"""Resolving what to show for a node type: its description, and the category
the picker sub-groups it under.

Description priority, highest first:
  1. what a person wrote      (frozen until they reset it)
  2. what an agent distilled  (only while the config it was written against is unchanged)
  3. auto, derived from the workflows themselves

The category is derived here rather than in its own pass because it comes off
the same capabilities this already loads (core/node_category.py). Only the
derived value is resolved here -- the manual override lives on the
NodeTemplate row itself, and the route picks between them.
"""
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.node_category import category_for_capabilities
from app.core.node_fingerprint import build_fingerprint, compute_config_hash, describe_fingerprint
from app.db.models import Backend, Capability, DescriptionSource, NodeTypeDescription


@dataclass
class ResolvedDescription:
    description: str
    source: str  # manual | agent | auto
    fingerprint: dict[str, str]
    config_hash: str
    # The *derived* category (model family) only -- "" when nothing about this
    # type says which model it runs on. NodeTemplate.category_override wins
    # over it where set; see api/routes/node_templates.py.
    auto_category: str = ""


async def _backend_names(db: AsyncSession) -> dict:
    result = await db.execute(select(Backend.id, Backend.name))
    return {row[0]: row[1] for row in result.all()}


async def resolve_descriptions(db: AsyncSession, slugs_and_names: list[tuple[str, str]]) -> dict[str, ResolvedDescription]:
    """Resolve many node types at once -- one query per table rather than per
    node type, since the type list is rendered on every settings/grid load."""
    slugs = [slug for slug, _ in slugs_and_names]
    if not slugs:
        return {}

    caps_result = await db.execute(select(Capability).where(Capability.node_type_slug.in_(slugs)))
    by_slug: dict[str, list[Capability]] = {}
    for capability in caps_result.scalars().all():
        by_slug.setdefault(capability.node_type_slug, []).append(capability)

    stored_result = await db.execute(select(NodeTypeDescription).where(NodeTypeDescription.node_type_slug.in_(slugs)))
    stored = {row.node_type_slug: row for row in stored_result.scalars().all()}

    names = await _backend_names(db)

    out: dict[str, ResolvedDescription] = {}
    for slug, display_name in slugs_and_names:
        capabilities = by_slug.get(slug, [])
        fingerprint = build_fingerprint(capabilities, names)
        config_hash = compute_config_hash(capabilities)
        category = category_for_capabilities(capabilities)
        row = stored.get(slug)

        if row is not None and row.description_source == DescriptionSource.manual and row.manual_description:
            out[slug] = ResolvedDescription(row.manual_description, "manual", fingerprint, config_hash, category)
            continue
        # A cached agent description only counts while the configuration it was
        # written against still matches -- otherwise it describes something
        # that no longer exists.
        if row is not None and row.agent_description and row.config_hash == config_hash:
            out[slug] = ResolvedDescription(row.agent_description, "agent", fingerprint, config_hash, category)
            continue
        out[slug] = ResolvedDescription(
            describe_fingerprint(display_name, fingerprint), "auto", fingerprint, config_hash, category
        )
    return out


async def upsert_description(
    db: AsyncSession,
    slug: str,
    *,
    manual: str | None = None,
    agent: str | None = None,
    config_hash: str | None = None,
    reset_to_auto: bool = False,
) -> NodeTypeDescription:
    row = await db.get(NodeTypeDescription, slug)
    if row is None:
        row = NodeTypeDescription(node_type_slug=slug)
        db.add(row)

    if reset_to_auto:
        row.manual_description = None
        row.description_source = DescriptionSource.auto
    if manual is not None:
        row.manual_description = manual
        row.description_source = DescriptionSource.manual
    if agent is not None:
        row.agent_description = agent
        row.config_hash = config_hash
    await db.commit()
    await db.refresh(row)
    return row
