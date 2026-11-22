"""bridge_devices -- approved Unreal/Blender plugins

Revision ID: 0035
Revises: 0034
Create Date: 2026-11-22

A plugin pairs by asking, the person approves it on the Settings page, and the
plugin gets its own key (stored here only as a SHA-256), valid for a month.
Replaces handing the site's API_TOKEN to the plugins. New table only.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.db.types import GUID

revision: str = "0035"
down_revision: Union[str, None] = "0034"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "bridge_devices",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("app", sa.String(32), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("key_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("bridge_devices")
