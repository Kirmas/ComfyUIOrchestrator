"""backends.model_overrides

Revision ID: 0029
Revises: 0028
Create Date: 2026-10-20

Per-PC swap of model files (UNET/CLIP/VAE) inside a shared workflow; see
core/model_overrides.py.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.db.types import JSONVariant

revision: str = "0029"
down_revision: Union[str, None] = "0028"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("backends", sa.Column("model_overrides", JSONVariant, nullable=False, server_default="{}"))


def downgrade() -> None:
    op.drop_column("backends", "model_overrides")
