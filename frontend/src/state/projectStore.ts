import { create } from "zustand";
import { nodesApi, projectsApi, tracksApi } from "../api/endpoints";
import type { Asset, NodeItem, ProgressEvent, Track } from "../types";

// The API returns tracks already in list order (backend walks the linked
// list). row_index is a purely client-side, derived positional index (array
// position) used for rendering/positional math -- never sent back to the
// server. Centralised here so it can never drift from list order.
function withRowIndex(tracks: Track[]): Track[] {
  return tracks.map((t, i) => (t.row_index === i ? t : { ...t, row_index: i }));
}

// The backend-computed layout (workflow spans + blocked cells) is derived from
// nodes+tracks, so it must be re-fetched whenever either changes. Rather than
// hunt every mutation site, every store mutator that touches nodes/tracks
// pings this; it debounces so a burst of local updates (e.g. onSelectCandidate
// adding several nodes) coalesces into one refetch. The frontend no longer
// computes span itself -- it just asks the backend after each change.
let layoutTimer: ReturnType<typeof setTimeout> | null = null;
function scheduleLayoutRefresh() {
  if (layoutTimer) clearTimeout(layoutTimer);
  layoutTimer = setTimeout(() => {
    const { projectId, reloadLayout } = useProjectStore.getState();
    if (projectId) reloadLayout(projectId).catch(() => undefined);
  }, 60);
}

interface ProjectState {
  projectId: string | null;
  tracks: Track[];
  nodesById: Record<string, NodeItem>;
  outputsByNode: Record<string, Asset[]>;
  // Backend-computed derived layout -- see core/grid_layout.py. spans keyed by
  // workflow node id; blockedCells is a Set of "row:col" strings a spanning
  // card covers in its own column. The frontend reads these instead of
  // recomputing the span formula.
  spans: Record<string, { desired: number; achieved: number }>;
  blockedCells: Set<string>;

  loadProject: (projectId: string) => Promise<void>;
  reloadTracks: (projectId: string) => Promise<void>;
  reloadLayout: (projectId: string) => Promise<void>;
  refreshTrack: (trackId: string) => Promise<void>;
  refreshNodeOutputs: (nodeId: string) => Promise<void>;
  applyProgressEvent: (event: ProgressEvent) => void;
  addTrack: (track: Track) => void;
  setTracks: (tracks: Track[]) => void;
  addNode: (node: NodeItem) => void;
  replaceNode: (oldId: string, node: NodeItem) => void;
  setNode: (node: NodeItem) => void;
  removeNode: (nodeId: string) => void;
  removeTrack: (trackId: string) => void;
}

