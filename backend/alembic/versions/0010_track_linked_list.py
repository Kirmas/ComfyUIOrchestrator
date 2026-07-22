"""tracks ordering: dense row_index -> doubly-linked list

Replaces Track.row_index (a dense 0..N-1 integer that had to be reindexed on
every insert/delete, a non-atomic bulk PATCH that could leave gaps and,
historically, lose data -- 2026-07-21) with a per-project doubly-linked list
(prev_track_id / next_track_id). The visible "track N" number is derived from
list position at render time and never stored again.

Upgrade backfill walks each project's tracks in the OLD row_index order and
chains them -- which also heals any existing gap (e.g. a project stuck at
row_index 0,1,2,5,6,7 becomes a clean contiguous chain), since the chain only
cares about relative order, not the absolute numbers.
"""
from itertools import groupby
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.db.types import GUID

revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("tracks", sa.Column("prev_track_id", GUID(), nullable=True))
    op.add_column("tracks", sa.Column("next_track_id", GUID(), nullable=True))
    op.create_foreign_key(
        "fk_tracks_prev_track", "tracks", "tracks", ["prev_track_id"], ["id"], ondelete="SET NULL"
    )
    op.create_foreign_key(
        "fk_tracks_next_track", "tracks", "tracks", ["next_track_id"], ["id"], ondelete="SET NULL"
    )

    conn = op.get_bind()
    rows = conn.execute(
        sa.text("SELECT id, project_id, row_index FROM tracks ORDER BY project_id, row_index")
    ).fetchall()
    # rows already grouped/ordered by (project_id, row_index) -> chain each project.
    for _project_id, group in groupby(rows, key=lambda r: r[1]):
        chain = list(group)
        for idx, row in enumerate(chain):
            prev_id = chain[idx - 1][0] if idx > 0 else None
            next_id = chain[idx + 1][0] if idx < len(chain) - 1 else None
            conn.execute(
                sa.text("UPDATE tracks SET prev_track_id = :prev, next_track_id = :next WHERE id = :id"),
                {"prev": prev_id, "next": next_id, "id": row[0]},
            )

    op.drop_column("tracks", "row_index")


def downgrade() -> None:
    op.add_column("tracks", sa.Column("row_index", sa.Integer(), nullable=True))

    conn = op.get_bind()
    rows = conn.execute(
        sa.text("SELECT id, project_id, prev_track_id, next_track_id FROM tracks")
    ).fetchall()
    by_project: dict = {}
    for row in rows:
        by_project.setdefault(row[1], []).append(row)

    for _project_id, group in by_project.items():
        by_id = {r[0]: r for r in group}
        heads = [r for r in group if r[2] is None]
        start = heads[0] if heads else group[0]
        seen: set = set()
        idx = 0
        cur = start
        while cur is not None and cur[0] not in seen:
            seen.add(cur[0])
            conn.execute(sa.text("UPDATE tracks SET row_index = :ri WHERE id = :id"), {"ri": idx, "id": cur[0]})
            idx += 1
            next_id = cur[3]
            cur = by_id.get(next_id) if next_id else None
        for row in group:  # any orphan the walk didn't reach
            if row[0] not in seen:
                conn.execute(sa.text("UPDATE tracks SET row_index = :ri WHERE id = :id"), {"ri": idx, "id": row[0]})
                idx += 1

    op.alter_column("tracks", "row_index", nullable=False)
    op.drop_constraint("fk_tracks_prev_track", "tracks", type_="foreignkey")
    op.drop_constraint("fk_tracks_next_track", "tracks", type_="foreignkey")
    op.drop_column("tracks", "prev_track_id")
    op.drop_column("tracks", "next_track_id")
