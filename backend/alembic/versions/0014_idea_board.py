"""idea board: boards + board_items, project-scoped assets

Revision ID: 0014
Revises: 0013
Create Date: 2026-07-29

roadmap.md §1. Three things:

1. assets.project_id -- a library asset that no grid cell owns. assets.node_id
   was already nullable, so this is additive; nothing existing changes owner.
   The two are alternatives, never both: node_id cascades on cell deletion, so
   a board image also owned by a cell would disappear from the board when that
   cell was deleted.
2. assets.tags -- labels for the flat, filterable presentation of the same
   library the board shows by position.
3. boards / board_items -- the stickers themselves.

board_items carries two self-referential FKs (connector endpoints, comment
anchor). They're created with use_alter so the table can reference itself, and
they cascade so deleting a sticker takes its connectors and comments with it.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.db.types import GUID, JSONVariant

revision: str = "0014"
down_revision: Union[str, None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "assets",
        sa.Column("project_id", GUID(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=True),
    )
    # JSONVariant is an instance, not a class (db/types.py) -- matching 0001's usage.
    op.add_column("assets", sa.Column("tags", JSONVariant, nullable=False, server_default="[]"))

    op.create_table(
        "boards",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("project_id", GUID(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False, server_default="Ideas"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "board_items",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("board_id", GUID(), sa.ForeignKey("boards.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("x", sa.Float(), nullable=False, server_default="0"),
        sa.Column("y", sa.Float(), nullable=False, server_default="0"),
        sa.Column("w", sa.Float(), nullable=False, server_default="220"),
        sa.Column("h", sa.Float(), nullable=False, server_default="180"),
        sa.Column("z", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("color", sa.String(32), nullable=True),
        sa.Column("text", sa.Text(), nullable=False, server_default=""),
        sa.Column("tag", sa.String(64), nullable=True),
        sa.Column("asset_id", GUID(), sa.ForeignKey("assets.id", ondelete="CASCADE"), nullable=True),
        sa.Column("shape", sa.String(16), nullable=True),
        sa.Column("path", sa.Text(), nullable=True),
        sa.Column("stroke_width", sa.Float(), nullable=True),
        sa.Column("source_item_id", GUID(), nullable=True),
        sa.Column("target_item_id", GUID(), nullable=True),
        sa.Column("source", sa.String(16), nullable=False, server_default="user"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("board_id", "tag", name="uq_board_items_board_tag"),
    )
    op.create_foreign_key(
        "fk_board_items_source_item", "board_items", "board_items", ["source_item_id"], ["id"], ondelete="CASCADE"
    )
    op.create_foreign_key(
        "fk_board_items_target_item", "board_items", "board_items", ["target_item_id"], ["id"], ondelete="CASCADE"
    )


def downgrade() -> None:
    op.drop_constraint("fk_board_items_target_item", "board_items", type_="foreignkey")
    op.drop_constraint("fk_board_items_source_item", "board_items", type_="foreignkey")
    op.drop_table("board_items")
    op.drop_table("boards")
    op.drop_column("assets", "tags")
    op.drop_column("assets", "project_id")
