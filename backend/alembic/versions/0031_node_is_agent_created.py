"""nodes.is_agent_created -- marks nodes created via MCP / agent

Revision ID: 0031
Revises: 0030
Create Date: 2026-10-24

Defaults to False on upgrade. Set to True when created via MCP tools.
Can be cleared by the user in the UI to protect the node from deletion by agents.
The delete_node MCP tool only permits deleting nodes with is_agent_created=True.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0031"
down_revision: Union[str, None] = "0030"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "nodes", sa.Column("is_agent_created", sa.Boolean(), nullable=False, server_default=sa.false())
    )


def downgrade() -> None:
    op.drop_column("nodes", "is_agent_created")
