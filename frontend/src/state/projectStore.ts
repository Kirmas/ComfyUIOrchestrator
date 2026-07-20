import { create } from "zustand";
import { nodesApi, projectsApi, tracksApi } from "../api/endpoints";
import type { Asset, NodeItem, ProgressEvent, Track } from "../types";

interface ProjectState {
  projectId: string | null;
  tracks: Track[];
  nodesById: Record<string, NodeItem>;
  outputsByNode: Record<string, Asset[]>;

  loadProject: (projectId: string) => Promise<void>;
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

  loadProject: async (projectId: string) => {
    const tracks = await projectsApi.tracks(projectId);
    const nodesById: Record<string, NodeItem> = {};
    for (const track of tracks) {
      const nodes = await tracksApi.nodes(track.id);
      for (const node of nodes) nodesById[node.id] = node;
    }
    set({ projectId, tracks, nodesById, outputsByNode: {} });

    for (const node of Object.values(nodesById)) {
      if (node.status === "done") {
        get()
          .refreshNodeOutputs(node.id)
          .catch(() => undefined);
      }
    }
  },

  refreshTrack: async (trackId: string) => {
    const nodes = await tracksApi.nodes(trackId);
    set((state) => {
      const nodesById = { ...state.nodesById };
      for (const node of nodes) nodesById[node.id] = node;
      return { nodesById };
    });
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
              const track = await tracksApi.get(fetched.track_id);
              get().addTrack(track);
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

  addTrack: (track: Track) => set((state) => ({ tracks: [...state.tracks, track] })),
  setTracks: (tracks: Track[]) => set({ tracks }),
  addNode: (node: NodeItem) => set((state) => ({ nodesById: { ...state.nodesById, [node.id]: node } })),
  replaceNode: (oldId: string, node: NodeItem) =>
    set((state) => {
      const nodesById = { ...state.nodesById };
      delete nodesById[oldId];
      nodesById[node.id] = node;
      return { nodesById };
    }),
  setNode: (node: NodeItem) => set((state) => ({ nodesById: { ...state.nodesById, [node.id]: node } })),
  removeNode: (nodeId: string) =>
    set((state) => {
      const nodesById = { ...state.nodesById };
      delete nodesById[nodeId];
      const outputsByNode = { ...state.outputsByNode };
      delete outputsByNode[nodeId];
      return { nodesById, outputsByNode };
    }),
  removeTrack: (trackId: string) =>
    set((state) => {
      const nodesById = { ...state.nodesById };
      for (const [id, node] of Object.entries(nodesById)) {
        if (node.track_id === trackId) delete nodesById[id];
      }
      return { tracks: state.tracks.filter((t) => t.id !== trackId), nodesById };
    }),
}));
