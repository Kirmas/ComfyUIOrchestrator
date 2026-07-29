import enum
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, func
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
    # Per-project ordering is a doubly-linked list, NOT a dense row_index
    # anymore (migration 0010). The visible "track N" number is derived from
    # position in this list at render time (frontend) and never stored, so it
    # can no longer gap or desync the way a reindexed integer column did --
    # deleting/inserting a track is now a pointer splice (2 writes), never a
    # bulk renumber of every track below it (the non-atomic renumber was the
    # 2026-07-21 data-loss surface). prev/next are NULL at the two ends.
    # core/track_order.py's ordered_tracks()/unlink_track()/splice_after() are
    # the only code that should read or mutate these.
    prev_track_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tracks.id", ondelete="SET NULL", use_alter=True, name="fk_tracks_prev_track"),
        nullable=True,
    )
    next_track_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tracks.id", ondelete="SET NULL", use_alter=True, name="fk_tracks_next_track"),
        nullable=True,
    )
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
    # Set only via POST /api/nodes/{id}/collapse|expand (api/routes/nodes.py),
    # never through generic PATCH -- lives on the pass-through asset node of a
    # workflow -> asset -> workflow chain (this asset's own created_by_node_id
    # is that chain's first workflow; this column, once set, points at the
    # second). A non-NULL value means: fold the 3-cell chain into one card in
    # the UI (NodeCell.tsx), and both of those two workflow nodes are locked
    # (no generate/reroll/discard -- see _reject_if_locked) since collapsing
    # is meant for finished history the user doesn't intend to regenerate.
    collapse_target_id: Mapped[uuid.UUID | None] = mapped_column(
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
    # Set instead of node_id for a project-scoped library asset -- one that no
    # grid cell owns, uploaded straight onto the idea board (see Board below).
    # Exactly one of the two is set in practice: node_id for generated/uploaded
    # cell output, project_id for board media. They are deliberately NOT both
    # set on one row: node_id cascades on cell deletion, so a board image that
    # a cell also owned would vanish from the board the moment that cell was
    # deleted. The grid only ever *references* library assets (asset.refasset),
    # never owns them, which is why there is no "send this output to the board"
    # direction at all (roadmap.md §1).
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=True
    )
    storage_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[AssetKind] = mapped_column(String(32), default=AssetKind.image, nullable=False)
    selected: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Free-form labels, only meaningful for project-scoped library assets: the
    # board shows them by position, the grid's "з референсів" picker shows the
    # same assets as a flat filterable list (one storage, two presentations --
    # roadmap.md §1). Filtering happens in Python, not SQL: a project's library
    # is hundreds of rows at most, and a JSON containment predicate that works
    # on both Postgres and the SQLite dev fallback isn't worth writing.
    tags: Mapped[list] = mapped_column(JSONVariant, default=list, nullable=False)
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


class AnnotationSource(str, enum.Enum):
    user = "user"
    agent = "agent"


class Annotation(Base):
    """A comment block: free text attached to a set of nodes, drawn as a frame
    around them in the grid.

    Deliberately stores no coordinates. The frame's box is derived from where
    its member nodes currently are, so moving a node moves the frame with it --
    the same rule the grid already follows for nodes themselves (a node's
    position is always exactly its track_id + step_index, never a stored
    display-only override). Storing a rect here would reintroduce exactly the
    kind of position that can silently desync from the content it describes.

    An agent flagging an ambiguous cell (the MCP flag_cell tool) creates one of
    these with source=agent and a single member, so agent flags and hand-written
    notes are the same object and are reviewed in the same place.
    """

    __tablename__ = "annotations"

    id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    source: Mapped[AnnotationSource] = mapped_column(String(16), default=AnnotationSource.user, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    members: Mapped[list["AnnotationNode"]] = relationship(back_populates="annotation", cascade="all, delete-orphan")


class AnnotationNode(Base):
    """Membership of one node in one annotation. Both FKs cascade: deleting a
    node drops it out of any frame it was in (leaving the frame around the
    remaining members), and deleting the annotation drops all its rows."""

    __tablename__ = "annotation_nodes"

    annotation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("annotations.id", ondelete="CASCADE"), primary_key=True
    )
    node_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("nodes.id", ondelete="CASCADE"), primary_key=True)

    annotation: Mapped["Annotation"] = relationship(back_populates="members")


