import { create } from "zustand";
import { resolveAssetUrl } from "../api/client";
import { assetFace } from "../assetNodes";
import type { NodeItem } from "../types";
import { useProjectStore } from "./projectStore";

/** A grid cell attached to the next message from the agent dock. The agent
 * gets the ids (and reads the rest through MCP); the preview, if any, goes
 * along as an image so it can see the picture without a tool call. */
export type AgentNodeRef = {
  projectId: string;
  nodeId: string;
  dashboardId: string | null;
  label: string;
  previewUrl: string | null;
};

/** The project-chat dock shown over the grid, board and design doc
 * (AgentDock.tsx). A store rather than props because the gestures that feed
 * it live deep in Grid/NodeCell, and the dock itself sits in App. */
type AgentDockState = {
  open: boolean;
  /** Tapping a grid cell attaches it instead of doing what a tap does. */
  picking: boolean;
  refs: AgentNodeRef[];
  setOpen: (open: boolean) => void;
  setPicking: (picking: boolean) => void;
  addRef: (ref: AgentNodeRef) => void;
  removeRef: (nodeId: string) => void;
  /** Only this project's: the others wait for their own dock. */
  clearRefs: (projectId: string) => void;
};

// Attached-but-unsent cells survive a reload, like the draft text does
// (chatDrafts.ts). Just ids and a label, so localStorage is plenty.
const REFS_KEY = "comfy-orchestrator:agentDockRefs";

function storedRefs(): AgentNodeRef[] {
  try {
    const list = JSON.parse(localStorage.getItem(REFS_KEY) ?? "[]");
    return Array.isArray(list) ? list.filter((r) => r && r.projectId && r.nodeId) : [];
  } catch {
    return [];
  }
}

export const useAgentDock = create<AgentDockState>((set) => ({
  open: false,
  picking: false,
  refs: storedRefs(),
  setOpen: (open) => set(open ? { open } : { open, picking: false }),
  setPicking: (picking) => set({ picking }),
  // Attaching always ends a pick and brings the sheet back with the chip in it.
  addRef: (ref) =>
    set((s) => ({
      refs: s.refs.some((r) => r.nodeId === ref.nodeId) ? s.refs : [...s.refs, ref],
      open: true,
      picking: false,
    })),
  removeRef: (nodeId) => set((s) => ({ refs: s.refs.filter((r) => r.nodeId !== nodeId) })),
  clearRefs: (projectId) => set((s) => ({ refs: s.refs.filter((r) => r.projectId !== projectId) })),
}));

useAgentDock.subscribe((s, prev) => {
  if (s.refs !== prev.refs) localStorage.setItem(REFS_KEY, JSON.stringify(s.refs));
});

/** Attach a grid cell: position and type for the chip, the cell's face (if
 * it's a picture) for the agent to look at. A workflow cell has no face; the
 * agent reads its settings and results by id. */
export async function attachNodeToAgent(node: NodeItem, typeLabel: string): Promise<void> {
  const { projectId, tracks, dashboardId } = useProjectStore.getState();
  if (!projectId) return;
  const row = tracks.find((tr) => tr.id === node.track_id)?.row_index;
  const face = node.kind === "asset" ? await assetFace(node).catch(() => null) : null;
  const src = face?.kind === "image" ? (face.fit_url ?? face.url) : null;
  useAgentDock.getState().addRef({
    projectId,
    nodeId: node.id,
    dashboardId,
    label: `${typeLabel} · ${row === undefined ? "?" : row + 1}:${node.step_index + 1}`,
    previewUrl: src ? resolveAssetUrl(src) : null,
  });
}
