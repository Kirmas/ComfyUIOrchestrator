"""add nodes.node_type discriminator + unique node_templates.node_type_slug

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-17

Adds `nodes.node_type`, a namespaced string discriminator ("asset.select" /
"asset.single" / "native.<slug>" / "template.<slug>") that's meant to become
the single source of truth for "what specific flavor of node is this",
replacing template_id/is_picker as the things application code branches on
(see memory/node_model_refactor_plan.md for the full design). `kind` (asset
vs workflow) is untouched -- that's a different axis (track-layout
alternation), not node flavor, and stays exactly as it was.

template_id/is_picker are NOT dropped here -- they're kept, and the app layer
keeps them mirrored/derived from node_type on every write, purely as a safety
net (nothing should read them as authoritative anymore, but nothing breaks if
some path still does).

node_type_slug gets a real UNIQUE constraint: "template.<slug>" needs a
slug to be a stable, unambiguous identifier, which only holds if duplicates
are actually impossible, not just conventionally avoided.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("nodes", sa.Column("node_type", sa.String(255), nullable=True))
    op.create_unique_constraint("uq_node_templates_node_type_slug", "node_templates", ["node_type_slug"])

    # Backfill from existing kind/is_picker/template_id + capabilities data.
    op.execute(
        """
        UPDATE nodes
        SET node_type = CASE WHEN is_picker THEN 'asset.select' ELSE 'asset.single' END
        WHERE kind = 'asset'
        """
    )
    op.execute(
        """
        UPDATE nodes
        SET node_type = 'native.' || nt.node_type_slug
        FROM node_templates nt
        WHERE nodes.template_id = nt.id
          AND nodes.kind = 'workflow'
          AND EXISTS (
              SELECT 1 FROM capabilities c
              WHERE c.node_type_slug = nt.node_type_slug AND c.execution_type = 'native'
          )
        """
    )
    op.execute(
        """
        UPDATE nodes
        SET node_type = 'template.' || nt.node_type_slug
        FROM node_templates nt
        WHERE nodes.template_id = nt.id
          AND nodes.kind = 'workflow'
          AND NOT EXISTS (
              SELECT 1 FROM capabilities c
              WHERE c.node_type_slug = nt.node_type_slug AND c.execution_type = 'native'
          )
        """
    )
    # workflow nodes with no template_id yet (draft cell, template not chosen) -- node_type stays NULL.


def downgrade() -> None:
    op.drop_constraint("uq_node_templates_node_type_slug", "node_templates", type_="unique")
    op.drop_column("nodes", "node_type")
