import enum
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.types import GUID, JSONVariant


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(GUID(), primary_key=True, default=uuid.uuid4)


class BackendKind(str, enum.Enum):
    comfyui = "comfyui"
    api_provider = "api_provider"
    native = "native"  # runs in this process, no remote backend at all -- see core/native_backend.py


class ExecutionType(str, enum.Enum):
    comfyui_workflow = "comfyui_workflow"
    api_call = "api_call"
    native = "native"


class NodeKind(str, enum.Enum):
    asset = "asset"  # a set of N selectable asset "lines" -- uploaded or produced by a workflow node
    workflow = "workflow"  # a ComfyUI workflow / API call; its result materializes as a following asset node


class NodeStatus(str, enum.Enum):
    draft = "draft"
    queued = "queued"
    running = "running"
    done = "done"
    error = "error"
    discarded = "discarded"


class JobStatusEnum(str, enum.Enum):
    pending = "pending"
    waiting_for_backend = "waiting_for_backend"
    running = "running"
    done = "done"
    error = "error"
    cancelled = "cancelled"


class AssetKind(str, enum.Enum):
    image = "image"
    mesh = "mesh"
    other = "other"


class Backend(Base):
    __tablename__ = "backends"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[BackendKind] = mapped_column(String(32), nullable=False)
    base_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_stats: Mapped[dict] = mapped_column(JSONVariant, default=dict, nullable=False)
    # api_provider kind only -- one key per Backend row, not per node type:
    # a Capability just points its backend_id at whichever api_provider
    # Backend it wants to use, so any number of node types can share the
    # same key. Wanting a second key means adding a second api_provider
    # Backend, not a second grant on the same one. `provider` is the
    # PROVIDERS registry key (api_backend.py), e.g. "nano_banana".
    provider: Mapped[str | None] = mapped_column(String(128), nullable=True)
    api_key: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    # Max successful api_call jobs in the trailing 24h across every node type
    # that shares this backend's key -- NULL means unlimited. See
    # api_usage_log and dispatcher._backend_within_quota.
    daily_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    capabilities: Mapped[list["Capability"]] = relationship(back_populates="backend", cascade="all, delete-orphan")


