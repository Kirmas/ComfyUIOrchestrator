"""add nodes.use_api, api_key_permissions.daily_limit, api_usage_log

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-20

Four independent layers gating a paid API call, from outermost to
innermost: (1) ApiKeyPermission.enabled -- the provider is granted to this
node type at all; (2) Node.use_api -- this specific node has explicitly
opted into spending money, never implied by backend_mode="auto"; (3)
ApiKeyPermission.daily_limit against a rolling COUNT(*) over api_usage_log;
(4) a frontend confirmation modal before the request is even sent. This
migration adds the two DB-backed layers (2) and (3).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.db.types import GUID

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("nodes", sa.Column("use_api", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.alter_column("nodes", "use_api", server_default=None)

    op.add_column("api_key_permissions", sa.Column("daily_limit", sa.Integer(), nullable=True))

    op.create_table(
        "api_usage_log",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("permission_id", GUID(), sa.ForeignKey("api_key_permissions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("node_id", GUID(), sa.ForeignKey("nodes.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_api_usage_log_permission_id_created_at", "api_usage_log", ["permission_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_api_usage_log_permission_id_created_at", table_name="api_usage_log")
    op.drop_table("api_usage_log")
    op.drop_column("api_key_permissions", "daily_limit")
    op.drop_column("nodes", "use_api")
