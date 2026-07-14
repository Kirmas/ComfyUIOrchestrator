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
    throw new ApiError(res.status, text || res.statusText);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: "POST", body: body !== undefined ? JSON.stringify(body) : undefined }),
  postForm: <T>(path: string, form: FormData) => request<T>(path, { method: "POST", body: form }),
  patch: <T>(path: string, body: unknown) => request<T>(path, { method: "PATCH", body: JSON.stringify(body) }),
  delete: (path: string) => request<void>(path, { method: "DELETE" }),
};

export { ApiError };
