"""add node_type_descriptions

Revision ID: 0013
Revises: 0012
Create Date: 2026-07-28

Descriptions of node types, from three sources (hand-written, agent-distilled,
or auto-derived from the workflows). Keyed by slug, not by a FK to
node_templates, because native node types have no row there -- see
db/models.py's NodeTypeDescription docstring.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: Union[str, None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "node_type_descriptions",
        sa.Column("node_type_slug", sa.String(128), primary_key=True),
        sa.Column("manual_description", sa.Text(), nullable=True),
        sa.Column("agent_description", sa.Text(), nullable=True),
        sa.Column("description_source", sa.String(16), nullable=False, server_default="auto"),
        sa.Column("config_hash", sa.String(64), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("node_type_descriptions")
