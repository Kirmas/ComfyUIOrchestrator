"""add nodes.slot_row

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-17

Rendering-only row override for an asset-kind node (see db/models.py's Node.slot_row
docstring): lets the frontend position a node at an absolute grid row that differs
from its own track's row_index, without touching track_id/step_index (which the
backend's generation lookups are keyed on). Nullable, no backfill -- NULL means "use
the track's own row".
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("nodes", sa.Column("slot_row", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("nodes", "slot_row")
