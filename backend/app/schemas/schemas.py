import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.db.models import (
    AnnotationSource,
    AssetKind,
    BackendKind,
    BoardItemKind,
    ExecutionType,
    JobStatusEnum,
    NodeKind,
    NodeStatus,
)


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---------- Backend ----------
class BackendCreate(BaseModel):
    name: str
    kind: BackendKind
    base_url: str | None = None
    is_active: bool = True
    # api_provider kind only -- see db/models.py's Backend.provider/api_key
    # docstring: one key per Backend row, shared by every node type that
    # points a Capability at it.
    provider: str | None = None
    api_key: str | None = None
    daily_limit: int | None = None


class BackendUpdate(BaseModel):
    name: str | None = None
    base_url: str | None = None
    is_active: bool | None = None
    provider: str | None = None
    # Omit to leave the stored key untouched (e.g. editing just daily_limit) --
    # only overwritten when explicitly present in the request body.
    api_key: str | None = None
    daily_limit: int | None = None


class BackendRead(ORMModel):
    id: uuid.UUID
    name: str
    kind: BackendKind
    base_url: str | None
    is_active: bool
    last_heartbeat_at: datetime | None
    last_stats: dict
    provider: str | None
    daily_limit: int | None
    created_at: datetime
    # Never the raw key -- just whether one is set, so the UI can show
    # "key configured" without ever re-displaying (or re-transmitting) the
    # secret itself.
    has_api_key: bool = False
    # Not an ORM column -- computed per-request in backends.list_backends
    # (rolling 24h COUNT(*) over api_usage_log), same reasoning as 0007's
    # used_today had on the now-removed ApiKeyPermissionRead.
    used_today: int = 0


# ---------- Capability ----------
class CapabilityCreate(BaseModel):
    backend_id: uuid.UUID
    node_type_slug: str
    enabled: bool = True
    execution_type: ExecutionType
    config: dict[str, Any] = {}


class CapabilityUpdate(BaseModel):
    node_type_slug: str | None = None
    enabled: bool | None = None
    execution_type: ExecutionType | None = None
    config: dict[str, Any] | None = None


class CapabilityRead(ORMModel):
    id: uuid.UUID
    backend_id: uuid.UUID
    node_type_slug: str
    enabled: bool
    execution_type: ExecutionType
    config: dict
    created_at: datetime


class CapabilityTextFieldUpdate(BaseModel):
    node_id: str
    input_key: str
    value: str


class CapabilityVariableDefaultUpdate(BaseModel):
    field_name: str
    value: str


class CapabilityPromptLink(BaseModel):
    # Make this capability a "follower" that mirrors `leader_id`'s baked prompt
    # text (leader -> follower). None unlinks it back to independent prompts.
    leader_id: uuid.UUID | None = None


# ---------- NodeTemplate ----------
class NodeTemplateCreate(BaseModel):
    node_type_slug: str
    name: str
    param_schema: dict[str, Any] = {}
    defaults: dict[str, Any] = {}


class NodeTemplateUpdate(BaseModel):
    node_type_slug: str | None = None
    name: str | None = None
    param_schema: dict[str, Any] | None = None
    defaults: dict[str, Any] | None = None


class NodeTemplateRead(ORMModel):
    id: uuid.UUID
    node_type_slug: str
    name: str
    param_schema: dict
    defaults: dict
    created_at: datetime
    # Node.node_type value a node picking this template should be given --
    # "template.<slug>" for a real DB row, "native.<slug>" for a synthetic
    # entry synthesized from the NATIVE_NODE_TYPES registry (see
    # api/routes/node_templates.py's list_node_templates and core/node_types.py).
    # Not a DB column; set by the route handler after validation.
    node_type: str = ""
    # What this node type does, resolved from the highest-priority source
    # available (hand-written > agent-distilled > auto-derived from the
    # workflows). Filled in by list_node_templates, see core/node_descriptions.py.
    description: str = ""
    description_source: str = "auto"
    # The auto-derived facts behind the description: model, LoRAs, image
    # inputs, prompt... Values differing between backends are shown per
    # backend, so "same node, configured differently here" is visible.
    fingerprint: dict[str, str] = {}


