"""assets.name -- the person's own name for an asset

Revision ID: 0034
Revises: 0033
Create Date: 2026-11-21

Nullable: generated assets start without a name, uploads take the uploaded
file's stem. Existing rows stay NULL (no backfill: no upload ever stored its
original filename before this).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0034"
down_revision: Union[str, None] = "0033"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("assets", sa.Column("name", sa.String(255), nullable=True))


def downgrade() -> None:
    op.drop_column("assets", "name")
