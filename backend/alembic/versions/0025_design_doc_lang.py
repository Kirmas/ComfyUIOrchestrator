"""design docs: one per project *and language* (uk/en)

Revision ID: 0025
Revises: 0024
Create Date: 2026-09-30

0024 keyed the doc by project alone; a project needs a Ukrainian and an
English version side by side. Any doc written in between becomes the "uk" one.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0025"
down_revision: Union[str, None] = "0024"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("design_docs", sa.Column("lang", sa.String(length=8), nullable=False, server_default="uk"))
    op.alter_column("design_docs", "lang", server_default=None)
    op.drop_constraint("design_docs_pkey", "design_docs", type_="primary")
    op.create_primary_key("design_docs_pkey", "design_docs", ["project_id", "lang"])


def downgrade() -> None:
    op.execute("DELETE FROM design_docs WHERE lang <> 'uk'")
    op.drop_constraint("design_docs_pkey", "design_docs", type_="primary")
    op.create_primary_key("design_docs_pkey", "design_docs", ["project_id"])
    op.drop_column("design_docs", "lang")