class Capability(Base):
    __tablename__ = "capabilities"

    id: Mapped[uuid.UUID] = _uuid_pk()
    backend_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("backends.id", ondelete="CASCADE"), nullable=False)
    node_type_slug: Mapped[str] = mapped_column(String(128), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    execution_type: Mapped[ExecutionType] = mapped_column(String(32), nullable=False)
    config: Mapped[dict] = mapped_column(JSONVariant, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    backend: Mapped["Backend"] = relationship(back_populates="capabilities")


class NodeTemplate(Base):
    __tablename__ = "node_templates"

    id: Mapped[uuid.UUID] = _uuid_pk()
    # Stable identifier used by Node.node_type's "template.<slug>" form -- must
    # be unique for that to unambiguously resolve (enforced at the DB level,
    # see migration 0003).
    node_type_slug: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    param_schema: Mapped[dict] = mapped_column(JSONVariant, default=dict, nullable=False)
    defaults: Mapped[dict] = mapped_column(JSONVariant, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # Column kind (asset/workflow) is a project-wide, position-based pattern, not
    # a per-node choice: whichever kind the very first node in the project is
    # given fixes column 0's kind, and it strictly alternates from there. Null
    # until that first node exists. See nodes.py's create_node.
    start_kind: Mapped[NodeKind | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    tracks: Mapped[list["Track"]] = relationship(back_populates="project", cascade="all, delete-orphan")


class Track(Base):
    __tablename__ = "tracks"

    id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    row_index: Mapped[int] = mapped_column(Integer, nullable=False)
    spawned_from_node_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("nodes.id", ondelete="SET NULL", use_alter=True, name="fk_tracks_spawned_from_node"),
        nullable=True,
    )
    spawned_from_output_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(),
        ForeignKey("assets.id", ondelete="SET NULL", use_alter=True, name="fk_tracks_spawned_from_output"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    project: Mapped["Project"] = relationship(back_populates="tracks")
    nodes: Mapped[list["Node"]] = relationship(
        back_populates="track", cascade="all, delete-orphan", foreign_keys="Node.track_id"
    )


class Node(Base):
    __tablename__ = "nodes"

    id: Mapped[uuid.UUID] = _uuid_pk()
    track_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tracks.id", ondelete="CASCADE"), nullable=False)
    step_index: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[NodeKind] = mapped_column(String(32), default=NodeKind.workflow, nullable=False)
    # Namespaced discriminator -- "asset.select" / "asset.single" / "native.<slug>"
    # / "template.<slug>" -- the authoritative answer to "what specific flavor of
    # node is this" (see core/node_types.py and memory/node_model_refactor_plan.md).
    # "asset"/"native" are resolved via a code registry, no DB row; "template" is
    # resolved via node_templates.node_type_slug. NULL only transiently, for a
    # freshly-created workflow cell that hasn't picked a template yet.
    node_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Legacy columns -- kept mirrored/derived from node_type on every write (see
    # core/node_types.sync_legacy_fields) as a safety net, not read as
    # authoritative by new code anymore.
    # Explicit, persistent marker for an asset-kind node holding raw, not-yet-
    # resolved generation output (set by _get_or_create_output_asset_node in
    # worker/tasks.py) -- NodeCell.tsx uses this, not the current output count
    # or sibling lookups, to decide whether to force a spawn/keep/discard
    # choice on every image. Flips to False once the user resolves it via
    # PATCH /api/nodes/{id} with is_picker: false (keep one in place) or once
    # it empties out entirely (spawn/discard draining it to zero, see NodeCell.tsx).
    is_picker: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    template_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("node_templates.id", ondelete="SET NULL"), nullable=True
    )
    inputs: Mapped[list] = mapped_column(JSONVariant, default=list, nullable=False)
    params: Mapped[dict] = mapped_column(JSONVariant, default=dict, nullable=False)
    status: Mapped[NodeStatus] = mapped_column(String(32), default=NodeStatus.draft, nullable=False)
    backend_used_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("backends.id", ondelete="SET NULL"), nullable=True
    )
    requested_variants: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    backend_mode: Mapped[str] = mapped_column(String(32), default="auto", nullable=False)
    manual_backend_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("backends.id", ondelete="SET NULL"), nullable=True
    )
    # Explicit opt-in gate for paid api_call capabilities, independent of
    # backend_mode -- "auto" (and even "api_only"/"manual" pointed at an
    # api_provider backend) never make a paid call unless this is also True.
    # Defaults False so a node never starts spending money by accident; see
    # dispatcher.eligible_capabilities.
    use_api: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Set exactly once, by _get_or_create_output_asset_node (worker/tasks.py)
    # when it materializes a workflow's result as a following asset node --
    # never written anywhere else, never changed afterward. NULL for every
    # other asset (manual upload, "+ asset", RefAsset, the settled node
    # onSelectCandidate creates fresh in the vacated cell): those have no
    # creator and stay freely repositionable. A non-NULL value rigidly binds
    # the asset to that one workflow node's own output position -- see
    # Grid.tsx's isPositionAllowedFor and api/routes/nodes.py's
    # _ensure_output_binding, which both derive "allowed positions" as
    # exactly the creator's own home track plus any track spawned from it,
    # at the creator's step_index + 1. Not exposed on NodeCreate/NodeUpdate
    # (see schemas.py) -- there is no API path that sets or moves this value
    # except that one backend call site.
    created_by_node_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("nodes.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    track: Mapped["Track"] = relationship(back_populates="nodes", foreign_keys=[track_id])
    outputs: Mapped[list["Asset"]] = relationship(
        back_populates="node", cascade="all, delete-orphan", foreign_keys="Asset.node_id"
    )
    jobs: Mapped[list["Job"]] = relationship(back_populates="node", cascade="all, delete-orphan")


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    node_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("nodes.id", ondelete="CASCADE"), nullable=False)
    backend_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("backends.id", ondelete="SET NULL"), nullable=True)
    variant_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[JobStatusEnum] = mapped_column(String(32), default=JobStatusEnum.pending, nullable=False)
    external_job_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    retries: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Annotated float but backed by an Integer column on purpose -- every
    # writer (worker/tasks.py's on_progress) only ever assigns whole percents
    # (0-100); the wider Python type is just so callers doing pct math don't
    # need an explicit int() cast, not a hint that fractional values persist.
    progress: Mapped[float] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    node: Mapped["Node"] = relationship(back_populates="jobs")


class Asset(Base):
    __tablename__ = "assets"

    id: Mapped[uuid.UUID] = _uuid_pk()
    node_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("nodes.id", ondelete="CASCADE"), nullable=True)
    storage_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[AssetKind] = mapped_column(String(32), default=AssetKind.image, nullable=False)
    selected: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    meta: Mapped[dict] = mapped_column(JSONVariant, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    node: Mapped["Node"] = relationship(back_populates="outputs", foreign_keys=[node_id])


class ApiUsageLog(Base):
    """One row per successful paid API call (worker/tasks.py's run_variant_job,
    right after _materialize_job_result succeeds for an api_call capability) --
    a rolling COUNT(*) over the trailing 24h against this table is
    Backend.daily_limit's enforcement, chosen over a mutable used_today/
    reset_at counter to sidestep day-rollover races between concurrent
    workers, and it gets a spend history for free."""

    __tablename__ = "api_usage_log"

    id: Mapped[uuid.UUID] = _uuid_pk()
    backend_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("backends.id", ondelete="CASCADE"), nullable=False)
    node_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("nodes.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
