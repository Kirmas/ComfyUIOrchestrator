import { api } from "./client";
import type {
  Asset,
  Backend,
  Capability,
  DetectedField,
  GridLayout,
  InputRef,
  Job,
  NodeItem,
  NodeKind,
  NodeTemplate,
  Project,
  Track,
  WorkflowAnalysis,
} from "../types";

export const backendsApi = {
  list: () => api.get<Backend[]>("/api/backends"),
  create: (data: {
    name: string;
    kind: string;
    base_url?: string | null;
    provider?: string | null;
    api_key?: string | null;
    daily_limit?: number | null;
  }) => api.post<Backend>("/api/backends", data),
  update: (id: string, data: Partial<Backend> & { api_key?: string }) => api.patch<Backend>(`/api/backends/${id}`, data),
  remove: (id: string) => api.delete(`/api/backends/${id}`),
};

export const capabilitiesApi = {
  list: (params?: { node_type_slug?: string; backend_id?: string }) => {
    const q = new URLSearchParams(params as Record<string, string>).toString();
    return api.get<Capability[]>(`/api/capabilities${q ? `?${q}` : ""}`);
  },
  create: (data: Partial<Capability>) => api.post<Capability>("/api/capabilities", data),
  update: (id: string, data: Partial<Capability>) => api.patch<Capability>(`/api/capabilities/${id}`, data),
  remove: (id: string) => api.delete(`/api/capabilities/${id}`),
  textFields: (id: string) => api.get<DetectedField[]>(`/api/capabilities/${id}/text-fields`),
  updateTextField: (id: string, data: { node_id: string; input_key: string; value: string }) =>
    api.patch<Capability>(`/api/capabilities/${id}/text-fields`, data),
};

export const nodeTemplatesApi = {
  list: () => api.get<NodeTemplate[]>("/api/node-templates"),
  create: (data: Partial<NodeTemplate>) => api.post<NodeTemplate>("/api/node-templates", data),
  update: (id: string, data: Partial<NodeTemplate>) => api.patch<NodeTemplate>(`/api/node-templates/${id}`, data),
  remove: (id: string) => api.delete(`/api/node-templates/${id}`),
  analyzeWorkflow: (file: File) => {
    const form = new FormData();
    form.append("file", file);
    return api.postForm<WorkflowAnalysis>("/api/node-templates/analyze-workflow", form);
  },
};

export const projectsApi = {
  list: () => api.get<Project[]>("/api/projects"),
  create: (name: string) => api.post<Project>("/api/projects", { name }),
  get: (id: string) => api.get<Project>(`/api/projects/${id}`),
  tracks: (id: string) => api.get<Track[]>(`/api/projects/${id}/tracks`),
  // Backend-computed derived layout: workflow row-spans + blocked cells. The
  // client renders from this instead of recomputing the span formula.
  layout: (id: string) => api.get<GridLayout>(`/api/projects/${id}/layout`),
  remove: (id: string) => api.delete(`/api/projects/${id}`),
};

export const tracksApi = {
  // Placement is relative now (linked list): after_track_id, or place_at_head,
  // else appended at the tail. No row_index -- see Track type / core/track_order.py.
  create: (data: {
    project_id: string;
    after_track_id?: string | null;
    place_at_head?: boolean;
    spawned_from_node_id?: string | null;
    spawned_from_output_id?: string | null;
  }) => api.post<Track>("/api/tracks", data),
  get: (id: string) => api.get<Track>(`/api/tracks/${id}`),
  nodes: (id: string) => api.get<NodeItem[]>(`/api/tracks/${id}/nodes`),
  remove: (id: string) => api.delete(`/api/tracks/${id}`),
};

export const nodesApi = {
  create: (data: {
    track_id: string;
    step_index: number;
    kind?: NodeKind;
    node_type?: string | null;
    template_id?: string | null;
    inputs?: InputRef[];
    params?: Record<string, unknown>;
    requested_variants?: number;
    backend_mode?: string;
    manual_backend_id?: string | null;
    use_api?: boolean;
    // Forwarding-only: passes an EXISTING node's own created_by_node_id
    // through to a new node standing in for it (Grid.tsx's
    // onSelectCandidate settling a candidate into the vacated cell) --
    // never a fresh/arbitrary value. See backend's _ensure_output_binding,
    // which validates this exactly like a PATCH would.
    created_by_node_id?: string | null;
  }) => api.post<NodeItem>("/api/nodes", data),
  get: (id: string) => api.get<NodeItem>(`/api/nodes/${id}`),
  update: (id: string, data: Partial<NodeItem>) => api.patch<NodeItem>(`/api/nodes/${id}`, data),
  // Intent-only move: the backend owns all placement logic; the client just
  // names the target grid cell and re-fetches the authoritative layout after.
  move: (id: string, data: { target_row: number; target_step: number }) =>
    api.post<NodeItem[]>(`/api/nodes/${id}/move`, data),
  // Candidate fork -- backend owns the whole reshuffle (settle one, relocate
  // the leftover picker, spawn the next step). Client just names the kept asset.
  pickCandidate: (id: string, keptAssetId: string) =>
    api.post<NodeItem>(`/api/nodes/${id}/pick-candidate`, { kept_asset_id: keptAssetId }),
  pickAllCandidates: (id: string) => api.post<void>(`/api/nodes/${id}/pick-all-candidates`, {}),
  remove: (id: string) => api.delete(`/api/nodes/${id}`),
  outputs: (id: string) => api.get<Asset[]>(`/api/nodes/${id}/outputs`),
  jobs: (id: string) => api.get<Job[]>(`/api/nodes/${id}/jobs`),
  generate: (id: string) => api.post<NodeItem>(`/api/nodes/${id}/generate`),
  discard: (id: string) => api.post<NodeItem>(`/api/nodes/${id}/discard`),
  reroll: (id: string) => api.post<NodeItem>(`/api/nodes/${id}/reroll`),
  // id here is the pass-through asset node's own id, not either workflow
  // node's -- see db/models.py's Node.collapse_target_id docstring.
  collapse: (id: string) => api.post<NodeItem>(`/api/nodes/${id}/collapse`),
  expand: (id: string) => api.post<NodeItem>(`/api/nodes/${id}/expand`),
  uploadAsset: (id: string, file: File) => {
    const form = new FormData();
    form.append("file", file);
    return api.postForm<Asset>(`/api/nodes/${id}/upload-asset`, form);
  },
};

export const assetsApi = {
  upload: (file: File) => {
    const form = new FormData();
    form.append("file", file);
    return api.postForm<Asset>("/api/assets/upload", form);
  },
  get: (id: string) => api.get<Asset>(`/api/assets/${id}`),
  select: (id: string, selected: boolean) => api.patch<Asset>(`/api/assets/${id}/select`, { selected }),
  move: (id: string, nodeId: string) => api.post<Asset>(`/api/assets/${id}/move`, { node_id: nodeId }),
  remove: (id: string) => api.delete(`/api/assets/${id}`),
};

export const jobsApi = {
  get: (id: string) => api.get<Job>(`/api/jobs/${id}`),
  cancel: (id: string) => api.post<Job>(`/api/jobs/${id}/cancel`),
};

export const logsApi = {
  tail: (lines = 300) => api.get<{ lines: string[] }>(`/api/logs?lines=${lines}`),
  clear: () => api.delete("/api/logs"),
};
