"""jobs.progress_step / jobs.progress_max -- raw ComfyUI step counters

Revision ID: 0020
Revises: 0019
Create Date: 2026-09-19

Job.progress already collapses ComfyUI's own "progress" ws message
(data["value"]/["max"]) into a single 0-100 percent -- these two new
columns keep the raw pair too, so the frontend can show "23/40" and derive
a remaining-time estimate from elapsed-so-far, instead of a bare percent
with no step count and no ETA. NULL everywhere on upgrade (nothing to
backfill -- a finished job's step trace was never persisted, only its last
computed percent) and NULL for any job that never got a comfyui "progress"
message at all (api_call backends, or a job that errored before its first
one).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: Union[str, None] = "0019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("progress_step", sa.Integer(), nullable=True))
    op.add_column("jobs", sa.Column("progress_max", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("jobs", "progress_max")
    op.drop_column("jobs", "progress_step")
