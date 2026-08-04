import { api } from "./client";
import type {
  Annotation,
  Asset,
  Backend,
  Board,
  BoardItem,
  BoardItemKind,
  Capability,
  Dashboard,
  DetectedField,
  GridLayout,
  IdeaText,
  InputRef,
  Job,
  MacroResolveResult,
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
  // For a field that's already a param_schema variable (DetectedField.is_variable):
  // edits the field's default on the shared NodeTemplate instead of any one
  // backend's baked workflow_json. Returns the updated NodeTemplate, not the
  // Capability, hence the separate endpoint/method rather than overloading
  // updateTextField.
  updateVariableDefault: (id: string, data: { field_name: string; value: string }) =>
    api.patch<NodeTemplate>(`/api/capabilities/${id}/variable-default`, data),
  setPromptLink: (id: string, leaderId: string | null) =>
    api.patch<Capability>(`/api/capabilities/${id}/prompt-link`, { leader_id: leaderId }),
};

export const nodeTemplatesApi = {
  list: () => api.get<NodeTemplate[]>("/api/node-templates"),
  create: (data: Partial<NodeTemplate>) => api.post<NodeTemplate>("/api/node-templates", data),
  update: (id: string, data: Partial<NodeTemplate>) => api.patch<NodeTemplate>(`/api/node-templates/${id}`, data),
  remove: (id: string) => api.delete(`/api/node-templates/${id}`),
  // Descriptions are addressed by slug, not template id: native node types
  // have no template row but still have a description.
  setDescription: (slug: string, description: string) =>
    api.patch(`/api/node-templates/by-slug/${slug}/description`, { description }),
  resetDescription: (slug: string) => api.delete(`/api/node-templates/by-slug/${slug}/description`),
  // backendId is the ComfyUI this node type is being created for: combo
  // widgets only become enum fields with real options if the analyzer can ask
  // that instance's /object_info, and a custom node present on one backend may
  // be missing on another.
  analyzeWorkflow: (file: File, backendId?: string) => {
    const form = new FormData();
    form.append("file", file);
    if (backendId) form.append("backend_id", backendId);
    return api.postForm<WorkflowAnalysis>("/api/node-templates/analyze-workflow", form);
  },
};

export const projectsApi = {
  list: () => api.get<Project[]>("/api/projects"),
  create: (name: string) => api.post<Project>("/api/projects", { name }),
  get: (id: string) => api.get<Project>(`/api/projects/${id}`),
  // dashboardId null/omitted = the project's main grid; pass one to read that
  // sub-dashboard's own scope instead. Row order, spans and blocked cells only
  // mean anything within a single scope, so tracks and layout must always be
  // fetched for the same one.
  tracks: (id: string, dashboardId?: string | null) =>
    api.get<Track[]>(`/api/projects/${id}/tracks${dashboardId ? `?dashboard_id=${dashboardId}` : ""}`),
  // Backend-computed derived layout: workflow row-spans + blocked cells. The
  // client renders from this instead of recomputing the span formula.
  layout: (id: string, dashboardId?: string | null) =>
    api.get<GridLayout>(`/api/projects/${id}/layout${dashboardId ? `?dashboard_id=${dashboardId}` : ""}`),
  annotations: (id: string) => api.get<Annotation[]>(`/api/projects/${id}/annotations`),
  remove: (id: string) => api.delete(`/api/projects/${id}`),
};

export const dashboardsApi = {
  get: (id: string) => api.get<Dashboard>(`/api/dashboards/${id}`),
  // Turns an existing empty asset cell into a smart pointer on a brand-new
  // dashboard, in one call -- a dashboard never exists without the pointer
  // that keeps it reachable.
  create: (nodeId: string, name: string) => api.post<Dashboard>("/api/dashboards", { node_id: nodeId, name }),
  addPointer: (dashboardId: string, nodeId: string) =>
    api.post<Dashboard>(`/api/dashboards/${dashboardId}/pointers`, { node_id: nodeId }),
  rename: (id: string, name: string) => api.patch<Dashboard>(`/api/dashboards/${id}`, { name }),
  setResult: (id: string, assetId: string | null) =>
    api.post<Dashboard>(`/api/dashboards/${id}/result`, { asset_id: assetId }),
  transferOwnership: (id: string, nodeId: string) =>
    api.post<Dashboard>(`/api/dashboards/${id}/transfer-ownership`, { node_id: nodeId }),
};

