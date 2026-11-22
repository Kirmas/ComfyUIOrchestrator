const DEFAULT_BASE_URL = import.meta.env.DEV ? "http://localhost:8000" : "";

export function getApiBaseUrl(): string {
  return localStorage.getItem("orchestrator.apiBaseUrl") || DEFAULT_BASE_URL;
}

export function setApiBaseUrl(url: string): void {
  localStorage.setItem("orchestrator.apiBaseUrl", url);
}

export function getApiToken(): string {
  return localStorage.getItem("orchestrator.apiToken") || "";
}

export function setApiToken(token: string): void {
  localStorage.setItem("orchestrator.apiToken", token);
}

export function getWsBaseUrl(): string {
  const base = getApiBaseUrl();
  if (base) return base.replace(/^http/, "ws");
  return `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}`;
}

/** Asset URLs from the API are same-origin-relative paths (e.g.
 * "/api/assets/<id>/file?token=..."); prepend the configured API base URL so
 * <img>/<model-viewer> tags resolve correctly even when the frontend is
 * served from a different origin/port than the API (e.g. `npm run dev`). */
export function resolveAssetUrl(path: string | null | undefined): string {
  if (!path) return "";
  if (/^https?:\/\//.test(path)) return path;
  return `${getApiBaseUrl()}${path}`;
}

/** The asset's original with ?download=true: the server then sends a
 * Content-Disposition filename (dashboard name if it's that dashboard's result,
 * else the asset's own name, else an automatic one), which also beats the
 * <a download> attribute -- so the naming rule lives only in assets.py. */
export function assetDownloadUrl(asset: { url: string | null }): string {
  const url = resolveAssetUrl(asset.url);
  return url ? `${url}${url.includes("?") ? "&" : "?"}download=true` : "";
}

/** What a grid-sized <img> should point at: the asset's thumbnail when the
 * backend has one, the original otherwise. Kept next to resolveAssetUrl so the
 * "which URL does a cell use" decision lives in exactly one place -- full-size
 * views (zoom, compare, download) deliberately keep calling resolveAssetUrl. */
export function resolveAssetPreviewUrl(asset: { url: string | null; preview_url?: string | null }): string {
  return resolveAssetUrl(asset.preview_url ?? asset.url);
}

class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${getApiBaseUrl()}${path}`, {
    ...init,
    headers: {
      ...(init?.body && !(init.body instanceof FormData) ? { "Content-Type": "application/json" } : {}),
      Authorization: `Bearer ${getApiToken()}`,
      ...init?.headers,
    },
  });
  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText);
    // FastAPI's HTTPException body is {"detail": "..."} -- extract it so
    // callers that just do `alert(err.message)` (Settings.tsx's tryDelete,
    // etc.) show the actual reason instead of the raw JSON envelope. Falls
    // back to the raw text for any non-JSON-detail error body.
    let message = text || res.statusText;
    try {
      const parsed = JSON.parse(text);
      if (typeof parsed?.detail === "string") message = parsed.detail;
    } catch {
      // not JSON -- keep the raw text
    }
    throw new ApiError(res.status, message);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

/** POST JSON with upload progress (fetch can't report it). onProgress gets 0..1. */
function postWithProgress<T>(path: string, body: unknown, onProgress: (fraction: number) => void): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `${getApiBaseUrl()}${path}`);
    xhr.setRequestHeader("Content-Type", "application/json");
    xhr.setRequestHeader("Authorization", `Bearer ${getApiToken()}`);
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) onProgress(e.loaded / e.total);
    };
    xhr.onerror = () => reject(new ApiError(0, "Network error"));
    xhr.onload = () => {
      let parsed: unknown = null;
      try {
        parsed = JSON.parse(xhr.responseText);
      } catch {
        // not JSON
      }
      if (xhr.status >= 200 && xhr.status < 300) return resolve(parsed as T);
      const detail = (parsed as { detail?: unknown } | null)?.detail;
      reject(new ApiError(xhr.status, typeof detail === "string" ? detail : xhr.responseText || xhr.statusText));
    };
    xhr.send(JSON.stringify(body));
  });
}

export const api = {
  postProgress: postWithProgress,
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: "POST", body: body !== undefined ? JSON.stringify(body) : undefined }),
  postForm: <T>(path: string, form: FormData) => request<T>(path, { method: "POST", body: form }),
  patch: <T>(path: string, body: unknown) => request<T>(path, { method: "PATCH", body: JSON.stringify(body) }),
  put: <T>(path: string, body: unknown) => request<T>(path, { method: "PUT", body: JSON.stringify(body) }),
  delete: (path: string) => request<void>(path, { method: "DELETE" }),
};

export { ApiError };