export const useProjectStore = create<ProjectState>((set, get) => ({
  projectId: null,
  tracks: [],
  nodesById: {},
  outputsByNode: {},
  spans: {},
  blockedCells: new Set(),

  loadProject: async (projectId: string) => {
    const tracks = withRowIndex(await projectsApi.tracks(projectId));
    const nodesById: Record<string, NodeItem> = {};
    for (const track of tracks) {
      const nodes = await tracksApi.nodes(track.id);
      for (const node of nodes) nodesById[node.id] = node;
    }
    const layout = await projectsApi.layout(projectId).catch(() => ({ spans: {}, blocked_cells: [] as [number, number][] }));
    set({
      projectId,
      tracks,
      nodesById,
      outputsByNode: {},
      spans: layout.spans,
      blockedCells: new Set(layout.blocked_cells.map(([r, c]) => `${r}:${c}`)),
    });

    for (const node of Object.values(nodesById)) {
      if (node.status === "done") {
        get()
          .refreshNodeOutputs(node.id)
          .catch(() => undefined);
      }
    }
  },

  // Re-fetch just the ordered track list (nodes don't move -- their track_id
  // is stable) and re-derive row_index from the new order. This is what every
  // structural track op (create/delete/splice) calls instead of the old
  // optimistic-shift + per-track PATCH reindex: one GET, no renumber writes,
  // nothing to leave half-applied. Span depends on track positions, so refresh
  // the layout too.
  reloadTracks: async (projectId: string) => {
    const tracks = withRowIndex(await projectsApi.tracks(projectId));
    set({ tracks });
    await get().reloadLayout(projectId);
  },

  reloadLayout: async (projectId: string) => {
    const layout = await projectsApi.layout(projectId);
    set({ spans: layout.spans, blockedCells: new Set(layout.blocked_cells.map(([r, c]) => `${r}:${c}`)) });
  },

  refreshTrack: async (trackId: string) => {
    const nodes = await tracksApi.nodes(trackId);
    set((state) => {
      const nodesById = { ...state.nodesById };
      for (const node of nodes) nodesById[node.id] = node;
      return { nodesById };
    });
    scheduleLayoutRefresh();
  },

  refreshNodeOutputs: async (nodeId: string) => {
    const outputs = await nodesApi.outputs(nodeId);
    set((state) => ({ outputsByNode: { ...state.outputsByNode, [nodeId]: outputs } }));
  },

  applyProgressEvent: (event: ProgressEvent) => {
    const node = get().nodesById[event.node_id];

    if (event.type === "node") {
      if (!node) {
        // The worker lazily creates the paired output asset-node on first
        // result -- the frontend doesn't know it exists until now, so fetch
        // and insert it instead of dropping the event. Its track can be
        // just as new: worker/tasks.py's _locate_output_row can insert a
        // brand-new Track (no spawned_from_node_id, so nothing else ever
        // adds it) when the workflow's home row's output cell was already
        // settled -- without also fetching that, the node would sit in
        // nodesById with a track_id nothing in `tracks` recognizes, and
        // Grid.tsx's render loop only walks known tracks, so it silently
        // never appeared until a full reload re-fetched everything fresh
        // (2026-07-20 incident).
        nodesApi
          .get(event.node_id)
          .then(async (fetched) => {
            if (!get().tracks.some((t) => t.id === fetched.track_id)) {
              // The worker may have spliced this output's track into the
              // MIDDLE of the list (_locate_output_row), not at the tail, so
              // re-fetch the whole ordered list to place it correctly rather
              // than appending it (addTrack would put it at the wrong row).
              const pid = get().projectId;
              if (pid) await get().reloadTracks(pid);
            }
            get().addNode(fetched);
            return get().refreshNodeOutputs(event.node_id);
          })
          .catch(() => undefined);
        return;
      }
      set((state) => ({
        nodesById: { ...state.nodesById, [event.node_id]: { ...node, status: event.status as NodeItem["status"] } },
      }));
      get()
        .refreshNodeOutputs(event.node_id)
        .catch(() => undefined);
    } else if (event.type === "job" && node && node.status !== "done" && node.status !== "error") {
      const nextStatus = event.status === "running" ? "running" : node.status;
      set((state) => ({
        nodesById: { ...state.nodesById, [event.node_id]: { ...node, status: nextStatus as NodeItem["status"] } },
      }));
    }
  },

  // Appends at the tail with a derived row_index (= new length-1). Used only
  // for genuine tail appends (a scratch track for a swap, "+ New track");
  // positional inserts go through the backend splice + reloadTracks instead.
  addTrack: (track: Track) => {
    set((state) => ({ tracks: [...state.tracks, { ...track, row_index: state.tracks.length }] }));
    scheduleLayoutRefresh();
  },
  setTracks: (tracks: Track[]) => {
    set({ tracks: withRowIndex(tracks) });
    scheduleLayoutRefresh();
  },
  addNode: (node: NodeItem) => {
    set((state) => ({ nodesById: { ...state.nodesById, [node.id]: node } }));
    scheduleLayoutRefresh();
  },
  replaceNode: (oldId: string, node: NodeItem) => {
    set((state) => {
      const nodesById = { ...state.nodesById };
      delete nodesById[oldId];
      nodesById[node.id] = node;
      return { nodesById };
    });
    scheduleLayoutRefresh();
  },
  setNode: (node: NodeItem) => {
    set((state) => ({ nodesById: { ...state.nodesById, [node.id]: node } }));
    scheduleLayoutRefresh();
  },
  removeNode: (nodeId: string) => {
    set((state) => {
      const nodesById = { ...state.nodesById };
      delete nodesById[nodeId];
      const outputsByNode = { ...state.outputsByNode };
      delete outputsByNode[nodeId];
      return { nodesById, outputsByNode };
    });
    scheduleLayoutRefresh();
  },
  removeTrack: (trackId: string) => {
    set((state) => {
      const nodesById = { ...state.nodesById };
      for (const [id, node] of Object.entries(nodesById)) {
        if (node.track_id === trackId) delete nodesById[id];
      }
      return { tracks: state.tracks.filter((t) => t.id !== trackId), nodesById };
    });
    scheduleLayoutRefresh();
  },
}));