class ManualDescriptionWrite(BaseModel):
    description: str


class AgentDescriptionWrite(BaseModel):
    description: str
    # How much text the agent actually read to write this. The server can't
    # know that by itself -- the source might have been a merged fingerprint,
    # a baked prompt, or a whole workflow graph -- so it's stated by the
    # caller and used to check the summary is genuinely shorter.
    source_length: int = 0


# ---------- Workflow analysis (template creation wizard) ----------
class WorkflowNodeInfoOut(BaseModel):
    node_id: str
    class_type: str
    title: str | None


class DetectedFieldOut(BaseModel):
    key: str
    label: str
    type: str
    node_id: str
    input_key: str
    default: Any = None
    # Present only for a combo widget, and only when the analyze call named a
    # backend to read /object_info from (see apply_combo_options).
    options: list[str] | None = None
    # True => this field is already a param_schema variable; editing it in
    # CapabilityTextFieldsModal writes its *default*, via a different endpoint
    # than a baked literal. See workflow_analyzer.variable_text_fields.
    is_variable: bool = False


class WorkflowAnalysisOut(BaseModel):
    input_image_nodes: list[WorkflowNodeInfoOut]
    output_nodes: list[WorkflowNodeInfoOut]
    detected_fields: list[DetectedFieldOut]
    duplicate_titles: list[str]


# ---------- Project ----------
class ProjectCreate(BaseModel):
    name: str


class ProjectRead(ORMModel):
    id: uuid.UUID
    name: str
    start_kind: NodeKind | None
    created_at: datetime


# ---------- Track ----------
# Ordering is a doubly-linked list now (Track.prev/next_track_id), not a dense
# row_index -- see core/track_order.py. Placement is expressed relatively:
# "after this track", or "at the head", else append at the tail.
class TrackCreate(BaseModel):
    project_id: uuid.UUID
    # Which grid scope the track joins. None = the project's main grid; an id
    # = that sub-dashboard. When after_track_id is given the anchor's own scope
    # wins, so the client can't splice a track into a list it doesn't belong to.
    dashboard_id: uuid.UUID | None = None
    # Insert the new track immediately after this one. None + place_at_head
    # False (the default) appends at the tail.
    after_track_id: uuid.UUID | None = None
    place_at_head: bool = False
    spawned_from_node_id: uuid.UUID | None = None
    spawned_from_output_id: uuid.UUID | None = None


class TrackRead(ORMModel):
    id: uuid.UUID
    project_id: uuid.UUID
    # Which grid scope this track belongs to; None = the project's main grid.
    dashboard_id: uuid.UUID | None
    prev_track_id: uuid.UUID | None
    next_track_id: uuid.UUID | None
    spawned_from_node_id: uuid.UUID | None
    spawned_from_output_id: uuid.UUID | None
    created_at: datetime


# ---------- InputRef (tagged union, stored as plain dict in JSONB) ----------
class InputRef(BaseModel):
    # Two live types. Four others (self_prev, track_below_prev, upload, text)
    # predate the row-span paradigm and were removed in 2026-08 once nothing
    # created them and no row in the DB carried one: cell_index generalized
    # the first two (they were index 0 and 1), explicit absorbed upload.
    type: str  # cell_index | explicit
    node_id: uuid.UUID | None = None
    output_id: uuid.UUID | None = None
    asset_id: uuid.UUID | None = None
    # "cell_index" only: row-span paradigm positional ref -- read whatever
    # asset node's row (its track's row_index) equals this workflow node's
    # own home row (its track's row_index) + index, in the column right
    # before it, reaching every row a spanning workflow node can grow into.
    index: int | None = None