export const annotationsApi = {
  create: (data: { project_id: string; node_ids: string[]; text?: string }) =>
    api.post<Annotation>("/api/annotations", data),
  update: (id: string, data: { text?: string; node_ids?: string[] }) =>
    api.patch<Annotation>(`/api/annotations/${id}`, data),
  remove: (id: string) => api.delete(`/api/annotations/${id}`),
};

export const tracksApi = {
  // Placement is relative now (linked list): after_track_id, or place_at_head,
  // else appended at the tail. No row_index -- see Track type / core/track_order.py.
  create: (data: {
    project_id: string;
    // Which grid scope to join; null = the project's main grid. Ignored when
    // after_track_id is given -- splicing into a list means joining its scope.
    dashboard_id?: string | null;
    after_track_id?: string | null;
    place_at_head?: boolean;
    spawned_from_node_id?: string | null;
    spawned_from_output_id?: string | null;
  }) => api.post<Track>("/api/tracks", data),
  get: (id: string) => api.get<Track>(`/api/tracks/${id}`),
  // Moves a contiguous run of tracks into another grid scope (null = the
  // project's main grid). The backend validates that the selection is closed
  // under row-spans, creator/output pairs and spawned tracks, and realigns
  // column parity if the destination started on the other kind.
  moveToDashboard: (trackIds: string[], dashboardId: string | null) =>
    api.post<Track[]>("/api/tracks/move-to-dashboard", { track_ids: trackIds, dashboard_id: dashboardId }),
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
  // Real copy of a workflow node (template + every local setting, none of its
  // results) at the named cell -- same intent-only shape as move above. Not the
  // asset world's reference: the copy is a second independent node.
  duplicate: (id: string, data: { target_row: number; target_step: number }) =>
    api.post<NodeItem>(`/api/nodes/${id}/duplicate`, data),
  // Candidate fork -- backend owns the whole reshuffle (settle one, relocate
  // the leftover picker). Client just names the kept asset.
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
  setTags: (id: string, tags: string[]) => api.patch<Asset>(`/api/assets/${id}/tags`, { tags }),
  remove: (id: string) => api.delete(`/api/assets/${id}`),
};

// The idea board and the project asset library it owns (roadmap.md §1). Note
// there is no "send a grid output to the board" call on purpose: the grid only
// ever references library assets, never owns them.
export const boardApi = {
  get: (projectId: string) => api.get<Board>(`/api/projects/${projectId}/board`),
  items: (boardId: string) => api.get<BoardItem[]>(`/api/boards/${boardId}/items`),
  createItem: (boardId: string, data: Partial<BoardItem> & { kind: BoardItemKind }) =>
    api.post<BoardItem>(`/api/boards/${boardId}/items`, data),
  updateItem: (itemId: string, data: Partial<BoardItem>) => api.patch<BoardItem>(`/api/board-items/${itemId}`, data),
  removeItem: (itemId: string) => api.delete(`/api/board-items/${itemId}`),

  // Project-scoped assets: no node, no cell, no column parity -- this is what
  // makes collecting loose references possible at all.
  assets: (projectId: string, tag?: string) =>
    api.get<Asset[]>(`/api/projects/${projectId}/assets${tag ? `?tag=${encodeURIComponent(tag)}` : ""}`),
  uploadAsset: (projectId: string, file: File) => {
    const form = new FormData();
    form.append("file", file);
    return api.postForm<Asset>(`/api/projects/${projectId}/assets`, form);
  },

  ideaTexts: (projectId: string) => api.get<IdeaText[]>(`/api/projects/${projectId}/idea-texts`),
  // Resolved server-side so the preview shown in a node's config is literally
  // the same computation the worker runs -- a macro must never hide what runs.
  resolveMacros: (projectId: string, text: string) =>
    api.post<MacroResolveResult>(`/api/projects/${projectId}/resolve-macros`, { text }),
};

export const jobsApi = {
  get: (id: string) => api.get<Job>(`/api/jobs/${id}`),
  cancel: (id: string) => api.post<Job>(`/api/jobs/${id}/cancel`),
};

export const logsApi = {
  tail: (lines = 300) => api.get<{ lines: string[] }>(`/api/logs?lines=${lines}`),
  clear: () => api.delete("/api/logs"),
};
