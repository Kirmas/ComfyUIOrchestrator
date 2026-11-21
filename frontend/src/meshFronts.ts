/** Which view of a mesh counts as its front.
 *
 * Saved on the server (Asset.meta.front_orbit: theta, phi, radius) because a
 * model's front is a fact about the model -- STL in particular has none of its
 * own. Every view of a mesh (grid still, fullscreen viewer, compare, board
 * sticker) reads it through useFront, and this store is what makes them all
 * follow a change at once: each holds its own copy of the Asset, which goes
 * stale the moment the front is saved from another one. */
import { useSyncExternalStore } from "react";
import { assetsApi } from "./api/endpoints";
import type { Asset } from "./types";

/** The default front: theta 0 is the model's +Z (what a glTF is authored
 * toward), at model-viewer's own default elevation and framing distance. */
export const FRONT_ORBIT = "0deg 75deg 105%";

type Orbit = number[] | null;

const saved = new Map<string, Orbit>();
const listeners = new Set<() => void>();
let version = 0;

const subscribe = (fn: () => void) => {
  listeners.add(fn);
  return () => listeners.delete(fn);
};

/** The front to use for this asset: a just-saved one wins over the Asset's own. */
export function frontOf(asset: Asset): Orbit {
  return saved.has(asset.id) ? saved.get(asset.id)! : (asset.front_orbit ?? null);
}

/** Re-renders the caller whenever any mesh's front changes. */
export function useFront(asset: Asset): Orbit {
  useSyncExternalStore(subscribe, () => version);
  return frontOf(asset);
}

/** model-viewer's camera-orbit attribute for a front. Fixed decimals: a plain
 * Number#toString() writes tiny values as exponents, which it can't parse. */
export function orbitAttr(front: Orbit): string {
  return front ? `${front[0].toFixed(6)}rad ${front[1].toFixed(6)}rad ${front[2].toFixed(6)}m` : FRONT_ORBIT;
}

/** Saves (or, with null, resets) the front and tells every view. */
export async function saveFront(asset: Asset, orbit: Orbit): Promise<void> {
  await assetsApi.setFront(asset.id, orbit);
  saved.set(asset.id, orbit);
  version += 1;
  listeners.forEach((fn) => fn());
}
