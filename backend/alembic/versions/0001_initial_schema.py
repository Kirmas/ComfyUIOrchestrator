"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-07-12

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.db.types import GUID, JSONVariant

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    is_postgres = op.get_bind().dialect.name == "postgresql"

    op.create_table(
        "backends",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("base_url", sa.String(1024), nullable=True),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_stats", JSONVariant, nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "capabilities",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("backend_id", GUID(), sa.ForeignKey("backends.id", ondelete="CASCADE"), nullable=False),
        sa.Column("node_type_slug", sa.String(128), nullable=False),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("execution_type", sa.String(32), nullable=False),
        sa.Column("config", JSONVariant, nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_capabilities_node_type_slug", "capabilities", ["node_type_slug"])

    op.create_table(
        "node_templates",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("node_type_slug", sa.String(128), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("param_schema", JSONVariant, nullable=False, server_default="{}"),
        sa.Column("defaults", JSONVariant, nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "projects",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("start_kind", sa.String(32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "api_key_permissions",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("provider", sa.String(128), nullable=False),
        sa.Column("node_type_slug", sa.String(128), nullable=False),
        sa.Column("api_key", sa.String(1024), nullable=False),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # tracks / nodes / assets have a circular reference (tracks -> nodes -> tracks,
    # tracks -> assets -> nodes). On Postgres the two "spawned_from_*" FKs on tracks
    # are added with ALTER TABLE once all three tables exist. SQLite has no ALTER
    # TABLE ADD CONSTRAINT at all, so on that dialect the columns are left as plain
    # nullable references (enforced at the application layer instead).
    op.create_table(
        "tracks",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("project_id", GUID(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("row_index", sa.Integer, nullable=False),
        sa.Column("spawned_from_node_id", GUID(), nullable=True),
        sa.Column("spawned_from_output_id", GUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "nodes",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("track_id", GUID(), sa.ForeignKey("tracks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("step_index", sa.Integer, nullable=False),
        sa.Column("kind", sa.String(32), nullable=False, server_default="workflow"),
        sa.Column("template_id", GUID(), sa.ForeignKey("node_templates.id", ondelete="SET NULL"), nullable=True),
        sa.Column("inputs", JSONVariant, nullable=False, server_default="[]"),
        sa.Column("params", JSONVariant, nullable=False, server_default="{}"),
        sa.Column("status", sa.String(32), nullable=False, server_default="draft"),
        sa.Column("backend_used_id", GUID(), sa.ForeignKey("backends.id", ondelete="SET NULL"), nullable=True),
        sa.Column("requested_variants", sa.Integer, nullable=False, server_default="1"),
        sa.Column("backend_mode", sa.String(32), nullable=False, server_default="auto"),
        sa.Column("manual_backend_id", GUID(), sa.ForeignKey("backends.id", ondelete="SET NULL"), nullable=True),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_nodes_track_id", "nodes", ["track_id"])

    op.create_table(
        "assets",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("node_id", GUID(), sa.ForeignKey("nodes.id", ondelete="CASCADE"), nullable=True),
        sa.Column("storage_key", sa.String(1024), nullable=False),
        sa.Column("mime_type", sa.String(128), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False, server_default="image"),
        sa.Column("selected", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("meta", JSONVariant, nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_assets_node_id", "assets", ["node_id"])

    op.create_table(
        "jobs",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("node_id", GUID(), sa.ForeignKey("nodes.id", ondelete="CASCADE"), nullable=False),
        sa.Column("backend_id", GUID(), sa.ForeignKey("backends.id", ondelete="SET NULL"), nullable=True),
        sa.Column("variant_index", sa.Integer, nullable=False, server_default="0"),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("external_job_id", sa.String(255), nullable=True),
        sa.Column("retries", sa.Integer, nullable=False, server_default="0"),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("progress", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_jobs_node_id", "jobs", ["node_id"])

    if is_postgres:
        op.create_foreign_key(
            "fk_tracks_spawned_from_node", "tracks", "nodes", ["spawned_from_node_id"], ["id"], ondelete="SET NULL"
        )
        op.create_foreign_key(
            "fk_tracks_spawned_from_output", "tracks", "assets", ["spawned_from_output_id"], ["id"], ondelete="SET NULL"
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.drop_constraint("fk_tracks_spawned_from_output", "tracks", type_="foreignkey")
        op.drop_constraint("fk_tracks_spawned_from_node", "tracks", type_="foreignkey")
    op.drop_table("jobs")
    op.drop_table("assets")
    op.drop_table("nodes")
    op.drop_table("tracks")
    op.drop_table("api_key_permissions")
    op.drop_table("projects")
    op.drop_table("node_templates")
    op.drop_table("capabilities")
    op.drop_table("backends")
