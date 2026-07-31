"""sub-dashboards: dashboards table, per-scope tracks, smart pointers

Revision ID: 0015
Revises: 0014
Create Date: 2026-07-31

A project's grid stops being a single coordinate space. A *scope* is now
(project, dashboard), and `tracks.dashboard_id IS NULL` means the project's
main grid -- which is why this migration is purely additive and touches no
existing row: every track that already exists reads as "main" for free, and
the ordering linked list is unchanged (only the key it is filtered by).

- dashboards: one sub-grid. `start_kind` is its own column-parity origin, so a
  sub-dashboard may start on a different kind than the graph pointing at it.
  `owner_node_id` is its *main* smart pointer.
- tracks.dashboard_id: which scope a track belongs to (NULL = main).
- nodes.subgraph_dashboard_id: set on an `asset.subgraph` node -- the smart
  pointer -- naming the dashboard it opens. Several nodes may point at one
  dashboard; reachability is guaranteed by the ownership rules in
  api/routes/dashboards.py, not by this column.

dashboards.owner_node_id uses use_alter because nodes and dashboards reference
each other. It is SET NULL rather than CASCADE on purpose: losing the owner
must never silently destroy a dashboard's contents.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.db.types import GUID

revision: str = "0015"
down_revision: Union[str, None] = "0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "dashboards",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("project_id", GUID(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("start_kind", sa.String(length=32), nullable=True),
        sa.Column("owner_node_id", GUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_foreign_key(
        "fk_dashboards_owner_node",
        "dashboards",
        "nodes",
        ["owner_node_id"],
        ["id"],
        ondelete="SET NULL",
        use_alter=True,
    )

    op.add_column(
        "tracks",
        sa.Column("dashboard_id", GUID(), sa.ForeignKey("dashboards.id", ondelete="CASCADE"), nullable=True),
    )
    op.add_column(
        "nodes",
        sa.Column("subgraph_dashboard_id", GUID(), sa.ForeignKey("dashboards.id", ondelete="CASCADE"), nullable=True),
    )
    # Every ordered_tracks() call filters on this pair, and it runs on any grid
    # read plus every span/placement check.
    op.create_index("ix_tracks_project_dashboard", "tracks", ["project_id", "dashboard_id"])


def downgrade() -> None:
    op.drop_index("ix_tracks_project_dashboard", table_name="tracks")
    op.drop_column("nodes", "subgraph_dashboard_id")
    op.drop_column("tracks", "dashboard_id")
    op.drop_constraint("fk_dashboards_owner_node", "dashboards", type_="foreignkey")
    op.drop_table("dashboards")
