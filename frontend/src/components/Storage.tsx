import { useCallback, useEffect, useRef, useState } from "react";
import { assetsApi, scenesApi, storageApi } from "../api/endpoints";
import { useT } from "../i18n";
import type { Asset } from "../types";
import { formatBytes } from "../utils";

/** Per-project file storage: .blend files kept so they are not lost. Not the
 * board, not the grid -- a place to put a file and get it back. Double-click
 * opens a copy as a live scene (the stored original is never modified; a scene
 * itself is disposable and gets cleaned up). */
export function Storage({
  projectId,
  scenesAvailable,
  onOpenScene,
}: {
  projectId: string;
  scenesAvailable: boolean;
  onOpenScene: () => void;
}) {
  const t = useT();
  const [files, setFiles] = useState<Asset[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const input = useRef<HTMLInputElement>(null);

  const reload = useCallback(
    () =>
      storageApi
        .list(projectId)
        .then((r) => {
          setError(null);
          setFiles(r);
        })
        .catch((err) => setError(err instanceof Error ? err.message : String(err))),
    [projectId],
  );
  useEffect(() => {
    setFiles(null);
    reload();
  }, [reload]);

  const run = async (id: string, fn: () => Promise<unknown>) => {
    setBusy(id);
    setError(null);
    try {
      await fn();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(null);
      reload();
    }
  };

  const upload = (list: FileList | null) => {
    const picked = Array.from(list ?? []);
    if (!picked.length) return;
    run("*upload", async () => {
      for (const f of picked) await storageApi.upload(projectId, f);
    });
  };

  const openAsScene = (file: Asset) => {
    if (!scenesAvailable || busy) return;
    run(file.id, async () => {
      await scenesApi.create([file.id], displayName(file));
      onOpenScene();
    });
  };

  const q = query.trim().toLowerCase();
  const shown = (files ?? []).filter((f) => !q || displayName(f).toLowerCase().includes(q));

  return (
    <div className="settings-panel">
      <div className="settings-section">
        <div className="node-cell-header">
          <span className="scenes-summary">{t("storage.hint")}</span>
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <input placeholder={t("storage.search")} value={query} onChange={(e) => setQuery(e.target.value)} />
            <button onClick={() => input.current?.click()} disabled={busy === "*upload"}>
              {busy === "*upload" ? t("storage.uploading") : t("storage.upload")}
            </button>
            <input
              ref={input}
              type="file"
              multiple
              accept=".blend,.blend1"
              style={{ display: "none" }}
              onChange={(e) => {
                upload(e.target.files);
                e.target.value = "";
              }}
            />
          </div>
        </div>
        {error && <div className="error-text">{error}</div>}
        {files && files.length === 0 && <p className="scenes-empty">{t("storage.empty")}</p>}
        {busy && busy !== "*upload" && <p className="scenes-note">{t("storage.opening")}</p>}
        <div className="scene-list">
          {shown.map((file) => (
            <div
              key={file.id}
              className="scene-card storage-file"
              onDoubleClick={() => openAsScene(file)}
              title={scenesAvailable ? t("storage.dblclick") : undefined}
            >
              <div className="scene-card-main">
                <span className="storage-icon">▣</span>
                <div className="scene-body">
                  <strong>
                    {displayName(file)}
                    {file.meta.agent_saved === true && <span className="scene-state running storage-agent-mark"> {t("storage.agentSaved")}</span>}
                  </strong>
                  <div className="scene-line dim">
                    {new Date(file.created_at).toLocaleString()}
                    {typeof file.meta.size === "number" ? ` · ${formatBytes(file.meta.size)}` : ""}
                  </div>
                </div>
                <div className="scene-actions">
                  <button onClick={() => openAsScene(file)} disabled={!!busy || !scenesAvailable} title={t("storage.openTitle")}>
                    {t("storage.open")}
                  </button>
                  {file.meta.agent_saved === true && (
                    <button
                      disabled={!!busy}
                      title={t("storage.clearMarkTitle")}
                      onClick={() => run(file.id, () => storageApi.setAgentMark(projectId, file.id, false))}
                    >
                      {t("storage.clearMark")}
                    </button>
                  )}
                  <button onClick={() => (window.location.href = storageApi.downloadUrl(file.id))}>{t("storage.download")}</button>
                  <button
                    disabled={!!busy}
                    onClick={() => {
                      const name = prompt(t("storage.renamePrompt"), displayName(file));
                      if (name != null && name.trim()) run(file.id, () => assetsApi.rename(file.id, name.trim()));
                    }}
                  >
                    {t("storage.rename")}
                  </button>
                  <button
                    className="scene-danger"
                    disabled={!!busy}
                    onClick={() => {
                      if (confirm(t("storage.confirmDelete", { name: displayName(file) }))) run(file.id, () => assetsApi.remove(file.id));
                    }}
                  >
                    {t("common.delete")}
                  </button>
                </div>
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

const displayName = (f: Asset) => f.name || String(f.meta.filename ?? "file").replace(/\.blend1?$/i, "");
