"""comment threads: annotation_messages + resolved state

Revision ID: 0022
Revises: 0021
Create Date: 2026-09-27

A comment block used to be a single text field. That made a conversation on a
cell impossible (see db/models.py's Annotation docstring): the agent could only
add frames, a person could only overwrite text, so the person's replies went
into the agent's own frames -- which still said source=agent -- and every
"done" became another frame stacked on the same cell.

What was said moves into annotation_messages, one row per message, each with
its own author. Existing data is converted, not dropped:

* Every annotation's text becomes one message, dated when that text was last
  written (updated_at).
* Its author is the annotation's source -- except an agent frame whose text
  was edited after it was created. The MCP tools never had a way to edit, so
  that edit was a person typing over it in the UI: the words are theirs.
* Frames on exactly the same set of cells are merged into one thread, oldest
  first. That is the model from here on (one thread per member set), and on
  the data this was written against it reassembles the actual back-and-forth
  -- agent "in progress", the person's feedback typed over a later frame,
  agent "done" -- which is what those stacked frames were.
* Everything starts open.

annotations.text/source are dropped in the same transaction. The old process
keeps running until the restart that follows `alembic upgrade head`, so for a
few seconds its comment requests fail -- loudly, which is the point: the DDL
locks the table before the rows are read, so nothing can land in the old
column after being converted and then vanish with it.

downgrade() rebuilds text/source from the messages (joined, first author), but
cannot split merged threads back apart.
"""
import uuid
from collections import defaultdict
from datetime import timedelta
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.db.types import GUID

revision: str = "0022"
down_revision: Union[str, None] = "0021"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# An agent frame whose updated_at trails created_at by more than this was
# edited, i.e. typed over by a person. Unedited ones match to the microsecond.
_EDIT_SLACK = timedelta(seconds=1)

_messages = sa.table(
    "annotation_messages",
    sa.column("id", GUID()),
    sa.column("annotation_id", GUID()),
    sa.column("source", sa.String(16)),
    sa.column("text", sa.Text()),
    sa.column("created_at", sa.DateTime(timezone=True)),
    sa.column("updated_at", sa.DateTime(timezone=True)),
)


def upgrade() -> None:
    op.add_column("annotations", sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("annotations", sa.Column("resolved_by", sa.String(16), nullable=True))
    op.create_table(
        "annotation_messages",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("annotation_id", GUID(), sa.ForeignKey("annotations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_annotation_messages_annotation_id", "annotation_messages", ["annotation_id"])

    conn = op.get_bind()
    members: dict = defaultdict(set)
    for annotation_id, node_id in conn.execute(sa.text("SELECT annotation_id, node_id FROM annotation_nodes")):
        members[annotation_id].add(node_id)

    rows = conn.execute(
        sa.text("SELECT id, project_id, text, source, created_at, updated_at FROM annotations ORDER BY created_at, id")
    ).fetchall()

    thread_of: dict = {}
    new_messages = []
    merged_away = []
    for annotation_id, project_id, text, source, created_at, updated_at in rows:
        cells = frozenset(members[annotation_id])
        # A frame with no members left is an orphan the list route deletes on
        # its next read -- never merge those, they'd all share the empty set.
        kept = thread_of.setdefault((project_id, cells), annotation_id) if cells else annotation_id
        if kept != annotation_id:
            merged_away.append(annotation_id)

        typed_over = source == "agent" and updated_at is not None and created_at is not None and updated_at - created_at > _EDIT_SLACK
        written_at = updated_at or created_at
        if (text or "").strip():
            new_messages.append(
                {
                    "id": uuid.uuid4(),
                    "annotation_id": kept,
                    "source": "user" if typed_over else source,
                    "text": text,
                    "created_at": written_at,
                    "updated_at": written_at,
                }
            )

    if new_messages:
        op.bulk_insert(_messages, new_messages)
    for annotation_id in merged_away:
        # Same member set as the thread it merged into, so no membership is lost.
        conn.execute(sa.text("DELETE FROM annotation_nodes WHERE annotation_id = :id"), {"id": annotation_id})
        conn.execute(sa.text("DELETE FROM annotations WHERE id = :id"), {"id": annotation_id})

    op.drop_column("annotations", "text")
    op.drop_column("annotations", "source")


def downgrade() -> None:
    op.add_column("annotations", sa.Column("text", sa.Text(), nullable=False, server_default=""))
    op.add_column("annotations", sa.Column("source", sa.String(16), nullable=False, server_default="user"))

    conn = op.get_bind()
    rows = conn.execute(
        sa.text("SELECT annotation_id, source, text FROM annotation_messages ORDER BY annotation_id, created_at")
    ).fetchall()
    threads: dict = defaultdict(list)
    for annotation_id, source, text in rows:
        threads[annotation_id].append((source, text))
    for annotation_id, messages in threads.items():
        conn.execute(
            sa.text("UPDATE annotations SET text = :text, source = :source WHERE id = :id"),
            {"text": "\n\n".join(text for _, text in messages), "source": messages[0][0], "id": annotation_id},
        )

    op.drop_index("ix_annotation_messages_annotation_id", table_name="annotation_messages")
    op.drop_table("annotation_messages")
    op.drop_column("annotations", "resolved_by")
    op.drop_column("annotations", "resolved_at")
