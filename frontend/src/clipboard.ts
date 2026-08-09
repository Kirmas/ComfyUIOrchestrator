/** One-slot clipboards for carrying something between dashboards.
 *
 * "+ ref elsewhere" and every other placement gesture is click-to-complete
 * inside one rendered grid, so none of them can reach across a subgraph
 * boundary: the target cell isn't on screen at the same time as the source.
 * Copying an id out into storage and pasting it in the other grid is the way
 * across.
 *
 * Deliberately NOT a browsable list of everything in the project. For assets
 * the board's reference library is already that, and a grid has far more asset
 * cells than a board has stickers; for subgraphs there is no such list *by
 * design* (see CLAUDE.md -- reachability is structural, you arrive at a
 * dashboard by diving into a pointer, not by picking it off a menu). One slot,
 * copy again for the next one. Deliberately localStorage rather than component
 * state: these are often carried across a page reload, and losing the copy to
 * an accidental F5 would be its own small annoyance.
 */
import { useEffect, useState } from "react";

// One event for every slot: a paste affordance re-reads its own slot when any
// of them changes, which costs a localStorage read and saves a per-slot event
// name that nothing would benefit from telling apart. Storage events only fire
// in *other* tabs, so this one has to be dispatched explicitly -- otherwise the
// affordance wouldn't appear until some unrelated re-render happened.
const CHANGED = "clipboard-slot-changed";

export interface ClipboardSlot<T> {
  copy(entry: T): void;
  read(): T | null;
}

export function clipboardSlot<T extends object>(key: string, isValid: (entry: T) => boolean): ClipboardSlot<T> {
  return {
    copy(entry: T) {
      localStorage.setItem(key, JSON.stringify(entry));
      window.dispatchEvent(new CustomEvent(CHANGED));
    },
    read(): T | null {
      const raw = localStorage.getItem(key);
      if (!raw) return null;
      try {
        const parsed = JSON.parse(raw) as T;
        return parsed && isValid(parsed) ? parsed : null;
      } catch {
        // Written by an older version, or hand-edited -- drop it rather than
        // letting a parse error break every grid render.
        return null;
      }
    },
  };
}

/** Subscribed read, for anything that renders a paste affordance. */
export function useClipboardSlot<T extends object>(slot: ClipboardSlot<T>): T | null {
  const [entry, setEntry] = useState<T | null>(() => slot.read());
  useEffect(() => {
    const sync = () => setEntry(slot.read());
    window.addEventListener(CHANGED, sync);
    window.addEventListener("storage", sync);
    return () => {
      window.removeEventListener(CHANGED, sync);
      window.removeEventListener("storage", sync);
    };
  }, [slot]);
  return entry;
}

/** Only the asset id is meaningful. `explicit` refs resolve by asset id alone
 * on both ends (explicit_ref_asset in core/asset_types.py, resolveSlotAsset
 * here), so a pasted reference works regardless of which dashboard the original
 * lives in. The label and thumbnail are only there so the paste affordance can
 * say what it is about to place. */
export interface CopiedAsset {
  assetId: string;
  label: string;
  url: string | null;
}

export const assetClipboard = clipboardSlot<CopiedAsset>(
  "comfy-orchestrator:assetClipboard",
  (entry) => typeof entry.assetId === "string",
);

/** A dashboard, carried so a second asset cell somewhere else can be pointed at
 * it (POST /api/dashboards/{id}/pointers). Extra pointers are non-tree edges --
 * they never affect reachability, so unlike the owner they're always safe to
 * delete. The name is only for labelling the paste affordance. */
export interface CopiedSubgraph {
  dashboardId: string;
  name: string;
}

export const subgraphClipboard = clipboardSlot<CopiedSubgraph>(
  "comfy-orchestrator:subgraphClipboard",
  (entry) => typeof entry.dashboardId === "string",
);
