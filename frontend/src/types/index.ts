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
  | { type: "text"; value: string };

export interface NodeItem {
  id: string;
  track_id: string;
  step_index: number;
  kind: NodeKind;
  is_picker: boolean;
  template_id: string | null;
  inputs: InputRef[];
  params: Record<string, unknown>;
  status: NodeStatus;
  backend_used_id: string | null;
  requested_variants: number;
  backend_mode: BackendMode;
  manual_backend_id: string | null;
  error: string | null;
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

export interface ApiKeyPermission {
  id: string;
  provider: string;
  node_type_slug: string;
  enabled: boolean;
  created_at: string;
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
