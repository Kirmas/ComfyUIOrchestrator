"""fold provider/api_key/daily_limit into backends, drop api_key_permissions

Revision ID: 0008
Revises: 0007
Create Date: 2026-07-20

0007's ApiKeyPermission (provider + node_type_slug + api_key, one grant per
node type) turned out to be the wrong shape: a single Gemini account/key is
naturally shared across every node type that wants it, not duplicated per
node type. This moves provider/api_key/daily_limit directly onto the
api_provider Backend row instead -- one Backend = one key, and any number of
Capability rows across any number of node types can point their backend_id
at it. api_key_permissions had zero rows in prod at the time of this
migration (the feature had just shipped, unused), so this drops it outright
rather than migrating data.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.db.types import GUID

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("backends", sa.Column("provider", sa.String(128), nullable=True))
    op.add_column("backends", sa.Column("api_key", sa.String(1024), nullable=True))
    op.add_column("backends", sa.Column("daily_limit", sa.Integer(), nullable=True))

    op.drop_index("ix_api_usage_log_permission_id_created_at", table_name="api_usage_log")
    op.drop_constraint("api_usage_log_permission_id_fkey", "api_usage_log", type_="foreignkey")
    op.drop_column("api_usage_log", "permission_id")
    op.add_column("api_usage_log", sa.Column("backend_id", GUID(), nullable=False))
    op.create_foreign_key(
        "api_usage_log_backend_id_fkey", "api_usage_log", "backends", ["backend_id"], ["id"], ondelete="CASCADE"
    )
    op.create_index("ix_api_usage_log_backend_id_created_at", "api_usage_log", ["backend_id", "created_at"])

    op.drop_table("api_key_permissions")


def downgrade() -> None:
    op.create_table(
        "api_key_permissions",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("provider", sa.String(128), nullable=False),
        sa.Column("node_type_slug", sa.String(128), nullable=False),
        sa.Column("api_key", sa.String(1024), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("daily_limit", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.drop_index("ix_api_usage_log_backend_id_created_at", table_name="api_usage_log")
    op.drop_constraint("api_usage_log_backend_id_fkey", "api_usage_log", type_="foreignkey")
    op.drop_column("api_usage_log", "backend_id")
    op.add_column("api_usage_log", sa.Column("permission_id", GUID(), nullable=False))
    op.create_foreign_key(
        "api_usage_log_permission_id_fkey", "api_usage_log", "api_key_permissions", ["permission_id"], ["id"], ondelete="CASCADE"
    )
    op.create_index("ix_api_usage_log_permission_id_created_at", "api_usage_log", ["permission_id", "created_at"])

    op.drop_column("backends", "daily_limit")
    op.drop_column("backends", "api_key")
    op.drop_column("backends", "provider")
