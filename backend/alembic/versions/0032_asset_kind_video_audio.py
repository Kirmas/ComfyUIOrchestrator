"""assets.kind: video / audio -- reclassify rows stored as 'other'

Revision ID: 0032
Revises: 0031
Create Date: 2026-10-29

AssetKind grew `video` and `audio` (db/models.py). The column is a plain
String, so there is no type to alter -- only the rows the board (and chat
uploads) stored as `other` while no such kind existed, decided by the
mime_type they were saved with. Their files carry no prefix block yet; the
poster frame / duration descriptor comes from
`scripts/asset_prefix.py --backfill`, not from here (a migration must not
decode media).

Downgrade folds them back into `other`, which is exactly what they were.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0032"
down_revision: Union[str, None] = "0031"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("UPDATE assets SET kind = 'video' WHERE kind = 'other' AND mime_type LIKE 'video/%'")
    op.execute("UPDATE assets SET kind = 'audio' WHERE kind = 'other' AND mime_type LIKE 'audio/%'")


def downgrade() -> None:
    op.execute("UPDATE assets SET kind = 'other' WHERE kind IN ('video', 'audio')")
