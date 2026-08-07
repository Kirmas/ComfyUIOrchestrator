/** A one-slot clipboard for carrying an asset between dashboards.
 *
 * "+ ref elsewhere" is a click-to-complete gesture that only works inside one
 * rendered grid, so it can't reach across a subgraph boundary: the target cell
 * isn't on screen at the same time as the source. Copying an asset id out into
 * storage and pasting it in the other grid is the way across.
 *
 * Deliberately NOT a browsable list of every asset in the project (the board's
 * reference library is that, and a grid has far more asset cells than a board
 * has stickers) -- one slot, copy it again for the next one. Deliberately
 * localStorage rather than component state: a reference is often carried across
 * a page reload, and losing the copy to an accidental F5 would be its own small
 * annoyance.
 *
 * Only the asset id is meaningful. `explicit` refs resolve by asset id alone on
 * both ends (explicit_ref_asset in core/asset_types.py, resolveSlotAsset here), so
 * a pasted reference works regardless of which dashboard the original lives in.
 * The label and thumbnail are only there so the paste affordance can say what
 * it is about to place.
 */

const KEY = "comfy-orchestrator:assetClipboard";

export interface CopiedAsset {
  assetId: string;
  /** Only for showing the user what's on the clipboard; never used to resolve. */
  label: string;
  url: string | null;
}

export function copyAsset(entry: CopiedAsset): void {
  localStorage.setItem(KEY, JSON.stringify(entry));
  // Storage events only fire in *other* tabs, so tell this one explicitly --
  // otherwise the paste affordance wouldn't appear until the next re-render
  // happened to be triggered by something else.
  window.dispatchEvent(new CustomEvent("asset-clipboard-changed"));
}

export function readClipboard(): CopiedAsset | null {
  const raw = localStorage.getItem(KEY);
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as CopiedAsset;
    return parsed && typeof parsed.assetId === "string" ? parsed : null;
  } catch {
    // Written by an older version, or hand-edited -- drop it rather than
    // letting a parse error break every grid render.
    return null;
  }
}

