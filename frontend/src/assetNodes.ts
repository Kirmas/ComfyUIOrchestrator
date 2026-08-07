/** Asset-kind node behavior, one class per `asset.*` node_type.
 *
 * Frontend mirror of `backend/app/core/asset_types.py` -- same four kinds,
 * same two questions (what is this cell's *face*, and does it own its own
 * Asset rows), deliberately a separate implementation rather than shared code
 * across the language split (see CLAUDE.md). Change both together.
 *
 * This replaces the `node.node_type === "asset.refasset"` chains that had been
 * copy-pasted into Grid.tsx (isPickable, resolvePrimaryOutput),
 * slotResolution.ts (faceOf) and NodeCell.tsx. They had already drifted apart:
 * `asset.subgraph` was handled in faceOf but not in isPickable or
 * resolvePrimaryOutput, so a smart pointer rendered a picture yet refused to be
 * compared with one.
 *
 * Rendering stays where it was -- NodeCell.tsx's NODE_VIEWS registry, keyed by
 * the same node_type strings. This module is the behavior half of that split:
 * what a cell *is*, not what it looks like.
 */
import { assetsApi, dashboardsApi } from "./api/endpoints";
import { useProjectStore } from "./state/projectStore";
import type { Asset, NodeItem } from "./types";

/** Reads a node's own Asset rows, cache-first (the caller supplies the store's
 * `outputsByNode` lookup -- the grid deliberately avoids re-fetching images it
 * already has). */
export type OutputsFor = (nodeId: string) => Promise<Asset[]>;

export abstract class AssetNodeKind {
  abstract readonly nodeType: string;
  /** True if Asset rows hang off this node. False for the pointer kinds, which
   * borrow someone else's -- so `outputs` is always empty for them and no check
   * may be written in terms of `outputs.length`. */
  readonly ownsAssets: boolean = true;
  /** The undecided-candidates picker: several variants, none chosen yet. */
  readonly isPicker: boolean = false;

  /** The one asset this cell stands for -- what zoom, compare, "+ ref
   * elsewhere", copy-ref and every position-based slot lookup all want. */
  async face(node: NodeItem, outputsFor: OutputsFor): Promise<Asset | null> {
    const assets = await outputsFor(node.id);
    if (assets.length === 0) return null;
    return assets.find((a) => a.selected) ?? assets[assets.length - 1];
  }

  /** Eligible as a compare source/target and as a "pick cell..." target.
   *
   * One rule for all four kinds rather than a per-kind table: the question is
   * only ever "is there a single well-defined picture here". A picker has
   * several undecided ones, so it's out (picking it would silently grab
   * whichever happened to be first -- never a choice the user made; resolve it
   * with ★ first and it becomes an asset.single). A kind that owns its assets
   * needs at least one. A pointer kind owns none by definition and resolves its
   * face on demand, so it qualifies as soon as it exists.
   */
  pickable(outputs: Asset[]): boolean {
    if (this.isPicker) return false;
    return this.ownsAssets ? outputs.length > 0 : true;
  }
}

/** Shared by the two pointer kinds: no Asset row of its own, so its outputs are
 * just the single borrowed asset it stands for. */
abstract class BorrowedFaceAssetNode extends AssetNodeKind {
  override readonly ownsAssets = false;
}

class SingleAssetNode extends AssetNodeKind {
  readonly nodeType = "asset.single";
}

class SelectAssetNode extends AssetNodeKind {
  readonly nodeType = "asset.select";
  override readonly isPicker = true;
}

/** A lightweight pointer to another node's (or the board library's) asset, made
 * via "+ ref elsewhere" -- one picture in more than one cell, no duplicated
 * file. Resolves by asset id alone: a board-library reference carries no
 * node_id at all, and a grid one's node_id only exists so Grid.tsx can draw the
 * arrow back to the source cell. */
class RefAssetNode extends BorrowedFaceAssetNode {
  readonly nodeType = "asset.refasset";

  override async face(node: NodeItem): Promise<Asset | null> {
    const ref = node.inputs[0];
    if (!ref || ref.type !== "explicit" || !ref.output_id) return null;
    return assetsApi.get(ref.output_id).catch(() => null);
  }
}

/** A smart pointer: an asset cell that also opens a sub-dashboard. Its face is
 * whichever asset inside that dashboard was chosen to represent it
 * (Dashboard.result_asset_id), not anything hung off the pointer node. */
class SubgraphAssetNode extends BorrowedFaceAssetNode {
  readonly nodeType = "asset.subgraph";

  override async face(node: NodeItem): Promise<Asset | null> {
    if (!node.subgraph_dashboard_id) return null;
    const dashboard = await dashboardsApi.get(node.subgraph_dashboard_id).catch(() => null);
    if (!dashboard?.result_asset_id) return null;
    return assetsApi.get(dashboard.result_asset_id).catch(() => null);
  }
}

const KINDS: AssetNodeKind[] = [new SingleAssetNode(), new SelectAssetNode(), new RefAssetNode(), new SubgraphAssetNode()];

export const ASSET_NODE_KINDS: Record<string, AssetNodeKind> = Object.fromEntries(KINDS.map((k) => [k.nodeType, k]));

const DEFAULT_ASSET_NODE = ASSET_NODE_KINDS["asset.single"];

/** null for a workflow node. An asset node whose node_type is unset or
 * unrecognized falls back to the plain owns-its-own-assets behavior, which is
 * what every call site did implicitly before. */
export function assetNodeKind(node: NodeItem): AssetNodeKind | null {
  if (node.kind !== "asset") return null;
  return (node.node_type && ASSET_NODE_KINDS[node.node_type]) || DEFAULT_ASSET_NODE;
}

/** Convenience wrapper: the face of any node, or null if it isn't an asset. */
export function assetFace(node: NodeItem, outputsFor: OutputsFor = storeOutputsFor): Promise<Asset | null> {
  return assetNodeKind(node)?.face(node, outputsFor) ?? Promise.resolve(null);
}

/** The default OutputsFor: the project store's own cache, fetching only on a
 * miss. Reads through getState() rather than a subscribed snapshot so it stays
 * usable from plain async code -- and, more importantly, so the grid never
 * re-fetches full-size images it already holds (see projectStore.loadProject's
 * carriedOutputs note: that re-fetch is what used to make a move hang). */
export async function storeOutputsFor(nodeId: string): Promise<Asset[]> {
  const cached = useProjectStore.getState().outputsByNode[nodeId];
  if (cached) return cached;
  await useProjectStore.getState().refreshNodeOutputs(nodeId).catch(() => undefined);
  return useProjectStore.getState().outputsByNode[nodeId] ?? [];
}