# ---------- Node ----------
class NodeCreate(BaseModel):
    track_id: uuid.UUID
    step_index: int
    kind: NodeKind = NodeKind.workflow
    # Authoritative discriminator going forward -- "asset.select"/"asset.single"/
    # "native.<slug>"/"template.<slug>" (see core/node_types.py). template_id
    # is kept for backward compatibility only; the route derives it (and
    # is_picker) from node_type when both are given, node_type wins.
    node_type: str | None = None
    template_id: uuid.UUID | None = None
    inputs: list[InputRef] = []
    params: dict[str, Any] = {}
    requested_variants: int = 1
    backend_mode: str = "auto"
    manual_backend_id: uuid.UUID | None = None
    # Explicit opt-in for paid api_call capabilities -- see db/models.py's
    # Node.use_api docstring. Defaults False.
    use_api: bool = False
    # Forwarding-only, not a general-purpose field: Grid.tsx's
    # onSelectCandidate is the one caller, passing an EXISTING node's own
    # created_by_node_id through to the fresh settled node standing in for
    # it in the vacated cell -- that settled node is just as much this
    # workflow's output as the picker it replaced, even though it's created
    # here rather than by _get_or_create_output_asset_node. Never a
    # caller-fabricated value: create_node validates it with the same
    # _ensure_output_binding check update_node uses, so a bogus value 409s
    # exactly like an illegal PATCH would rather than silently taking hold.
    created_by_node_id: uuid.UUID | None = None


class PickCandidate(BaseModel):
    # "Keep this candidate asset out of the picker; move the rest to their own
    # line." The backend owns the whole fork (settle the kept one in place,
    # relocate the leftover picker to a spawned/reused row) -- see
    # api/routes/nodes.py's _pick_candidate.
    kept_asset_id: uuid.UUID


class NodeDuplicate(BaseModel):
    # Intent only, same two fields and same meaning as NodeMove below: "put a
    # real copy of this workflow node at grid (row, column)". target_row is a
    # position in the project's track list order (core/track_order.py), not a
    # stored number. See api/routes/nodes.py's duplicate_node.
    target_row: int
    target_step: int


class NodeMove(BaseModel):
    # Intent only -- "put this node at grid (row, column)". The backend owns
    # ALL of the placement logic (allowed-position/output-binding checks,
    # carrying a workflow's dependents along, collision handling); the client
    # just names where the user dropped it. target_row is a position in the
    # project's track list order (core/track_order.py), not a stored number.
    target_row: int
    target_step: int


class NodeUpdate(BaseModel):
    # Relocates the node to a different track (same id, same history/jobs/
    # assets) -- used by Grid.tsx's onSelectCandidate, dropAssetAt, and
    # applyRowMove to move a node to a different row (tracks model rows 1:1
    # via row_index) without a destructive delete+recreate (DELETE
    # /api/nodes/{id} cascades forward through the rest of the track, which
    # is right for the user-facing "delete this cell" action but wrong here
    # -- this node isn't being deleted, just relocated). This is the ONLY
    # way a node's row changes -- there is no cosmetic/view-only position;
    # wherever a node is displayed is exactly its track_id/step_index.
    track_id: uuid.UUID | None = None
    # Relocates the node to a different column, used only by Grid.tsx's
    # insertColumnsAt (shifting every node at/after an insertion point by a
    # fixed, parity-preserving delta so a horizontal workflow drag can make
    # room without breaking the project-wide asset/workflow alternation --
    # see _kind_for_step). Never sent in isolation for a single node; always
    # part of a whole-project batch that preserves every node's kind.
    step_index: int | None = None
    node_type: str | None = None
    template_id: uuid.UUID | None = None
    inputs: list[InputRef] | None = None
    params: dict[str, Any] | None = None
    requested_variants: int | None = None
    status: NodeStatus | None = None
    backend_mode: str | None = None
    manual_backend_id: uuid.UUID | None = None
    use_api: bool | None = None
    is_picker: bool | None = None
    # Explicit unbind only -- NodeCell.tsx's "detach & remove workflow"
    # button sends `created_by_node_id: null` right before deleting the
    # creator workflow node, so delete_node's own_output_nodes sweep no
    # longer counts this asset as one of that workflow's outputs and
    # doesn't cascade-delete it along with the rest. Never set to a real
    # id via this route -- created_by_node_id is otherwise written exactly
    # once, by worker/tasks.py's _get_or_create_output_asset_node, and
    # never touched again (see db/models.py's docstring).
    created_by_node_id: uuid.UUID | None = None


