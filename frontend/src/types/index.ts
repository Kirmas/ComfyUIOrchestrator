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
  type: "image" | "text" | "int" | "float" | "seed" | "enum" | "file" | "bool";
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

export interface Track {
  id: string;
  project_id: string;
  row_index: number;
  spawned_from_node_id: string | null;
  spawned_from_output_id: string | null;
  created_at: string;
}

export type InputRef =
  | { type: "self_prev" }
  | { type: "track_below_prev" }
  | { type: "explicit"; node_id: string; output_id: string }
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
  created_at: string;
}

export interface Asset {
  id: string;
  node_id: string | null;
  storage_key: string;
  mime_type: string;
  kind: AssetKind;
  selected: boolean;
  meta: Record<string, unknown>;
  created_at: string;
  url: string | null;
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
