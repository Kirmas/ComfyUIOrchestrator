"""nodes.visible_slot_count -- cosmetic cap on rendered card height

Revision ID: 0021
Revises: 0020
Create Date: 2026-09-25

NULL everywhere on upgrade (the only value any node had before this column
existed) means "render the full declared slot count", unchanged behavior.
Set only by POST /api/nodes/{id}/recompute-span -- never touches
slot_count()/_actual_span/_actual_row_span/ensure_span_rows/
_splice_after_would_split_a_span, which stay keyed to the template's true
declared max regardless of this value (see the column's own docstring in
db/models.py).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0021"
down_revision: Union[str, None] = "0020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("nodes", sa.Column("visible_slot_count", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("nodes", "visible_slot_count")