class NodeRead(ORMModel):
    id: uuid.UUID
    track_id: uuid.UUID
    step_index: int
    kind: NodeKind
    node_type: str | None
    is_picker: bool
    template_id: uuid.UUID | None
    inputs: list[dict]
    params: dict
    status: NodeStatus
    backend_used_id: uuid.UUID | None
    requested_variants: int
    backend_mode: str
    manual_backend_id: uuid.UUID | None
    use_api: bool
    error: str | None
    # Read-only -- see db/models.py's Node.created_by_node_id docstring.
    # Never appears on NodeCreate/NodeUpdate; the only writer is
    # _get_or_create_output_asset_node (worker/tasks.py).
    created_by_node_id: uuid.UUID | None
    # Read-only -- see db/models.py's Node.collapse_target_id docstring.
    # Never appears on NodeCreate/NodeUpdate; the only writers are
    # POST /api/nodes/{id}/collapse and .../expand.
    collapse_target_id: uuid.UUID | None
    # Read-only -- the dashboard this smart pointer opens. Written only by
    # api/routes/dashboards.py, never through a generic PATCH.
    subgraph_dashboard_id: uuid.UUID | None
    created_at: datetime


# ---------- Asset ----------
class AssetRead(ORMModel):
    id: uuid.UUID
    node_id: uuid.UUID | None
    project_id: uuid.UUID | None = None
    storage_key: str
    mime_type: str
    kind: AssetKind
    selected: bool
    tags: list[str] = []
    meta: dict
    created_at: datetime
    url: str | None = None


class AssetTagsUpdate(BaseModel):
    tags: list[str]


class AssetSelectUpdate(BaseModel):
    selected: bool


class AssetMoveUpdate(BaseModel):
    node_id: uuid.UUID


# ---------- Job ----------
class JobRead(ORMModel):
    id: uuid.UUID
    node_id: uuid.UUID
    backend_id: uuid.UUID | None
    variant_index: int
    status: JobStatusEnum
    external_job_id: str | None
    retries: int
    error: str | None
    progress: float
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None




# ---------- Annotation (comment blocks) ----------
# No geometry here on purpose: an annotation is a set of member nodes, and the
# frame drawn around them is derived from where those nodes currently are (see
# db/models.py's Annotation docstring).
class AnnotationCreate(BaseModel):
    project_id: uuid.UUID
    node_ids: list[uuid.UUID] = []
    text: str = ""
    source: AnnotationSource = AnnotationSource.user


class AnnotationUpdate(BaseModel):
    text: str | None = None
    node_ids: list[uuid.UUID] | None = None


class AnnotationRead(ORMModel):
    id: uuid.UUID
    project_id: uuid.UUID
    text: str
    source: AnnotationSource
    node_ids: list[uuid.UUID]
    created_at: datetime
    updated_at: datetime


# ---------- Idea board (roadmap.md §1) ----------
# Geometry IS stored here, unlike Annotation above -- a sticker's x/y is its
# only truth, nothing derives or validates it. That's the point of the board.
class BoardRead(ORMModel):
    id: uuid.UUID
    project_id: uuid.UUID
    name: str
    created_at: datetime


class BoardItemCreate(BaseModel):
    kind: BoardItemKind
    x: float = 0.0
    y: float = 0.0
    w: float = 220.0
    h: float = 180.0
    z: int = 0
    color: str | None = None
    text: str = ""
    tag: str | None = None
    asset_id: uuid.UUID | None = None
    shape: str | None = None
    path: str | None = None
    stroke_width: float | None = None
    source_item_id: uuid.UUID | None = None
    target_item_id: uuid.UUID | None = None
    source: AnnotationSource = AnnotationSource.user


