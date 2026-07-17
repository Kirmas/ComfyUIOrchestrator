import { assetsApi } from "./api/endpoints";
import { useProjectStore } from "./state/projectStore";
import type { Asset, InputRef, NodeItem, Track } from "./types";

/** Picks the same asset the backend would for a "settled" cell: the one
 * flagged selected, else the most recently created (see
 * _selected_or_latest_output in worker/tasks.py) -- outputs are already
 * ordered by created_at by the API, so "last" is "latest". */
function selectedOrLatest(assets: Asset[]): Asset | null {
  if (assets.length === 0) return null;
  return assets.find((a) => a.selected) ?? assets[assets.length - 1];
}

function nearestAssetNodeBefore(nodes: NodeItem[], trackId: string, beforeStepIndex: number): NodeItem | null {
  const candidates = nodes.filter((n) => n.track_id === trackId && n.kind === "asset" && n.step_index < beforeStepIndex);
  if (candidates.length === 0) return null;
  return candidates.reduce((latest, n) => (n.step_index > latest.step_index ? n : latest));
}

/** Frontend mirror of resolve_node_inputs' image-ref resolution (worker/tasks.py) --
 * for preview purposes only (no generation happens here), so it just needs to land
 * on the same Asset, not read its bytes. Used to find what image actually sits behind
 * an "image"/"file" slot so the crop-preview modal has something to show. */
export async function resolveSlotAsset(
  node: NodeItem,
  slotIndex: number,
  tracks: Track[],
  nodesById: Record<string, NodeItem>,
  outputsByNode: Record<string, Asset[]>,
  refreshNodeOutputs: (nodeId: string) => Promise<void>,
): Promise<Asset | null> {
  const ref: InputRef | undefined = node.inputs[slotIndex];
  if (!ref) return null;

  const outputsFor = async (nodeId: string): Promise<Asset[]> => {
    if (outputsByNode[nodeId]) return outputsByNode[nodeId];
    await refreshNodeOutputs(nodeId).catch(() => undefined);
    return useProjectStore.getState().outputsByNode[nodeId] ?? [];
  };

  if (ref.type === "upload" || ref.type === "explicit") {
    const assetId = ref.type === "upload" ? ref.asset_id : ref.output_id;
    if (!assetId) return null;
    return assetsApi.get(assetId).catch(() => null);
  }

  if (ref.type === "self_prev") {
    const priorAssetNode = nearestAssetNodeBefore(Object.values(nodesById), node.track_id, node.step_index);
    if (!priorAssetNode) return null;
    return selectedOrLatest(await outputsFor(priorAssetNode.id));
  }

  if (ref.type === "track_below_prev") {
    const track = tracks.find((t) => t.id === node.track_id);
    if (!track) return null;
    const below = tracks.find((t) => t.project_id === track.project_id && t.row_index === track.row_index + 1);
    if (!below) return null;
    const priorAssetNode = nearestAssetNodeBefore(Object.values(nodesById), below.id, node.step_index + 1);
    if (!priorAssetNode) return null;
    return selectedOrLatest(await outputsFor(priorAssetNode.id));
  }

  if (ref.type === "cell_index") {
    // Mirrors _asset_at_cell_index in worker/tasks.py: whatever asset node's
    // row (its track's row_index) equals this workflow node's own home row
    // (its track's row_index) + index, one column back. A node's row is
    // always exactly its track's row_index -- moving a node to a different
    // row means reassigning its track_id (see Grid.tsx's dropAssetAt/
    // applyRowMove), never a display-only override.
    const homeTrack = tracks.find((t) => t.id === node.track_id);
    if (!homeTrack) return null;
    const targetRow = homeTrack.row_index + ref.index;
    const targetStep = node.step_index - 1;
    const assetNode = Object.values(nodesById).find((n) => {
      if (n.kind !== "asset" || n.step_index !== targetStep) return false;
      return tracks.find((t) => t.id === n.track_id)?.row_index === targetRow;
    });
    if (!assetNode) return null;
    return selectedOrLatest(await outputsFor(assetNode.id));
  }

  return null; // "text" -- no image
}
