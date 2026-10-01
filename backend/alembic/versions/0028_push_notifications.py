"""Web Push: push_subscriptions + vapid_keypair

Revision ID: 0028
Revises: 0027
Create Date: 2026-10-01

Lets agent_runner nudge a subscribed browser when an agent chat turn finishes
(core/push.py, routes/push.py), even with the tab closed. The VAPID signing
key lives in vapid_keypair (a singleton row, generated lazily on first use)
rather than .env, so there's nothing to configure by hand on dev or prod.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.db.types import GUID

revision: str = "0028"
down_revision: Union[str, None] = "0027"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "push_subscriptions",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("endpoint", sa.Text(), nullable=False, unique=True),
        sa.Column("p256dh", sa.Text(), nullable=False),
        sa.Column("auth", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_table(
        "vapid_keypair",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("private_key_b64", sa.Text(), nullable=False),
        sa.Column("public_key_b64", sa.Text(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("vapid_keypair")
    op.drop_table("push_subscriptions")
