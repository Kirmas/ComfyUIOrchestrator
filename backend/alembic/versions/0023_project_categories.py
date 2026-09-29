"""project categories + a hand-picked project preview

Revision ID: 0023
Revises: 0022
Create Date: 2026-09-30

The project dropdown became a projects page: folders (nesting through
parent_id) and a card per project. Purely additive -- every existing project
lands at the top level with a random preview until someone files it away.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.db.types import GUID

revision: str = "0023"
down_revision: Union[str, None] = "0022"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "project_categories",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column(
            "parent_id", GUID(), sa.ForeignKey("project_categories.id", ondelete="RESTRICT"), nullable=True
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.add_column(
        "projects",
        sa.Column(
            "category_id", GUID(), sa.ForeignKey("project_categories.id", ondelete="RESTRICT"), nullable=True
        ),
    )
    op.add_column("projects", sa.Column("preview_asset_id", GUID(), nullable=True))
    op.create_foreign_key(
        "fk_projects_preview_asset_id",
        "projects",
        "assets",
        ["preview_asset_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_projects_preview_asset_id", "projects", type_="foreignkey")
    op.drop_column("projects", "preview_asset_id")
    op.drop_column("projects", "category_id")
    op.drop_table("project_categories")
