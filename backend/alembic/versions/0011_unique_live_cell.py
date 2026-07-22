"""partial unique index on (track_id, step_index) for live nodes

Safety net for the (track_id, step_index) uniqueness invariant that only
_ensure_slot_free enforced in app code -- several write paths bypass it
(direct column writes: collapse parking, the now-removed sibling-promotion,
any future one). A DB-level partial unique index makes any such duplicate a
loud IntegrityError (500) instead of two live nodes silently sharing one cell,
one of them then unreachable in the grid (a data-loss path).

Partial: only non-discarded nodes are constrained. Discarded rows are re-roll
history and legitimately overlap a live node's cell (see reroll_node /
_ensure_slot_free's own discarded exclusion), so they're excluded here too.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "uq_nodes_live_cell",
        "nodes",
        ["track_id", "step_index"],
        unique=True,
        postgresql_where=sa.text("status <> 'discarded'"),
        sqlite_where=sa.text("status <> 'discarded'"),
    )


def downgrade() -> None:
    op.drop_index("uq_nodes_live_cell", table_name="nodes")
