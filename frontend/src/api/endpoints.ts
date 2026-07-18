import { api } from "./client";
import type {
  ApiKeyPermission,
  Asset,
  Backend,
  Capability,
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
  create: (data: { name: string; kind: string; base_url?: string | null }) => api.post<Backend>("/api/backends", data),
  update: (id: string, data: Partial<Backend>) => api.patch<Backend>(`/api/backends/${id}`, data),
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
  remove: (id: string) => api.delete(`/api/projects/${id}`),
};

export const tracksApi = {
  create: (data: { project_id: string; row_index: number; spawned_from_node_id?: string | null; spawned_from_output_id?: string | null }) =>
    api.post<Track>("/api/tracks", data),
  nodes: (id: string) => api.get<NodeItem[]>(`/api/tracks/${id}/nodes`),
  update: (id: string, data: { row_index: number }) => api.patch<Track>(`/api/tracks/${id}`, data),
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
    // Forwarding-only: passes an EXISTING node's own created_by_node_id
    // through to a new node standing in for it (Grid.tsx's
    // onSelectCandidate settling a candidate into the vacated cell) --
    // never a fresh/arbitrary value. See backend's _ensure_output_binding,
    // which validates this exactly like a PATCH would.
    created_by_node_id?: string | null;
  }) => api.post<NodeItem>("/api/nodes", data),
  get: (id: string) => api.get<NodeItem>(`/api/nodes/${id}`),
  update: (id: string, data: Partial<NodeItem>) => api.patch<NodeItem>(`/api/nodes/${id}`, data),
  remove: (id: string) => api.delete(`/api/nodes/${id}`),
  outputs: (id: string) => api.get<Asset[]>(`/api/nodes/${id}/outputs`),
  jobs: (id: string) => api.get<Job[]>(`/api/nodes/${id}/jobs`),
  generate: (id: string) => api.post<NodeItem>(`/api/nodes/${id}/generate`),
  discard: (id: string) => api.post<NodeItem>(`/api/nodes/${id}/discard`),
  reroll: (id: string) => api.post<NodeItem>(`/api/nodes/${id}/reroll`),
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

export const apiKeysApi = {
  list: () => api.get<ApiKeyPermission[]>("/api/api-keys"),
  create: (data: { provider: string; node_type_slug: string; api_key: string; enabled?: boolean }) =>
    api.post<ApiKeyPermission>("/api/api-keys", data),
  remove: (id: string) => api.delete(`/api/api-keys/${id}`),
};

export const logsApi = {
  tail: (lines = 300) => api.get<{ lines: string[] }>(`/api/logs?lines=${lines}`),
  clear: () => api.delete("/api/logs"),
};
