"""drop backends.model_overrides

Revision ID: 0030
Revises: 0029
Create Date: 2026-10-20

0029 stored model overrides per backend; they belong to a (backend, workflow)
pair, i.e. Capability.config["model_overrides"], which needs no column.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.db.types import JSONVariant

revision: str = "0030"
down_revision: Union[str, None] = "0029"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column("backends", "model_overrides")


def downgrade() -> None:
    op.add_column("backends", sa.Column("model_overrides", JSONVariant, nullable=False, server_default="{}"))
