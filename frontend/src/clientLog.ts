// Client-side error/console log, kept in the browser so problems that only
// reproduce on a phone (where there's no dev console) can still be read back
// on the site itself (Logs view -> "Browser" tab). Buffered in memory and
// mirrored to localStorage so it survives a reload/crash; capped to MAX so it
// can't grow without bound. Not sent to the backend -- purely session-local.

export type ClientLogEntry = { t: string; level: "error" | "warn" | "info"; msg: string };

const KEY = "comfy-orchestrator:clientLog";
const MAX = 300;

let buffer: ClientLogEntry[] = load();
const listeners = new Set<() => void>();

function load(): ClientLogEntry[] {
  try {
    const raw = localStorage.getItem(KEY);
    return raw ? (JSON.parse(raw) as ClientLogEntry[]) : [];
  } catch {
    return [];
  }
}

function persist() {
  try {
    localStorage.setItem(KEY, JSON.stringify(buffer));
  } catch {
    /* quota exceeded / private mode -- keep the in-memory copy only */
  }
}

function fmt(arg: unknown): string {
  if (arg instanceof Error) return arg.stack || `${arg.name}: ${arg.message}`;
  if (typeof arg === "string") return arg;
  try {
    return JSON.stringify(arg);
  } catch {
    return String(arg);
  }
}

export function logClient(level: ClientLogEntry["level"], ...args: unknown[]) {
  const entry: ClientLogEntry = { t: new Date().toISOString(), level, msg: args.map(fmt).join(" ") };
  buffer.push(entry);
  if (buffer.length > MAX) buffer = buffer.slice(-MAX);
  persist();
  listeners.forEach((l) => l());
}

export function getClientLog(): ClientLogEntry[] {
  return buffer;
}

export function clearClientLog() {
  buffer = [];
  persist();
  listeners.forEach((l) => l());
}

export function subscribeClientLog(cb: () => void): () => void {
  listeners.add(cb);
  return () => {
    listeners.delete(cb);
  };
}

let installed = false;

/** Install global error hooks. Idempotent; call once at startup. */
export function installClientLogging() {
  if (installed) return;
  installed = true;

  window.addEventListener("error", (e) => {
    const where = e.filename ? ` (${e.filename}:${e.lineno}:${e.colno})` : "";
    // e.error is null for cross-origin script errors -- fall back to message.
    const detail = (e.error && (e.error.stack || e.error.message)) || e.message || "Unknown error";
    logClient("error", detail + where);
  });

  window.addEventListener("unhandledrejection", (e) => {
    const r = e.reason;
    logClient("error", "Unhandled promise rejection:", r instanceof Error ? r.stack || r.message : String(r));
  });

  // Mirror console.error/warn into the buffer, still passing through to the
  // real console. try/catch so a logging failure can never break the app.
  (["error", "warn"] as const).forEach((level) => {
    const orig = console[level].bind(console);
    console[level] = (...args: unknown[]) => {
      try {
        logClient(level, ...args);
      } catch {
        /* ignore */
      }
      orig(...args);
    };
  });

  logClient("info", `client log started — ${navigator.userAgent}`);
}
