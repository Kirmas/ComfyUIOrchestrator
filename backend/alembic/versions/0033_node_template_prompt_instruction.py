"""node_templates.prompt_instruction -- optional prompt guide for agents

Revision ID: 0033
Revises: 0032
Create Date: 2026-10-29

Optional text field on node_templates describing prompt format, syntax,
and guidelines for LLM agents when authoring prompts for this node type.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0033"
down_revision: Union[str, None] = "0032"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("node_templates", sa.Column("prompt_instruction", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("node_templates", "prompt_instruction")
