"""add nodes.created_by_node_id

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-18

Self-referencing, nullable FK: set exactly once by
_get_or_create_output_asset_node (worker/tasks.py) when it materializes a
workflow node's result as a following asset node, never written anywhere
else. Rigidly binds that asset to its creator's own output position -- see
db/models.py's Node.created_by_node_id docstring and api/routes/nodes.py's
_ensure_output_binding. NULL (the default, no backfill needed) means "no
creator, freely repositionable" -- true for every asset that predates this
column.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.db.types import GUID

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("nodes", sa.Column("created_by_node_id", GUID(), nullable=True))
    op.create_foreign_key(
        "fk_nodes_created_by_node_id",
        "nodes",
        "nodes",
        ["created_by_node_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_nodes_created_by_node_id", "nodes", type_="foreignkey")
    op.drop_column("nodes", "created_by_node_id")
