"""Drop nodes.is_picker and nodes.template_id -- write-only leftovers

Revision ID: 0017
Revises: 0016
Create Date: 2026-08-08

Both columns were mirrored from nodes.node_type on every write (by a
core/node_types.sync_legacy_fields helper, also removed) and read by nothing:
"is this an undecided candidates picker" is is_picker_type(node_type) in
core/asset_types.py, and "which template does this node use" is
resolve_effective_template. Verified before writing this: every reference in
the backend, the frontend and the MCP tools was a write, a schema echo or a
comment -- there was no reader left on either side.

The code that stopped writing them shipped in the deploy BEFORE this
migration, deliberately. That ordering matters both ways round:

  * new code + old schema is fine -- is_picker is NOT NULL but carries a
    server default of false, so an INSERT that omits it still works, and
    template_id is nullable;
  * old code + new schema would NOT be fine -- a process still selecting or
    inserting these columns errors the moment they are gone.

deploy.sh runs `alembic upgrade head` before restarting the unit, so doing
both in one deploy would leave exactly that bad window open for a few
seconds. Two deploys removes it entirely.

Irreversible in practice: downgrade() re-creates the columns and the FK, but
the per-row values are gone and there is nothing left that could recompute
them -- node_type is the only source, which is the whole reason these two
went away. Take a backup first (deploy/deploy.sh backup).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.db.types import GUID

revision: str = "0017"
down_revision: Union[str, None] = "0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column("nodes", "is_picker")
    # Named explicitly: dropping the column takes its FK with it, but the
    # constraint name is what a partial/failed run would leave behind.
    op.drop_column("nodes", "template_id")


def downgrade() -> None:
    op.add_column("nodes", sa.Column("is_picker", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("nodes", sa.Column("template_id", GUID(), nullable=True))
    op.create_foreign_key(
        "fk_nodes_template_id", "nodes", "node_templates", ["template_id"], ["id"], ondelete="SET NULL"
    )