class BoardItemUpdate(BaseModel):
    # Every field optional and None-means-untouched: dragging a sticker PATCHes
    # x/y alone several times a second, and must not have to echo back its text.
    x: float | None = None
    y: float | None = None
    w: float | None = None
    h: float | None = None
    z: int | None = None
    color: str | None = None
    text: str | None = None
    # None means "leave the tag alone"; the empty string means "remove it".
    # A nullable field whose absence and whose clearing are the same value
    # can't express both, and untagging a sticker has to be possible.
    tag: str | None = None
    shape: str | None = None
    path: str | None = None
    stroke_width: float | None = None


class BoardItemRead(ORMModel):
    id: uuid.UUID
    board_id: uuid.UUID
    kind: BoardItemKind
    x: float
    y: float
    w: float
    h: float
    z: int
    color: str | None
    text: str
    tag: str | None
    asset_id: uuid.UUID | None
    shape: str | None
    path: str | None
    stroke_width: float | None
    source_item_id: uuid.UUID | None
    target_item_id: uuid.UUID | None
    source: AnnotationSource
    created_at: datetime
    updated_at: datetime
    # Populated for image/audio/video stickers so the client can render without
    # a second round-trip per sticker. asset_tags is what the grid's reference
    # picker filters on -- the labels are set here, on the board that owns the
    # asset, and read over there.
    asset_url: str | None = None
    asset_mime_type: str | None = None
    asset_tags: list[str] = []


class IdeaTextRead(BaseModel):
    """A text sticker as the node-config prompt picker sees it. Both forms are
    returned: the markdown for display, the stripped text because that's what
    actually gets inserted (see core/idea_macros.py)."""

    item_id: uuid.UUID
    tag: str | None
    text_markdown: str
    text_plain: str


class MacroResolveRequest(BaseModel):
    text: str


class MacroResolveResult(BaseModel):
    resolved: str
    # Tags with no sticker behind them. They stay literal in `resolved` rather
    # than expanding to nothing; the UI marks them so a stale macro is visible
    # instead of quietly emptying a prompt.
    unresolved: list[str]


# ---------- Node-type authoring (agent-facing, see api/routes/node_types.py) ----------
class CreateNodeTypeRequest(BaseModel):
    workflow_json: dict[str, Any]
    name: str
    node_type_slug: str
    backend_id: uuid.UUID
    # Always required: the caller states up front what should be settable per
    # cell. There is no follow-up screen to fix an omission on.
    param_mapping: dict[str, Any]


class AddCapabilityRequest(BaseModel):
    backend_id: uuid.UUID
    workflow_json: dict[str, Any]
    param_mapping: dict[str, Any]


class DashboardCreate(BaseModel):
    # The asset cell that becomes this dashboard's main pointer.
    node_id: uuid.UUID
    name: str = ""


class PointerCreate(BaseModel):
    node_id: uuid.UUID


class DashboardRename(BaseModel):
    name: str


class TransferOwnership(BaseModel):
    node_id: uuid.UUID


class SetDashboardResult(BaseModel):
    # None clears the face.
    asset_id: uuid.UUID | None = None


class DashboardRead(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    name: str
    start_kind: NodeKind | None
    owner_node_id: uuid.UUID | None
    result_asset_id: uuid.UUID | None = None
    # Resolved here so a pointer can render the face without a second lookup.
    result_asset_url: str | None = None
    # Derived, not stored: node_count drives the "can't delete the main pointer
    # while it still holds work" rule, pointer_count tells the UI whether this
    # is the last way in.
    node_count: int = 0
    pointer_count: int = 0

    model_config = ConfigDict(from_attributes=True)


class TracksMove(BaseModel):
    # A contiguous run of tracks, moved as one unit into another grid scope.
    track_ids: list[uuid.UUID]
    # Destination scope; None = the project's main grid.
    dashboard_id: uuid.UUID | None = None
