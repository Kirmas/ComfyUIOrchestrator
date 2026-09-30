"""design docs become their own entity, owned by a project or a folder

Revision ID: 0026
Revises: 0025
Create Date: 2026-09-30

Until now a doc *was* a (project, lang) row. Some docs aren't about one
project (a world's lore), so a doc is now a row of its own -- owned by a
project, or by a folder as a "global" doc -- with its languages in
design_doc_texts. Every existing project doc carries over unchanged.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.db.types import GUID

revision: str = "0026"
down_revision: Union[str, None] = "0025"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.rename_table("design_docs", "design_docs_0025")
    op.execute("ALTER TABLE design_docs_0025 RENAME CONSTRAINT design_docs_pkey TO design_docs_0025_pkey")
    op.create_table(
        "design_docs",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("title", sa.String(length=255), nullable=False, server_default=""),
        sa.Column(
            "project_id", GUID(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, unique=True
        ),
        sa.Column(
            "category_id", GUID(), sa.ForeignKey("project_categories.id", ondelete="RESTRICT"), nullable=True
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_table(
        "design_doc_texts",
        sa.Column("doc_id", GUID(), sa.ForeignKey("design_docs.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("lang", sa.String(length=8), primary_key=True),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.execute(
        "INSERT INTO design_docs (id, title, project_id) "
        "SELECT gen_random_uuid(), '', project_id FROM design_docs_0025 GROUP BY project_id"
    )
    op.execute(
        "INSERT INTO design_doc_texts (doc_id, lang, content, updated_at) "
        "SELECT d.id, o.lang, o.content, o.updated_at FROM design_docs_0025 o "
        "JOIN design_docs d ON d.project_id = o.project_id"
    )
    op.drop_table("design_docs_0025")


def downgrade() -> None:
    # Global docs have nowhere to go in the old shape and are dropped.
    op.create_table(
        "design_docs_0025",
        sa.Column("project_id", GUID(), sa.ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("lang", sa.String(length=8), primary_key=True),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.execute(
        "INSERT INTO design_docs_0025 (project_id, lang, content, updated_at) "
        "SELECT d.project_id, t.lang, t.content, t.updated_at FROM design_doc_texts t "
        "JOIN design_docs d ON d.id = t.doc_id WHERE d.project_id IS NOT NULL"
    )
    op.drop_table("design_doc_texts")
    op.drop_table("design_docs")
    op.rename_table("design_docs_0025", "design_docs")
    op.execute("ALTER TABLE design_docs RENAME CONSTRAINT design_docs_0025_pkey TO design_docs_pkey")
