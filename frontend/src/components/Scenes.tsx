import { useCallback, useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { resolveAssetUrl } from "../api/client";
import { scenesApi } from "../api/endpoints";
import { useT } from "../i18n";
import { SceneViewport } from "./SceneViewport";
import type { SceneInfo, SceneListing, SceneSummary } from "../types";
import { cx, formatBytes } from "../utils";

const compact = (n: number) => (n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1e3 ? `${Math.round(n / 1e3)}k` : String(n));

/** The Scenes page: what the agents' live Blender workshops are costing and a
 * way to get rid of them without a shell. A scene's files are not assets or DB
 * rows (backend core/blender_scene.py), so this list is the only place a person
 * sees them -- which is the whole point: nothing should ever need an SSH
 * session to clean up. */
export function Scenes({ projectId }: { projectId: string | null }) {
  const t = useT();
  const [data, setData] = useState<SceneListing | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [auto, setAuto] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [zoom, setZoom] = useState<SceneSummary | null>(null);
  const [viewing, setViewing] = useState<SceneSummary | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [details, setDetails] = useState<{ id: string; info?: SceneInfo; error?: string } | null>(null);

  const reload = useCallback(
    () =>
      scenesApi
        .list()
        .then((r) => {
          setError(null);
          setData(r);
        })
        .catch((err) => setError(err instanceof Error ? err.message : t("scenes.loadFailed"))),
    [t],
  );

  useEffect(() => {
    reload();
    if (!auto) return;
    const id = setInterval(reload, 4000);
    return () => clearInterval(id);
  }, [auto, reload]);

  const act = async (id: string, fn: () => Promise<unknown>) => {
    setBusy(id);
    try {
      await fn();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(null);
      reload();
    }
  };

  const ago = (ts: number) => {
    const min = Math.max(0, Math.round((Date.now() / 1000 - ts) / 60));
    if (min < 1) return t("scenes.ago.now");
    if (min < 60) return t("scenes.ago.min", { n: min });
    if (min < 60 * 24) return t("scenes.ago.hour", { n: Math.round(min / 60) });
    return t("scenes.ago.day", { n: Math.round(min / 60 / 24) });
  };

  const openDetails = (scene: SceneSummary) => {
    if (details?.id === scene.id) {
      setDetails(null);
      return;
    }
    setDetails({ id: scene.id });
    scenesApi
      .info(scene.id)
      .then((info) => setDetails((d) => (d?.id === scene.id ? { id: scene.id, info } : d)))
      .catch((err) => setDetails((d) => (d?.id === scene.id ? { id: scene.id, error: String(err.message ?? err) } : d)));
  };

  const closed = data?.scenes.filter((s) => s.state === "closed") ?? [];

  return (
    <div className="settings-panel">
      <div className="settings-section">
        <div className="node-cell-header">
          <span className="scenes-summary">
            {data &&
              t("scenes.summary", {
                live: data.live,
                max: data.max_live,
                count: data.scenes.length,
                size: formatBytes(data.disk_bytes),
                mem: formatBytes(data.mem_available_mb * 1024 * 1024),
              })}
          </span>
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <label style={{ fontWeight: 400, fontSize: 12, display: "flex", gap: 4, alignItems: "center" }}>
              <input type="checkbox" checked={auto} onChange={(e) => setAuto(e.target.checked)} />
              {t("scenes.autoRefresh")}
            </label>
            <button onClick={reload}>{t("common.refresh")}</button>
            {closed.length > 0 && (
              <button
                className="scene-danger"
                onClick={() => {
                  if (!confirm(t("scenes.confirmDeleteClosed", { n: closed.length }))) return;
                  act("*", () => Promise.all(closed.map((s) => scenesApi.remove(s.id))));
                }}
              >
                {t("scenes.deleteClosed")}
              </button>
            )}
          </div>
        </div>
        {error && <div className="error-text">{error}</div>}
        {notice && <div className="scenes-note">{notice}</div>}
        {data && data.scenes.length === 0 && <p className="scenes-empty">{t("scenes.empty")}</p>}
        <div className="scene-list">
          {data?.scenes.map((scene) => (
            <SceneCard
              key={scene.id}
              scene={scene}
              busy={busy === scene.id || busy === "*"}
              ago={ago}
              details={details?.id === scene.id ? details : null}
              onZoom={() => setZoom(scene)}
              onView={() => setViewing(scene)}
              onSave={() => {
                if (!projectId) {
                  setError(t("scenes.noProject"));
                  return;
                }
                setNotice(null);
                act(scene.id, async () => {
                  const saved = await scenesApi.saveToStorage(scene.id, projectId, scene.name);
                  setNotice(t("scenes.saved", { name: saved.name ?? scene.name }));
                });
              }}
              onDetails={() => openDetails(scene)}
              onClose={() => act(scene.id, () => scenesApi.close(scene.id))}
              onDelete={() => {
                if (confirm(t("scenes.confirmDelete", { name: scene.name }))) act(scene.id, () => scenesApi.remove(scene.id));
              }}
            />
          ))}
        </div>
        <p className="scenes-note">{t("scenes.keptNote")}</p>
      </div>
      {viewing && <SceneViewport scene={viewing} onClose={() => setViewing(null)} />}
      {zoom?.render_url &&
        createPortal(
          <div className="image-modal-backdrop" onClick={() => setZoom(null)}>
            <div className="image-modal-content" onClick={(e) => e.stopPropagation()}>
              <img src={resolveAssetUrl(zoom.render_url)} alt={zoom.name} style={{ maxWidth: "100%", maxHeight: "85vh", display: "block" }} />
              <button style={{ position: "absolute", top: 6, right: 6 }} onClick={() => setZoom(null)}>
                {t("common.close")}
              </button>
            </div>
          </div>,
          document.body,
        )}
    </div>
  );
}

export function SceneCard({
  scene,
  busy,
  ago,
  details,
  onZoom,
  onView,
  onSave,
  onDetails,
  onClose,
  onDelete,
}: {
  scene: SceneSummary;
  busy: boolean;
  ago: (ts: number) => string;
  details: { id: string; info?: SceneInfo; error?: string } | null;
  onZoom: () => void;
  onView: () => void;
  onSave: () => void;
  onDetails: () => void;
  onClose: () => void;
  onDelete: () => void;
}) {
  const t = useT();
  const open = scene.state !== "closed";
  return (
    <div className={cx("scene-card", open && "open")}>
      <div className="scene-card-main">
        <button className="scene-thumb" onClick={onZoom} disabled={!scene.preview_url} title={scene.name}>
          {scene.preview_url ? <img src={resolveAssetUrl(scene.preview_url)} alt="" /> : <span>{t("scenes.noPreview")}</span>}
        </button>
        <div className="scene-body">
          <div className="scene-title">
            <strong>{scene.name}</strong>
            <span className={cx("scene-state", scene.state)}>{t(`scenes.state.${scene.state}` as const)}</span>
          </div>
          {scene.inputs.map((input, i) => (
            <div key={i} className="scene-line">
              {input.triangles != null
                ? input.simplified_from
                  ? t("scenes.simplified", { to: compact(input.triangles), from: compact(input.simplified_from) })
                  : t("scenes.triangles", { n: compact(input.triangles) })
                : `${input.kind} (${input.file})`}
              {input.note && !input.simplified_from ? ` — ${input.note}` : ""}
            </div>
          ))}
          <div className="scene-line dim">
            {open && scene.rss_mb != null
              ? t("scenes.memory", { now: scene.rss_mb, peak: scene.peak_rss_mb })
              : t("scenes.memoryClosed", { peak: scene.peak_rss_mb })}
            {" · "}
            {t("scenes.disk", { size: formatBytes(scene.disk_bytes) })}
          </div>
          <div className="scene-line dim">
            {t("scenes.activity", { ago: ago(scene.last_activity) })}
            {" · "}
            {t("scenes.counts", { runs: scene.runs, renders: scene.renders })}
          </div>
          {scene.error && <div className="scene-line error-text">{t("scenes.stopped", { reason: scene.error })}</div>}
        </div>
        <div className="scene-actions">
          <button onClick={onView} disabled={busy} title={t("scenes.viewTitle")}>
            {t("scenes.view")}
          </button>
          <button onClick={onSave} disabled={busy} title={t("scenes.saveToStorageTitle")}>
            {t("scenes.saveToStorage")}
          </button>
          <button onClick={onDetails} disabled={busy || !open} title={open ? undefined : t("scenes.detailsClosed")}>
            {t("scenes.details")}
          </button>
          <button onClick={onClose} disabled={busy || !open} title={t("scenes.closeTitle")}>
            {t("scenes.close")}
          </button>
          <button className="scene-danger" onClick={onDelete} disabled={busy}>
            {t("scenes.delete")}
          </button>
        </div>
      </div>
      {details && <SceneDetails details={details} />}
    </div>
  );
}

export function SceneDetails({ details }: { details: { info?: SceneInfo; error?: string } }) {
  const t = useT();
  if (details.error) return <div className="scene-details error-text">{details.error}</div>;
  if (!details.info) return <div className="scene-details dim">…</div>;
  const { blender } = details.info;
  const [from, to] = blender.frame_range;
  return (
    <div className="scene-details">
      <table>
        <thead>
          <tr>
            <th>{t("scenes.col.name")}</th>
            <th>{t("scenes.col.type")}</th>
            <th>{t("scenes.col.size")}</th>
            <th>{t("scenes.col.detail")}</th>
          </tr>
        </thead>
        <tbody>
          {blender.objects.map((o) => (
            <tr key={o.name}>
              <td>{o.name}</td>
              <td>{o.type}</td>
              <td>{o.dimensions.map((d) => d.toFixed(2)).join(" × ")}</td>
              <td>
                {[
                  o.faces != null ? t("scenes.faces", { n: compact(o.faces) }) : null,
                  o.bone_count != null ? t("scenes.bones", { n: o.bone_count }) : null,
                  o.modifiers?.length ? o.modifiers.join(", ") : null,
                ]
                  .filter(Boolean)
                  .join(" · ")}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="scene-line dim">
        {blender.actions.length > 0
          ? `${blender.actions.map((a) => a.name).join(", ")} — ${t("scenes.frameRange", { from, to, fps: blender.fps })}`
          : t("scenes.noAnimation")}
      </div>
    </div>
  );
}
