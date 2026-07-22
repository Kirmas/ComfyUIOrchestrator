"""add nodes.collapse_target_id

Revision ID: 0009
Revises: 0008
Create Date: 2026-07-21

Self-referencing, nullable FK, same shape as 0006's created_by_node_id: set
only via POST /api/nodes/{id}/collapse|expand (api/routes/nodes.py) on the
pass-through asset node of a workflow -> asset -> workflow chain, pointing at
the second workflow. NULL (the default, no backfill needed) means "not
collapsed" -- true for every node that predates this column.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.db.types import GUID

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("nodes", sa.Column("collapse_target_id", GUID(), nullable=True))
    op.create_foreign_key(
        "fk_nodes_collapse_target_id",
        "nodes",
        "nodes",
        ["collapse_target_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_nodes_collapse_target_id", "nodes", type_="foreignkey")
    op.drop_column("nodes", "collapse_target_id")
