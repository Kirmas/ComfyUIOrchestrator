import { assetsApi } from "./api/endpoints";
import { assetFace, storeOutputsFor } from "./assetNodes";
import type { Asset, InputRef, NodeItem, Track } from "./types";

/** Whatever asset node sits at row `homeTrack.row_index + rowOffset`, one
 * column back from `node` -- the cell_index resolution rule, mirroring
 * `_asset_at_cell_index` in worker/tasks.py. Factored out of
 * resolveSlotAsset's cell_index branch so a not-yet-assigned row offset can
 * be probed the same way an existing ref is (see templateUtils.ts's
 * defaultInputsForSchema, which matches a fresh slot to whatever's already
 * sitting in its row by asset kind). */
export function assetNodeAtRowOffset(
  node: NodeItem,
  rowOffset: number,
  tracks: Track[],
  nodesById: Record<string, NodeItem>,
): NodeItem | null {
  const homeTrack = tracks.find((t) => t.id === node.track_id);
  if (!homeTrack) return null;
  const targetRow = homeTrack.row_index + rowOffset;
  const targetStep = node.step_index - 1;
  return (
    Object.values(nodesById).find((n) => {
      if (n.kind !== "asset" || n.step_index !== targetStep) return false;
      return tracks.find((t) => t.id === n.track_id)?.row_index === targetRow;
    }) ?? null
  );
}

/** Frontend mirror of resolve_node_inputs' image-ref resolution (worker/tasks.py) --
 * for preview purposes only (no generation happens here), so it just needs to land
 * on the same Asset, not read its bytes. Used to find what image actually sits behind
 * an "image"/"file" slot so the crop-preview modal has something to show.
 *
 * Two ref types, matching the backend. Four others (self_prev, track_below_prev,
 * upload, text) predate the row-span paradigm and were removed in 2026-08 once
 * nothing created them and no row in the DB carried one -- see types/index.ts. */
export async function resolveSlotAsset(
  node: NodeItem,
  slotIndex: number,
  tracks: Track[],
  nodesById: Record<string, NodeItem>,
  outputsFor = storeOutputsFor,
): Promise<Asset | null> {
  const ref: InputRef | undefined = node.inputs[slotIndex];
  if (!ref) return null;

  if (ref.type === "explicit") {
    if (!ref.output_id) return null;
    return assetsApi.get(ref.output_id).catch(() => null);
  }

  if (ref.type === "cell_index") {
    // A node's row is always exactly its track's row_index -- moving a node
    // to a different row means reassigning its track_id (see Grid.tsx's
    // dropAssetAt/applyRowMove), never a display-only override.
    const assetNode = assetNodeAtRowOffset(node, ref.index, tracks, nodesById);
    if (!assetNode) return null;
    return assetFace(assetNode, outputsFor);
  }

  return null;
}
