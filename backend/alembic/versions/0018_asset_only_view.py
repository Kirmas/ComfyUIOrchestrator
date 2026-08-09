"""projects.asset_only_view / dashboards.asset_only_view -- per-scope display toggle

Revision ID: 0018
Revises: 0017
Create Date: 2026-08-09

Purely a frontend rendering preference (Grid.tsx hides workflow columns when
set) -- the backend's only job is remembering it per grid scope, same
(project main grid / dashboard sub-grid) split start_kind already uses
(grid_scope.py). Nothing server-side reads this value; step/track math is
untouched, so a stale or wrong value can never corrupt layout the way a wrong
start_kind could.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: Union[str, None] = "0017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("projects", sa.Column("asset_only_view", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("dashboards", sa.Column("asset_only_view", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    op.drop_column("dashboards", "asset_only_view")
    op.drop_column("projects", "asset_only_view")
