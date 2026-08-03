export type BackendKind = "comfyui" | "api_provider" | "native";
export type ExecutionType = "comfyui_workflow" | "api_call" | "native";
export type NodeStatus = "draft" | "queued" | "running" | "done" | "error" | "discarded";
export type JobStatus = "pending" | "waiting_for_backend" | "running" | "done" | "error" | "cancelled";
export type AssetKind = "image" | "mesh" | "other";
export type BackendMode = "auto" | "comfyui_only" | "api_only" | "manual";
export type NodeKind = "asset" | "workflow";

export interface Backend {
  id: string;
  name: string;
  kind: BackendKind;
  base_url: string | null;
  is_active: boolean;
  last_heartbeat_at: string | null;
  last_stats: Record<string, unknown>;
  // api_provider kind only -- one key per Backend row, shared by every node
  // type that points a Capability at it. `has_api_key`/`used_today` are
  // read-only (never the raw key; used_today is computed server-side).
  provider: string | null;
  has_api_key: boolean;
  daily_limit: number | null;
  used_today: number;
  created_at: string;
}

export interface Capability {
  id: string;
  backend_id: string;
  node_type_slug: string;
  enabled: boolean;
  execution_type: ExecutionType;
  config: Record<string, unknown>;
  created_at: string;
}

export interface ParamField {
  name: string;
  // "mask" is a params-stored bilevel PNG (base64), not a workflow input
  // slot -- see native.mask in node_types.py and MaskPreview.tsx.
  type: "image" | "text" | "int" | "float" | "seed" | "enum" | "file" | "bool" | "mask";
  label?: string;
  required?: boolean;
  // Not every capability for this node_type_slug has to honor this field --
  // a capability whose workflow doesn't wire it up just omits it from its
  // param_mapping, and the value is silently ignored for that backend.
  optional?: boolean;
  default?: unknown;
  min?: number;
  max?: number;
  options?: string[];
}

export interface ParamSchema {
  fields: ParamField[];
}

export interface NodeTemplate {
  id: string;
  node_type_slug: string;
  name: string;
  param_schema: ParamSchema;
  defaults: Record<string, unknown>;
  created_at: string;
  // What this node type does. Resolved server-side from the best available
  // source -- hand-written, agent-distilled, or derived from the workflows --
  // with description_source saying which. fingerprint holds the derived facts
  // behind it (model, LoRAs, image inputs, prompt), values shown per backend
  // where the backends differ.
  description?: string;
  description_source?: "auto" | "manual" | "agent";
  fingerprint?: Record<string, string>;
  // "template.<slug>" for a real DB-backed type, "native.<slug>" for one
  // synthesized from the backend's native registry (no DB row behind it).
  node_type: string;
}

export interface Project {
  id: string;
  name: string;
  start_kind: NodeKind | null;
  created_at: string;
}

// Backend-computed derived layout (GET /api/projects/{id}/layout). The client
// renders workflow spans / blocked cells from this instead of recomputing the
// span formula (which used to drift between the two sides).
export interface GridLayout {
  spans: Record<string, { desired: number; achieved: number }>;
  blocked_cells: [number, number][];
}

// A comment block: text attached to a set of nodes, drawn as a frame around
// them. No coordinates -- the frame is derived from where its members
// currently sit, so it follows them when they move. "agent" ones are cells an
// agent flagged as needing a look; they're the same object as a hand-written
// note and are reviewed together.
export interface Annotation {
  id: string;
  project_id: string;
  text: string;
  source: "user" | "agent";
  node_ids: string[];
  created_at: string;
  updated_at: string;
}

export interface Track {
  id: string;
  project_id: string;
  // Which grid scope this track belongs to; null = the project's main grid.
  dashboard_id: string | null;
  // Ordering is a doubly-linked list on the backend (prev/next). row_index is
  // NOT sent by the API anymore -- it's derived client-side from a track's
  // position in list order (the store assigns it in setTracks) purely for
  // rendering/positional math, and never round-trips to the server. This is
  // what killed the old reindex-on-every-delete data-loss bug: there's no
  // stored number left to gap or desync.
  prev_track_id: string | null;
  next_track_id: string | null;
  row_index: number;
  spawned_from_node_id: string | null;
  spawned_from_output_id: string | null;
  created_at: string;
}

export type InputRef =
  | { type: "self_prev" }
  | { type: "track_below_prev" }
  // node_id is optional: an asset in the project's reference library (the idea
  // board's) has no owning node at all, and resolution on both ends goes by
  // asset id anyway (_explicit_ref_asset / resolveSlotAsset). It stays part of
  // the ref when there IS an owner, since that's what the grid draws its "ref"
  // arrow from.
  | { type: "explicit"; node_id?: string; output_id: string }
  | { type: "upload"; asset_id: string }
  | { type: "text"; value: string }
  // Row-span paradigm positional ref: reads whatever asset node's row (its
  // own track's row_index) equals this workflow node's own home row + index,
  // in the column right before it. Generalizes self_prev (index 0) and
  // track_below_prev (index 1) across every row a spanning workflow node
  // can grow into.
  | { type: "cell_index"; index: number };

