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
    mask = "mask"
    other = "other"

    @classmethod
    def for_mime(cls, mime_type: str) -> "AssetKind":
        """What every ingest path records for a file it only knows the MIME
        type of. Never `mesh` or `mask`: a .glb arrives as
        application/octet-stream, and a mask is just an image/png with no
        MIME-visible distinction from any other picture -- both kinds are
        only ever set by the producer that knows what it actually made
        (comfyui_backend.py), never guessed here."""
        return cls.image if mime_type.startswith("image/") else cls.other


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
    # Sub-group this type is offered under in the node-type picker, when the
    # derived one (core/node_category.py: the family of the models its
    # capabilities load) is a bad label. NULL/empty means "use the derived
    # one" -- deliberately not denormalized into a stored value, so attaching
    # a backend that runs a different checkpoint re-groups the type by itself.
    category_override: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ProjectCategory(Base):
    """A folder on the projects page ("Babylon" > "Characters"). Pure
    organisation: nothing in the grid, the board or generation reads it.

    Nests through parent_id to any depth. Deleting one is refused while it
    still holds projects or subcategories (routes/project_categories.py) --
    a cascade here would take finished projects with it, and a SET NULL would
    silently scatter them to the top level."""

    __tablename__ = "project_categories"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("project_categories.id", ondelete="RESTRICT"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # Null = top level of the projects page.
    category_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("project_categories.id", ondelete="RESTRICT"), nullable=True
    )
    # The picture on this project's card, chosen by hand. Null = a random image
    # from the project on every load (routes/projects.py). A project rarely has
    # one "final" picture, so nothing picks this automatically. use_alter:
    # assets.project_id points back here, so the two tables reference each other.
    preview_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("assets.id", ondelete="SET NULL", use_alter=True, name="fk_projects_preview_asset_id"),
        nullable=True,
    )
    # Column kind (asset/workflow) is a project-wide, position-based pattern, not
    # a per-node choice: whichever kind the very first node in the project is
    # given fixes column 0's kind, and it strictly alternates from there. Null
    # until that first node exists. See nodes.py's create_node.
    start_kind: Mapped[NodeKind | None] = mapped_column(String(32), nullable=True)
    # Pure frontend display toggle (Grid.tsx hides workflow columns when set) --
    # the backend only remembers it per scope, same split as start_kind. Never
    # read by any placement/layout logic here.
    asset_only_view: Mapped[bool] = mapped_column(nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    tracks: Mapped[list["Track"]] = relationship(back_populates="project", cascade="all, delete-orphan")


class DesignDoc(Base):
    """A project's design doc: one markdown text per project *per language*
    (a Ukrainian and an English version, written side by side rather than
    translated by the app; the allowed set is `Lang` in routes/design_docs.py). The primary key is (project,
    lang), so a project can't grow a second doc in the same language. A row
    is created on first save; a missing one simply reads as an empty doc.

    The text may reference things elsewhere in the project by scheme instead
    of by URL -- `node:<id>` (a grid cell, shown as whatever picture it stands
    for *now*), `dashboard:<id>` (a sub-dashboard's current result),
    `board:<id>` (a sticker) or `asset:<id>` (one fixed file) --
    resolved at read time by routes/design_docs.py, so the doc follows the
    project instead of freezing a copy of it.
    """

    __tablename__ = "design_docs"

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    lang: Mapped[str] = mapped_column(String(8), primary_key=True)
    content: Mapped[str] = mapped_column(Text, default="", nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class Dashboard(Base):
    """A grid scope: its own tracks, its own column-parity origin, its own
    coordinate space. A project always has an implicit *main* dashboard --
    represented by `Track.dashboard_id IS NULL`, NOT by a row here -- so
    adding this table required no data migration and every pre-existing track
    kept working untouched.

    A sub-dashboard is reached through a **smart pointer**: an `asset.subgraph`
    node whose `subgraph_dashboard_id` points here. Pointers are references,
    not containment -- several may point at the same dashboard, and a pointer
    cycle (A -> B -> A) is legal, since diving in is one deliberate step and
    you navigate back through history, not through structure.

    Reachability is instead guaranteed by ownership: `owner_node_id` is the
    *main* pointer (the one whose creation made this dashboard). Because a
    subgraph node can never be moved out of the dashboard it was created in,
    and a main pointer can't be deleted while its dashboard still has content,
    the main pointers form a spanning tree rooted at the project's main
    dashboard -- so every non-empty dashboard stays reachable. Any additional
    pointer is a non-tree edge and is therefore always safe to delete.
    """

    __tablename__ = "dashboards"

    id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    # Same role as Project.start_kind, but for this scope: column 0's kind,
    # fixed by the first node created here and alternating from there. Each
    # dashboard gets its own origin, which is what lets a sub-dashboard start
    # on a different kind than its parent (nodes moved in are re-aligned by
    # shifting a column, never rejected).
    start_kind: Mapped[NodeKind | None] = mapped_column(String(32), nullable=True)
    # Same purely-cosmetic toggle as Project.asset_only_view, for this scope.
    asset_only_view: Mapped[bool] = mapped_column(nullable=False, default=False)
    # The main pointer. SET NULL rather than CASCADE: losing the owner must
    # never silently destroy the dashboard's contents -- the delete/auto-promote
    # rules in api/routes/dashboards.py decide what happens instead.
    owner_node_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("nodes.id", ondelete="SET NULL", use_alter=True, name="fk_dashboards_owner_node"),
        nullable=True,
    )
    # Which asset inside this subgraph stands for it -- the face every smart
    # pointer shows. Lives on the dashboard, not on the pointer, so several
    # pointers into one subgraph can't drift to different pictures.
    # SET NULL: if the asset is deleted the face simply goes blank and the user
    # picks another, the same way a dangling refasset renders nothing rather
    # than breaking the cell.
    result_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("assets.id", ondelete="SET NULL", use_alter=True, name="fk_dashboards_result_asset"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Track(Base):
    __tablename__ = "tracks"

    id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    # Which grid scope this track belongs to. NULL means the project's main
    # dashboard -- deliberately nullable so that adding sub-dashboards needed
    # no backfill: every track that existed before already reads as "main".
    # The ordering linked list below is per *scope*, not per project: each
    # dashboard has its own head (prev_track_id IS NULL within that scope).
    dashboard_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("dashboards.id", ondelete="CASCADE"), nullable=True
    )
    # Per-scope ordering is a doubly-linked list, NOT a dense row_index
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
    # (is_picker and template_id used to sit here, mirrored from node_type on
    # every write by a sync_legacy_fields helper and read by nobody. Dropped in
    # migration 0017 once node_type was the only thing anything actually
    # consulted: "is this a picker" is is_picker_type(node_type) and "which
    # template" is resolve_effective_template, both in core/.)
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
    # Set on an `asset.subgraph` node -- the *smart pointer* -- naming the
    # dashboard it opens. A pointer is a reference, never containment: many
    # nodes may point at one dashboard, and deleting a pointer never deletes
    # the dashboard's contents (see api/routes/dashboards.py for the ownership
    # and auto-promotion rules that keep every non-empty dashboard reachable).
    # CASCADE only in the other direction -- if a dashboard is ever destroyed,
    # pointers into it would be meaningless, so they go with it.
    subgraph_dashboard_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("dashboards.id", ondelete="CASCADE"), nullable=True
    )
    # How many of this workflow's declared OPTIONAL image/file slots are
    # actually reserved+rendered right now -- NULL (the default, and the only
    # value any node had before this column existed) means "reserve/show the
    # full declared count", unchanged legacy behavior. Set/grown by POST
    # /api/nodes/{id}/recompute-span (api/routes/nodes.py) and seeded on
    # template (re)assignment by ensure_span_rows, both counting how many
    # optional slots currently resolve to a real asset and capping at
    # required_count + filled_optional_count + 1 spare -- never automatically
    # off a fill/move event, since deriving this reactively from grid state was
    # the exact shape of a past incident (see
    # memory/feedback_no_reactive_span_effects.md); growth only ever happens
    # imperatively, from the "⤢" button.
    #
    # Until 2026-09-26 this was purely cosmetic and did NOT feed
    # grid_layout.py's real row reservation (blocked_cells) -- every row up to
    # the template's true declared max stayed structurally reserved regardless,
    # out of a worry that growing back into a since-reclaimed row could
    # silently collide with whatever else had been placed there. For a node
    # with many optional slots (e.g. Qwen Image 2.1 Edit, ~10 optional image
    # references but 2-3 used in practice) that meant permanently blocking 10
    # grid rows for a card that only ever shows 2-3. Traced and confirmed the
    # worry moot -- real occupancy is independently enforced wherever a node
    # is actually moved/created into a cell (_move_asset/_ensure_slot_free/
    # create_node in api/routes/nodes.py), so blocked_cells was only ever an
    # advisory hint for the frontend's own drop-target list, never a backend
    # invariant. This column is now the authoritative reservation target
    # itself (compute_layout uses it directly when set); growing back into a
    # reclaimed row now splices a fresh track for it (_ensure_rows_up_to)
    # instead of relying on the row having stayed pre-reserved underneath.
    visible_slot_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
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
    # Raw ComfyUI step counters behind `progress` above (data["value"]/["max"]
    # off its own "progress" ws message) -- kept alongside the collapsed
    # percent so the frontend can show "23/40" and derive a remaining-time
    # estimate from elapsed-so-far, the way ComfyUI's own tqdm bar does.
    # NULL until the first progress message of a run arrives (nothing to show
    # yet), and for anything that isn't a comfyui_workflow job at all (api_call
    # backends never call on_progress, so these just stay NULL for those).
    progress_step: Mapped[int | None] = mapped_column(Integer, nullable=True)
    progress_max: Mapped[int | None] = mapped_column(Integer, nullable=True)
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
    """A comment thread on a set of nodes, drawn as a frame around them in the
    grid: the frame is this row, what was said is its AnnotationMessages.

    Deliberately stores no coordinates. The frame's box is derived from where
    its member nodes currently are, so moving a node moves the frame with it --
    the same rule the grid already follows for nodes themselves (a node's
    position is always exactly its track_id + step_index, never a stored
    display-only override). Storing a rect here would reintroduce exactly the
    kind of position that can silently desync from the content it describes.

    An agent flagging a cell (the MCP flag_cell tool) writes into the same
    thread a person would, so the two talk in one place. It used to be one
    text field per frame, which made that conversation impossible: an agent
    could only add frames and a person could only overwrite text, so replies
    went into the agent's own frame (which still read source=agent) and every
    "done" became another frame stacked on the same cell (2026-09-27, the
    Nature Spirit session). There is one thread per member set -- commenting
    on cells that already have a thread continues it (see routes/annotations.py).

    Resolving is the "done" state that replaced deleting: nothing anyone said
    is removed, the thread just stops asking for attention, and a new message
    reopens it.
    """

    __tablename__ = "annotations"

    id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    # NULL while open. Who resolved it is kept so a person can tell "the agent
    # says it's done" from "I closed it".
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by: Mapped[AnnotationSource | None] = mapped_column(String(16), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    members: Mapped[list["AnnotationNode"]] = relationship(back_populates="annotation", cascade="all, delete-orphan")
    messages: Mapped[list["AnnotationMessage"]] = relationship(
        back_populates="annotation",
        cascade="all, delete-orphan",
        order_by="AnnotationMessage.created_at",
    )


class AnnotationMessage(Base):
    """One message in a comment thread.

    `source` is the author and never changes: an edit changes the words, not
    who said them. Only the author may edit (routes/annotations.py), so the
    agent can't rewrite a person's message or the other way round -- each
    replies instead."""

    __tablename__ = "annotation_messages"

    id: Mapped[uuid.UUID] = _uuid_pk()
    annotation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("annotations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source: Mapped[AnnotationSource] = mapped_column(String(16), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    annotation: Mapped["Annotation"] = relationship(back_populates="messages")


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