class BoardItemKind(str, enum.Enum):
    """What one sticker *is*. A sticker holds exactly one kind of content --
    there is no mixed card -- so this doubles as the content-type discriminator
    (roadmap.md §1)."""

    text = "text"  # markdown body, optionally tagged for prompt macros
    image = "image"
    audio = "audio"
    video = "video"  # no 3D on purpose: a model-viewer per sticker would sink the board
    frame = "frame"  # the lasso drawn around a group; rect or ellipse, see BoardItem.shape
    ink = "ink"  # freehand stroke, no semantics at all
    connector = "connector"  # source_item -> target_item, anchored to the items themselves
    comment = "comment"  # a remark about target_item, no position of its own


class Board(Base):
    """One idea board per project. Not "for now": a second board per project was
    considered and dropped, so the row is created on first access and nothing
    ever offers another.

    The board is where pre-production lives: the idea, the references, the
    divergence. The grid stays convergent-by-construction and is a poor fit for
    any of that; see roadmap.md §1 for why this reversed the earlier
    "no separate view" decision.
    """

    __tablename__ = "boards"

    id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), default="Ideas", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    items: Mapped[list["BoardItem"]] = relationship(back_populates="board", cascade="all, delete-orphan")


class BoardItem(Base):
    """A sticker on the board.

    Unlike a grid node -- whose position is *derived* from track_id/step_index
    and may never be a stored display-only override -- a sticker's x/y IS its
    only truth. Nothing computes it, nothing validates it against a layout, and
    that freedom is the entire point of the board.

    The two self-FKs cascade, so deleting a sticker takes its connectors and its
    comments with it instead of leaving them dangling at coordinates that no
    longer mean anything. They're real columns rather than ids buried in
    `content` precisely so the database enforces that.
    """

    __tablename__ = "board_items"

    id: Mapped[uuid.UUID] = _uuid_pk()
    board_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("boards.id", ondelete="CASCADE"), nullable=False)
    kind: Mapped[BoardItemKind] = mapped_column(String(16), nullable=False)

    x: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    y: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    w: Mapped[float] = mapped_column(Float, default=220.0, nullable=False)
    h: Mapped[float] = mapped_column(Float, default=180.0, nullable=False)
    z: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    color: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # text stickers
    text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # Handle for the `{tag}` prompt macro (bridge 2 in roadmap.md §1). Unique
    # per board at the DB level; the route additionally rejects a tag already
    # used on another board of the same project, because a macro resolves
    # against the project, not one board -- two stickers answering to {head}
    # would make it ambiguous which text a run actually used.
    tag: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # image/audio/video stickers -- always a project-scoped Asset (Asset.project_id)
    asset_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"), nullable=True)

    # frame: "rect" | "ellipse". One entity, two renderings -- not two kinds.
    shape: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # ink: an SVG path in board coordinates, plus stroke width in `w`-independent
    # px. Erasing is per-stroke (delete the row); there is no raster layer.
    path: Mapped[str | None] = mapped_column(Text, nullable=True)
    stroke_width: Mapped[float | None] = mapped_column(Float, nullable=True)

    source_item_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("board_items.id", ondelete="CASCADE", use_alter=True, name="fk_board_items_source_item"),
        nullable=True,
    )
    target_item_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("board_items.id", ondelete="CASCADE", use_alter=True, name="fk_board_items_target_item"),
        nullable=True,
    )

    source: Mapped[AnnotationSource] = mapped_column(String(16), default=AnnotationSource.user, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    board: Mapped["Board"] = relationship(back_populates="items")

    __table_args__ = (UniqueConstraint("board_id", "tag", name="uq_board_items_board_tag"),)


class DescriptionSource(str, enum.Enum):
    auto = "auto"
    manual = "manual"


class NodeTypeDescription(Base):
    """What a node type actually does, in words.

    Keyed by slug rather than being a column on NodeTemplate because native
    node types have no NodeTemplate row at all -- this one table covers both
    them and workflow-backed types.

    Three sources, in descending priority: a description a person wrote
    (manual_description, which freezes the entry -- it stops being regenerated
    until explicitly reset), one an agent distilled (agent_description), and
    otherwise an auto one derived from the workflows themselves at read time.

    config_hash pins the agent's version to the configuration it was written
    against: once the workflows change, that cached text is stale by
    definition and is ignored rather than left to mislead.
    """

    __tablename__ = "node_type_descriptions"

    node_type_slug: Mapped[str] = mapped_column(String(128), primary_key=True)
    manual_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    agent_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    description_source: Mapped[DescriptionSource] = mapped_column(String(16), default=DescriptionSource.auto, nullable=False)
    config_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
