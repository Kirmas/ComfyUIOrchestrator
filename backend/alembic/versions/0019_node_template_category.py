"""node_templates.category_override -- manual label for the picker's sub-groups

Revision ID: 0019
Revises: 0018
Create Date: 2026-08-24

The picker groups node types by the family of the models their capabilities
load (core/node_category.py), derived fresh on every read rather than stored,
so re-pointing a type at another checkpoint re-groups it with no write here.
This column only holds the exception: a hand-picked label that overrides that
derivation. NULL everywhere on upgrade, which is exactly "derive it" -- so no
data migration, and every existing type keeps the label it would have had.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: Union[str, None] = "0018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("node_templates", sa.Column("category_override", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("node_templates", "category_override")
