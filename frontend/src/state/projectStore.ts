import { create } from "zustand";
import { nodesApi, projectsApi, tracksApi } from "../api/endpoints";
import type { Annotation, Asset, NodeItem, ProgressEvent, Track } from "../types";

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

type NavEntry = { dashboardId: string | null; name: string };

// Which sub-dashboard (if any) a project was last showing, so an F5 (or just
// coming back to a project via the picker) lands where the session actually
// was instead of always resetting to the main grid -- same "put you back
// where you were" philosophy App.tsx already applies to project/view choice.
// Keyed per-project since the trail only means something within one project.
const NAV_STACK_KEY_PREFIX = "comfy-orchestrator:navStack:";

function readStoredNavStack(projectId: string): NavEntry[] | null {
  try {
    const raw = localStorage.getItem(NAV_STACK_KEY_PREFIX + projectId);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) && parsed.length > 0 ? parsed : null;
  } catch {
    return null;
  }
}

function storeNavStack(projectId: string, navStack: NavEntry[]) {
  try {
    localStorage.setItem(NAV_STACK_KEY_PREFIX + projectId, JSON.stringify(navStack));
  } catch {
    // Storage full/unavailable -- losing the remembered scope isn't worth
    // failing navigation over.
  }
}

interface ProjectState {
  projectId: string | null;
  // Which grid scope is on screen: null = the project's main grid, otherwise a
  // sub-dashboard id. Row order, spans and blocked cells are only meaningful
  // within one scope, so every scoped fetch reads this rather than taking it
  // as an argument -- that way tracks and layout can never come from two
  // different grids.
  dashboardId: string | null;
  // The navigation trail, including where we are now: entry 0 is always the
  // project's main grid, the last entry is the current scope. This is history,
  // deliberately NOT a structural parent -- several smart pointers may open the
  // same dashboard, so which one you came through is a fact about this session,
  // not about the data.
  navStack: { dashboardId: string | null; name: string }[];
  tracks: Track[];
  nodesById: Record<string, NodeItem>;
  outputsByNode: Record<string, Asset[]>;
  // Backend-computed derived layout -- see core/grid_layout.py. spans keyed by
  // workflow node id; blockedCells is a Set of "row:col" strings a spanning
  // card covers in its own column. The frontend reads these instead of
  // recomputing the span formula.
  spans: Record<string, { desired: number; achieved: number }>;
  blockedCells: Set<string>;
  // Comment blocks. Held flat here; the frame each one draws is derived at
  // render time from where its member nodes currently are.
  annotations: Annotation[];

  loadProject: (projectId: string, dashboardId?: string | null) => Promise<void>;
  // Entry point for mounting a project fresh (initial load, F5, or picking a
  // project from the switcher): restores whichever scope that project's
  // localStorage trail last had, falling back to loadProject's own main-grid
  // default if there's nothing stored or the stored scope no longer resolves
  // (its dashboard was since deleted).
  restoreProject: (projectId: string) => Promise<void>;
  enterDashboard: (dashboardId: string, name: string) => Promise<void>;
  leaveDashboard: (toIndex: number) => Promise<void>;
  reloadTracks: (projectId: string) => Promise<void>;
  reloadLayout: (projectId: string) => Promise<void>;
  reloadAnnotations: (projectId: string) => Promise<void>;
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
  dashboardId: null,
  navStack: [{ dashboardId: null, name: "" }],
  tracks: [],
  nodesById: {},
  outputsByNode: {},
  spans: {},
  blockedCells: new Set(),
  annotations: [],

