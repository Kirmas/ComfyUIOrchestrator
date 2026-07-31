"""dashboards.result_asset_id -- the asset a subgraph shows as its face

Revision ID: 0016
Revises: 0015
Create Date: 2026-07-31

A subgraph's smart pointer renders like an asset cell, but nothing set which
asset that was. It belongs to the dashboard rather than to the pointer so that
two pointers into the same subgraph can't show different pictures.

SET NULL rather than CASCADE: deleting the asset should blank the face and let
the user pick another, not destroy the dashboard.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.db.types import GUID

revision: str = "0016"
down_revision: Union[str, None] = "0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("dashboards", sa.Column("result_asset_id", GUID(), nullable=True))
    op.create_foreign_key(
        "fk_dashboards_result_asset", "dashboards", "assets", ["result_asset_id"], ["id"], ondelete="SET NULL"
    )


def downgrade() -> None:
    op.drop_constraint("fk_dashboards_result_asset", "dashboards", type_="foreignkey")
    op.drop_column("dashboards", "result_asset_id")
