/** Unsent agent-chat drafts (text + attached pictures), kept per chat across
 * page reloads. IndexedDB rather than localStorage: one attached picture can
 * be ~3.5 MB of base64, past localStorage's whole ~5 MB quota. */

const DB_NAME = "comfy-orchestrator";
const STORE = "agentDrafts";

export type ChatDraft = {
  text: string;
  attachments: { media_type: string; data: string; preview: string }[];
};

let dbPromise: Promise<IDBDatabase> | null = null;

function db(): Promise<IDBDatabase> {
  dbPromise ??= new Promise((ok, fail) => {
    const req = indexedDB.open(DB_NAME, 1);
    req.onupgradeneeded = () => req.result.createObjectStore(STORE);
    req.onsuccess = () => ok(req.result);
    req.onerror = () => fail(req.error);
  });
  return dbPromise;
}

function run<T>(mode: IDBTransactionMode, fn: (store: IDBObjectStore) => IDBRequest<T>): Promise<T> {
  return db().then(
    (d) =>
      new Promise<T>((ok, fail) => {
        const req = fn(d.transaction(STORE, mode).objectStore(STORE));
        req.onsuccess = () => ok(req.result);
        req.onerror = () => fail(req.error);
      }),
  );
}

export function loadChatDraft(chatId: string): Promise<ChatDraft | null> {
  return run<ChatDraft | undefined>("readonly", (s) => s.get(chatId)).then(
    (d) => d ?? null,
    () => null,
  );
}

/** An empty draft deletes the entry rather than storing nothing. */
export function saveChatDraft(chatId: string, draft: ChatDraft): Promise<void> {
  const empty = !draft.text.trim() && !draft.attachments.length;
  return run<unknown>("readwrite", (s) => (empty ? s.delete(chatId) : s.put(draft, chatId)) as IDBRequest<unknown>).then(
    () => undefined,
    (err) => console.warn("Could not save chat draft", err),
  );
}
