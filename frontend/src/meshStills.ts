/** Stills of 3D meshes, rendered once per asset and kept.
 *
 * A grid cell used to mount a live WebGL viewer for every mesh on every visit,
 * so a project with a dozen models re-parsed a dozen GLBs each time it opened.
 * Now the first time a mesh is on screen its front is captured into a small
 * image, stored in IndexedDB keyed by asset id (assets are immutable, so the
 * key never goes stale), and every later view is a plain <img>.
 *
 * IndexedDB rather than the Cache API: Cache API only exists in secure
 * contexts, and this app is served over plain http on the LAN. Per-browser
 * storage is the trade-off -- a second device renders its own stills once.
 */

const DB_NAME = "comfy-orchestrator-mesh-stills";
const STORE = "stills";

let dbPromise: Promise<IDBDatabase | null> | null = null;

function openDb(): Promise<IDBDatabase | null> {
  dbPromise ??= new Promise((resolve) => {
    if (typeof indexedDB === "undefined") return resolve(null);
    const request = indexedDB.open(DB_NAME, 1);
    request.onupgradeneeded = () => request.result.createObjectStore(STORE);
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => resolve(null);
  });
  return dbPromise;
}

async function readBlob(assetId: string): Promise<Blob | null> {
  const db = await openDb();
  if (!db) return null;
  return new Promise((resolve) => {
    const request = db.transaction(STORE, "readonly").objectStore(STORE).get(assetId);
    request.onsuccess = () => resolve((request.result as Blob | undefined) ?? null);
    request.onerror = () => resolve(null);
  });
}

async function writeBlob(assetId: string, blob: Blob): Promise<void> {
  const db = await openDb();
  if (!db) return;
  await new Promise<void>((resolve) => {
    const tx = db.transaction(STORE, "readwrite");
    tx.objectStore(STORE).put(blob, assetId);
    tx.oncomplete = () => resolve();
    tx.onerror = () => resolve();
  });
}

// One object URL per asset for the life of the page: every cell showing the
// same mesh shares it, and the lookup happens once, not once per mount.
const urlByAsset = new Map<string, Promise<string | null>>();

/** An object URL for the stored still of this asset, or null if none exists yet. */
export function loadStill(assetId: string): Promise<string | null> {
  let entry = urlByAsset.get(assetId);
  if (!entry) {
    entry = readBlob(assetId).then((blob) => (blob ? URL.createObjectURL(blob) : null));
    urlByAsset.set(assetId, entry);
  }
  return entry;
}

/** Forgets the stored still for an asset, so the next render captures a fresh one. */
export async function dropStill(assetId: string): Promise<void> {
  const pending = urlByAsset.get(assetId);
  urlByAsset.delete(assetId);
  const url = await pending;
  if (url) URL.revokeObjectURL(url);
  const db = await openDb();
  if (!db) return;
  await new Promise<void>((resolve) => {
    const tx = db.transaction(STORE, "readwrite");
    tx.objectStore(STORE).delete(assetId);
    tx.oncomplete = () => resolve();
    tx.onerror = () => resolve();
  });
}

/** Keeps a freshly captured still and returns the URL to show it with. */
export async function saveStill(assetId: string, dataUrl: string): Promise<string> {
  const blob = await (await fetch(dataUrl)).blob();
  await writeBlob(assetId, blob);
  const url = URL.createObjectURL(blob);
  urlByAsset.set(assetId, Promise.resolve(url));
  return url;
}

// Captures are serialized. Each one mounts a live WebGL viewer, and a page full
// of mesh cells mounting them all at once is what a browser's context limit
// (and a 4-core box) doesn't survive. A cell waits its turn and renders alone.
let rendering = false;
const waiting: (() => void)[] = [];

export function acquireRenderSlot(): Promise<void> {
  if (!rendering) {
    rendering = true;
    return Promise.resolve();
  }
  return new Promise((resolve) => waiting.push(resolve));
}

export function releaseRenderSlot(): void {
  const next = waiting.shift();
  if (next) next();
  else rendering = false;
}
