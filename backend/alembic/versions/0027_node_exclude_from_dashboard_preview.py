"""nodes.exclude_from_dashboard_preview -- opt out of a container's collage

Revision ID: 0027
Revises: 0026
Create Date: 2026-10-01

False everywhere on upgrade (the only value any node had before this column
existed) means "contribute normally", unchanged behavior. Set from the node's
own card to keep "service" content (e.g. a landmark's own prop sub-dashboards)
out of a parent container dashboard's collage preview -- see
routes/dashboards.py's _walk_container and the column's own docstring in
db/models.py.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0027"
down_revision: Union[str, None] = "0026"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "nodes", sa.Column("exclude_from_dashboard_preview", sa.Boolean(), nullable=False, server_default=sa.false())
    )


def downgrade() -> None:
    op.drop_column("nodes", "exclude_from_dashboard_preview")
