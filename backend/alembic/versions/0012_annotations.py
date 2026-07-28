"""add annotations + annotation_nodes (comment blocks)

Revision ID: 0012
Revises: 0011
Create Date: 2026-07-28

A comment block is free text attached to a set of nodes, drawn as a frame
around them. No coordinates are stored: the frame's box is derived from where
its members currently sit, so it follows them when they move (same rule the
grid already applies to nodes -- position is always track_id + step_index,
never a stored display-only override).

annotation_nodes cascades on both sides: deleting a node drops it out of any
frame (the frame survives around the remaining members), deleting an
annotation drops its membership rows.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.db.types import GUID

revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "annotations",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("project_id", GUID(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("text", sa.Text(), nullable=False, server_default=""),
        sa.Column("source", sa.String(16), nullable=False, server_default="user"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_table(
        "annotation_nodes",
        sa.Column("annotation_id", GUID(), sa.ForeignKey("annotations.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("node_id", GUID(), sa.ForeignKey("nodes.id", ondelete="CASCADE"), primary_key=True),
    )


def downgrade() -> None:
    op.drop_table("annotation_nodes")
    op.drop_table("annotations")