export interface NodeItem {
  id: string;
  track_id: string;
  step_index: number;
  kind: NodeKind;
  // Authoritative discriminator: "asset.select" / "asset.single" /
  // "native.<slug>" / "template.<slug>" (see backend/app/core/node_types.py).
  // null only for a fresh workflow cell that hasn't picked a template yet.
  node_type: string | null;
  is_picker: boolean;
  template_id: string | null;
  inputs: InputRef[];
  params: Record<string, unknown>;
  status: NodeStatus;
  backend_used_id: string | null;
  requested_variants: number;
  backend_mode: BackendMode;
  manual_backend_id: string | null;
  // Explicit opt-in for paid api_call capabilities -- never implied by
  // backend_mode="auto"/"api_only". See backend/app/db/models.py's
  // Node.use_api docstring.
  use_api: boolean;
  error: string | null;
  // Read-only, set exactly once by the backend when a workflow node
  // materializes its result as this (following) asset node -- see
  // backend/app/db/models.py's Node.created_by_node_id docstring. NULL for
  // every manually-placed asset (upload, "+ asset", RefAsset). Rigidly
  // binds this node to its creator's own output position -- see Grid.tsx's
  // isPositionAllowedFor.
  created_by_node_id: string | null;
  // Read-only, set only via POST /api/nodes/{id}/collapse|expand -- lives on
  // the pass-through asset node of a workflow -> asset -> workflow chain
  // (this asset's own created_by_node_id is that chain's first workflow;
  // this field, once set, points at the second). Non-null folds the 3-cell
  // chain into one card in NodeCell.tsx and locks both workflow nodes (no
  // generate/reroll/discard) since collapsing is for finished history the
  // user doesn't intend to regenerate.
  collapse_target_id: string | null;
  // Set on an `asset.subgraph` node -- the smart pointer -- naming the
  // dashboard it opens. Read-only: written only by the /api/dashboards
  // endpoints, never through a generic PATCH.
  subgraph_dashboard_id: string | null;
  created_at: string;
}

export interface Dashboard {
  id: string;
  project_id: string;
  name: string;
  start_kind: NodeKind | null;
  // The main pointer. Deleting it is refused while node_count > 0; if the
  // dashboard is empty and other pointers remain, one is auto-promoted.
  owner_node_id: string | null;
  // The asset inside this subgraph that stands for it -- the face every
  // pointer shows. Lives on the dashboard so two pointers can't diverge.
  result_asset_id: string | null;
  result_asset_url: string | null;
  node_count: number;
  pointer_count: number;
}

export interface Asset {
  id: string;
  node_id: string | null;
  // Set instead of node_id for a project-library asset -- one the idea board
  // owns and no grid cell does (see BoardItem below / roadmap.md §1).
  project_id?: string | null;
  storage_key: string;
  mime_type: string;
  kind: AssetKind;
  selected: boolean;
  tags?: string[];
  meta: Record<string, unknown>;
  created_at: string;
  url: string | null;
}

// ---------- Idea board ----------
// A sticker holds exactly one kind of content, so `kind` is also the
// content-type discriminator. Unlike a grid node, whose position is derived
// from track_id/step_index, a sticker's x/y IS its only truth -- that freedom
// is the point of the board.
export type BoardItemKind = "text" | "image" | "audio" | "video" | "frame" | "ink" | "connector" | "comment";

export interface Board {
  id: string;
  project_id: string;
  name: string;
  created_at: string;
}

export interface BoardItem {
  id: string;
  board_id: string;
  kind: BoardItemKind;
  x: number;
  y: number;
  w: number;
  h: number;
  z: number;
  color: string | null;
  text: string;
  // Handle for the `{tag}` prompt macro. Unique per project, enforced server-side.
  tag: string | null;
  asset_id: string | null;
  shape: string | null;
  path: string | null;
  stroke_width: number | null;
  source_item_id: string | null;
  target_item_id: string | null;
  source: "user" | "agent";
  created_at: string;
  updated_at: string;
  asset_url: string | null;
  asset_mime_type: string | null;
  // Labels on the underlying library asset (not the `tag` above, which is the
  // text sticker's prompt-macro handle). Set here on the board; read by the
  // grid's reference picker to filter.
  asset_tags: string[];
}

export interface IdeaText {
  item_id: string;
  tag: string | null;
  text_markdown: string;
  // What actually gets inserted into a prompt: markdown belongs on the board,
  // not in front of a sampler.
  text_plain: string;
}

export interface MacroResolveResult {
  resolved: string;
  unresolved: string[];
}

export interface Job {
  id: string;
  node_id: string;
  backend_id: string | null;
  variant_index: number;
  status: JobStatus;
  external_job_id: string | null;
  retries: number;
  error: string | null;
  progress: number;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface ProgressEvent {
  type: "job" | "node";
  job_id?: string;
  node_id: string;
  status: string;
  progress?: number;
  error?: string;
}

// ---------- Template creation wizard ----------
export interface WorkflowNodeInfo {
  node_id: string;
  class_type: string;
  title: string | null;
}

export interface DetectedField {
  key: string;
  label: string;
  type: string;
  node_id: string;
  input_key: string;
  default: unknown;
  // Set only for a ComfyUI combo widget, read from the chosen backend's
  // /object_info at analyze time -- a workflow.json records the picked value
  // but never the list it came from. Present => type is "enum".
  options?: string[] | null;
}

export interface WorkflowAnalysis {
  input_image_nodes: WorkflowNodeInfo[];
  output_nodes: WorkflowNodeInfo[];
  detected_fields: DetectedField[];
  duplicate_titles: string[];
}

// A capability's config.param_mapping entry (comfyui_workflow execution
// type only -- see backend/app/core/template_engine.py's build_workflow).
// node_id is what actually gets resolved against workflow_json; title is
// carried along only for error messages / display, captured once at wizard
// time from the same workflow_json snapshot so it can never drift out of
// sync with it the way a title-only lookup could (two nodes sharing a
// title, ComfyUI doesn't enforce uniqueness -- 2026-07-18 incident).
export interface ParamMappingEntry {
  node_id: string;
  title: string | null;
  input_key: string;
}