  loadProject: async (projectId: string, dashboardId: string | null = null) => {
    const tracks = withRowIndex(await projectsApi.tracks(projectId, dashboardId));

    // One parallel batch, not a waterfall: the per-track node fetches don't
    // depend on each other (nodesById is keyed by node id, so arrival order is
    // irrelevant) and layout/annotations don't depend on them either. Awaiting
    // them one at a time meant ~20 sequential round-trips on every single node
    // add/move/delete -- the wait was all latency, since each call itself takes
    // under 15 ms.
    const [nodesPerTrack, layout, annotations] = await Promise.all([
      Promise.all(tracks.map((track) => tracksApi.nodes(track.id))),
      projectsApi.layout(projectId, dashboardId).catch(() => ({ spans: {}, blocked_cells: [] as [number, number][] })),
      projectsApi.annotations(projectId).catch(() => [] as Annotation[]),
    ]);

    const nodesById: Record<string, NodeItem> = {};
    for (const nodes of nodesPerTrack) {
      for (const node of nodes) nodesById[node.id] = node;
    }

    // Carry the previous outputs over instead of clearing them. A bare
    // `outputsByNode: {}` made every <img> in the grid unmount and immediately
    // remount on each reload -- and since loadProject runs after every node
    // add/move/delete, that re-fetched and re-decoded the project's full-size
    // originals (300+ MB on humanChart) every single time, which is what made a
    // move appear to hang for seconds. Entries whose node is gone -- including
    // all of them when switching to a different project -- drop out here, and
    // the refreshNodeOutputs pass below still overwrites whatever may have
    // actually changed.
    const prevOutputs = get().outputsByNode;
    const carriedOutputs: Record<string, Asset[]> = {};
    for (const id of Object.keys(nodesById)) {
      if (prevOutputs[id]) carriedOutputs[id] = prevOutputs[id];
    }

    set({
      projectId,
      dashboardId,
      // Loading the main grid is either a project switch or a climb all the way
      // back out; either way the trail restarts at the root. enterDashboard and
      // leaveDashboard set the trail themselves after this returns.
      ...(dashboardId === null ? { navStack: [{ dashboardId: null, name: "" }] } : {}),
      tracks,
      nodesById,
      outputsByNode: carriedOutputs,
      spans: layout.spans,
      blockedCells: new Set(layout.blocked_cells.map(([r, c]) => `${r}:${c}`)),
      annotations,
    });
    if (dashboardId === null) storeNavStack(projectId, [{ dashboardId: null, name: "" }]);

    for (const node of Object.values(nodesById)) {
      if (node.status === "done") {
        get()
          .refreshNodeOutputs(node.id)
          .catch(() => undefined);
      }
    }
  },

  restoreProject: async (projectId: string) => {
    const stored = readStoredNavStack(projectId);
    const last = stored ? stored[stored.length - 1] : null;
    if (!last || last.dashboardId === null) {
      await get().loadProject(projectId);
      return;
    }
    try {
      await get().loadProject(projectId, last.dashboardId);
      set({ navStack: stored! });
    } catch {
      // The remembered dashboard no longer resolves (deleted since last
      // visit) -- fall back to the main grid rather than leaving the app on
      // a scope loadProject already failed to populate.
      storeNavStack(projectId, [{ dashboardId: null, name: "" }]);
      await get().loadProject(projectId);
    }
  },

  // Dive into a sub-dashboard through a smart pointer. The scope we're leaving
  // is pushed onto navStack, so "back" retraces the way this session actually
  // came -- a dashboard has no single parent to walk up to (several pointers
  // may open it), and its `owner_node_id` is about reachability, not about
  // where the user happens to be standing.
  enterDashboard: async (dashboardId: string, name: string) => {
    const { projectId, navStack } = get();
    if (!projectId) return;
    await get().loadProject(projectId, dashboardId);
    const next = [...navStack, { dashboardId, name }];
    set({ navStack: next });
    storeNavStack(projectId, next);
  },

  // Jump back to an earlier entry in the trail. `toIndex` is that entry's own
  // position, so the breadcrumb can climb several levels at once. Index 0 is
  // always the project's main grid.
  leaveDashboard: async (toIndex: number) => {
    const { projectId, navStack } = get();
    if (!projectId || toIndex < 0 || toIndex >= navStack.length) return;
    const target = navStack[toIndex];
    await get().loadProject(projectId, target.dashboardId);
    const next = navStack.slice(0, toIndex + 1);
    set({ navStack: next });
    storeNavStack(projectId, next);
  },

  // Re-fetch just the ordered track list (nodes don't move -- their track_id
  // is stable) and re-derive row_index from the new order. This is what every
  // structural track op (create/delete/splice) calls instead of the old
  // optimistic-shift + per-track PATCH reindex: one GET, no renumber writes,
  // nothing to leave half-applied. Span depends on track positions, so refresh
  // the layout too.
  reloadTracks: async (projectId: string) => {
    const tracks = withRowIndex(await projectsApi.tracks(projectId, get().dashboardId));
    set({ tracks });
    await get().reloadLayout(projectId);
  },

  reloadLayout: async (projectId: string) => {
    const layout = await projectsApi.layout(projectId, get().dashboardId);
    set({ spans: layout.spans, blockedCells: new Set(layout.blocked_cells.map(([r, c]) => `${r}:${c}`)) });
  },

  reloadAnnotations: async (projectId: string) => {
    set({ annotations: await projectsApi.annotations(projectId) });
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
