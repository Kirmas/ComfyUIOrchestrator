"""drop nodes.slot_row

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-17

Removes the cosmetic row-override field added in 0004. It let a node be
*displayed* at a different row than the track it structurally belongs to,
without moving track_id -- which meant the view could silently diverge from
the model. That divergence caused real incidents (2026-07-17: deleting a
track that looked empty cascade-deleted nodes that were structurally in it
but displayed elsewhere via slot_row). Position is now always exactly
track_id/row_index -- "moving" a node means actually reassigning its
track_id (see Grid.tsx's dropAssetAt/applyRowMove), the same pattern
onSelectCandidate already used before this column existed.

No data migration: the only rows with slot_row set are today's test data,
which the user has already agreed to rebuild.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column("nodes", "slot_row")


def downgrade() -> None:
    op.add_column("nodes", sa.Column("slot_row", sa.Integer(), nullable=True))
