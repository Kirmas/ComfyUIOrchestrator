import { useEffect, useRef, useState } from "react";
import { assetsApi, backendsApi, capabilitiesApi, nodeTemplatesApi, projectsApi, systemApi } from "../api/endpoints";
import { NodeTypeDescription } from "./NodeTypeDescription";
import type {
  Backend,
  Capability,
  DetectedField,
  DirBrowseResult,
  MigrationStatus,
  NodeTemplate,
  Project,
  StorageInfo,
} from "../types";
import { NodeTypeWizard } from "./NodeTypeWizard";
import { MultiAngleBuilder } from "./MultiAngleBuilder";
import { capabilityUsesMultiAngleLora } from "../multiAngleLora";
import { LANGS, tr, useLangStore, useT } from "../i18n";

// Only one provider is actually wired up backend-side right now
// (GeminiImageBackend, api_backend.py's PROVIDERS registry) -- a friendly
// label here instead of a free-text box, with room to grow as more get
// implemented.
const KNOWN_PROVIDERS = [{ value: "nano_banana", label: "Google Gemini (“nano banana”)" }];

/** Delete buttons below have no other feedback mechanism -- without this, a
 * failed request (network error, FK conflict, wrong API base URL) just leaves
 * the row sitting there with zero indication that anything went wrong. */
async function tryDelete(action: () => Promise<unknown>, reload: () => void): Promise<void> {
  try {
    await action();
    reload();
  } catch (err) {
    alert(err instanceof Error ? err.message : tr("settings.deleteFailed"));
  }
}

function describeError(err: unknown): string {
  return err instanceof Error ? err.message : tr("settings.loadFailed");
}

/** The one setting that isn't stored server-side: the interface language lives
 * in localStorage (see i18n.ts) because it's a property of this browser, not
 * of the single shared account every device signs in with. */
function LanguageSection() {
  const t = useT();
  const lang = useLangStore((s) => s.lang);
  const setLang = useLangStore((s) => s.setLang);

  return (
    <div className="settings-section">
      <h2>{t("settings.language")}</h2>
      <div className="inline-form">
        <select value={lang} onChange={(e) => setLang(e.target.value as typeof lang)}>
          {LANGS.map((l) => (
            <option key={l.value} value={l.value}>
              {t(l.labelKey)}
            </option>
          ))}
        </select>
        <span style={{ fontSize: 12, color: "var(--text-dim)" }}>{t("settings.languageHint")}</span>
      </div>
    </div>
  );
}

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let v = n / 1024;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v.toFixed(1)} ${units[i]}`;
}

const MIGRATION_ACTIVE: MigrationStatus["status"][] = ["copying", "verifying"];

/** One combined "leftover" list, shown after a scan: files with no Asset row
 * (core/storage_gc.py's scan_orphans) and Asset rows with no owner
 * (scan_unowned_assets) are two different backend mechanisms, but the user
 * doesn't care about that split -- both are just "stuff the app forgot to
 * clean up", so they're merged into one list here instead of two sections. */
type LeftoverItem =
  | { key: string; kind: "file"; path: string; size_bytes: number; mime_type_guess: string }
  | { key: string; kind: "asset"; id: string; storage_key: string; mime_type: string; size_bytes: number | null; referenced: boolean };

/** One row of the combined leftover list -- owns its own busy/error state so
 * acting on one row never blocks or hides the rest. */
function LeftoverRow({ item, projects, onRemoved }: { item: LeftoverItem; projects: Project[]; onRemoved: (item: LeftoverItem) => void }) {
  const t = useT();
  const [projectId, setProjectId] = useState(projects[0]?.id ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const label = item.kind === "file" ? item.path : item.storage_key;
  const mime = item.kind === "file" ? item.mime_type_guess : item.mime_type;
  const isImage = mime.startsWith("image/");
  const referenced = item.kind === "asset" && item.referenced;
  const thumbSrc = item.kind === "file" ? systemApi.orphanPreviewUrl(item.path) : assetsApi.fileUrl(item.id);

  const del = async () => {
    if (!window.confirm(t("storage.confirmDeleteLeftover", { path: label }))) return;
    setBusy(true);
    setError(null);
    try {
      if (item.kind === "file") await systemApi.deleteOrphan(item.path);
      else await systemApi.deleteUnownedAsset(item.id);
      onRemoved(item);
    } catch (err) {
      setError(describeError(err));
      setBusy(false);
    }
  };
  const adopt = async () => {
    if (!projectId) return;
    setBusy(true);
    setError(null);
    try {
      if (item.kind === "file") await systemApi.adoptOrphan(item.path, projectId);
      else await systemApi.adoptUnownedAsset(item.id, projectId);
      onRemoved(item);
    } catch (err) {
      setError(describeError(err));
      setBusy(false);
    }
  };

  return (
    <div style={{ display: "flex", alignItems: "center", gap: 8, padding: "6px 0", borderBottom: "1px solid var(--border)" }}>
      {isImage ? (
        <img src={thumbSrc} alt="" style={{ width: 48, height: 48, objectFit: "cover", borderRadius: 4, flexShrink: 0 }} />
      ) : (
        <div
          style={{
            width: 48,
            height: 48,
            flexShrink: 0,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            fontSize: 10,
            color: "var(--text-dim)",
            border: "1px solid var(--border)",
            borderRadius: 4,
          }}
        >
          {mime.split("/")[0] || "?"}
        </div>
      )}
      <div style={{ flex: 1, fontSize: 11, minWidth: 0 }}>
        <div style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }} title={label}>
          {label}
        </div>
        <div style={{ color: "var(--text-dim)" }}>
          {item.size_bytes != null ? formatBytes(item.size_bytes) : "?"}
          {referenced && <span style={{ color: "var(--danger)", marginLeft: 6 }}>{t("storage.stillReferenced")}</span>}
        </div>
        {error && <div className="error-text">{error}</div>}
      </div>
      <select value={projectId} onChange={(e) => setProjectId(e.target.value)} disabled={busy || projects.length === 0}>
        {projects.map((p) => (
          <option key={p.id} value={p.id}>
            {p.name}
          </option>
        ))}
      </select>
      <button disabled={busy || !projectId} onClick={adopt}>
        {t("storage.adopt")}
      </button>
      <button disabled={busy || referenced} title={referenced ? t("storage.stillReferenced") : undefined} onClick={del}>
        {t("common.deleteLower")}
      </button>
    </div>
  );
}

/** Where generated/uploaded files live on disk (MEDIA_DIR, backend/app/config.py),
 * moving that location, and finding leftovers the app forgot to clean up.
 *
 * Moving: see core/storage_migration.py's docstring for the copy ->
 * independent hash-diff -> commit -> delete-old sequencing this mirrors.
 * Nothing here is "permanent" (old files deleted, config rewritten) until
 * the backend has already verified the new copy matches byte-for-byte; a
 * failed/interrupted run always leaves the original files exactly as they
 * were.
 *
 * Scanning: combines core/storage_gc.py's two GC mechanisms (files with no
 * Asset row, Asset rows with no owner) into one list -- see LeftoverItem
 * above for why they're not two separate sections. */
function StorageSection() {
  const t = useT();
  const [storage, setStorage] = useState<StorageInfo | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [migration, setMigration] = useState<MigrationStatus | null>(null);
  const pollRef = useRef<number | null>(null);

  const [browsing, setBrowsing] = useState(false);
  const [browse, setBrowse] = useState<DirBrowseResult | null>(null);
  const [browseError, setBrowseError] = useState<string | null>(null);
  const [manualPath, setManualPath] = useState("");
  const [newFolderName, setNewFolderName] = useState("");
  const [target, setTarget] = useState<string | null>(null);
  const [migrateError, setMigrateError] = useState<string | null>(null);

  const [projects, setProjects] = useState<Project[]>([]);
  const [leftovers, setLeftovers] = useState<{ items: LeftoverItem[]; missingFileCount: number; scanErrors: string[] } | null>(null);
  const [scanning, setScanning] = useState(false);
  const [scanError, setScanError] = useState<string | null>(null);

  useEffect(() => {
    projectsApi.list().then(setProjects).catch(() => {});
  }, []);

  const runScan = async () => {
    setScanning(true);
    setScanError(null);
    try {
      const [orphans, unowned] = await Promise.all([systemApi.orphans(), systemApi.unownedAssets()]);
      const items: LeftoverItem[] = [
        ...orphans.orphan_files.map((f) => ({ key: `file:${f.path}`, kind: "file" as const, ...f })),
        ...unowned.unowned_assets.map((a) => ({ key: `asset:${a.id}`, kind: "asset" as const, ...a })),
      ];
      setLeftovers({ items, missingFileCount: orphans.missing_file_count, scanErrors: orphans.scan_errors });
    } catch (err) {
      setScanError(describeError(err));
    } finally {
      setScanning(false);
    }
  };

  const removeLeftover = (item: LeftoverItem) => {
    setLeftovers((s) => (s ? { ...s, items: s.items.filter((it) => it.key !== item.key) } : s));
  };

  const leftoverSize = leftovers ? leftovers.items.reduce((sum, it) => sum + (it.size_bytes ?? 0), 0) : 0;

  const reloadStorage = () =>
    systemApi
      .storage()
      .then((s) => {
        setLoadError(null);
        setStorage(s);
      })
      .catch((err) => setLoadError(describeError(err)));

  const stopPolling = () => {
    if (pollRef.current !== null) {
      window.clearInterval(pollRef.current);
      pollRef.current = null;
    }
  };
  const startPolling = () => {
    if (pollRef.current !== null) return;
    pollRef.current = window.setInterval(async () => {
      try {
        const s = await systemApi.migrationStatus();
        setMigration(s);
        if (!MIGRATION_ACTIVE.includes(s.status)) {
          stopPolling();
          if (s.status === "done") reloadStorage();
        }
      } catch {
        // transient poll failure -- next tick retries, no need to surface it
      }
    }, 1500);
  };

  useEffect(() => {
    reloadStorage();
    // Pick up a migration that was already running before this page loaded
    // (e.g. a refresh mid-copy) instead of silently losing track of it.
    systemApi
      .migrationStatus()
      .then((s) => {
        setMigration(s);
        if (MIGRATION_ACTIVE.includes(s.status)) startPolling();
      })
      .catch(() => {});
    return stopPolling;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const loadDir = (path?: string) => {
    systemApi
      .browse(path)
      .then((r) => {
        setBrowse(r);
        setBrowseError(null);
        setManualPath(r.path);
      })
      .catch((err) => setBrowseError(describeError(err)));
  };
  const openBrowser = () => {
    setBrowsing(true);
    setBrowseError(null);
    loadDir(undefined);
  };
  const createFolder = async () => {
    if (!browse || !newFolderName.trim()) return;
    try {
      const path = `${browse.path.replace(/\/+$/, "")}/${newFolderName.trim()}`;
      await systemApi.mkdir(path);
      setNewFolderName("");
      loadDir(path);
    } catch (err) {
      setBrowseError(describeError(err));
    }
  };
  const chooseTarget = () => {
    if (!browse) return;
    setTarget(browse.path);
    setBrowsing(false);
  };
  const startMigrate = async () => {
    if (!target) return;
    setMigrateError(null);
    try {
      const s = await systemApi.migrate(target);
      setMigration(s);
      setTarget(null);
      startPolling();
    } catch (err) {
      setMigrateError(describeError(err));
    }
  };

  const migrating = migration ? MIGRATION_ACTIVE.includes(migration.status) : false;
  const filesPct = migration && migration.files_total > 0 ? (migration.files_done / migration.files_total) * 100 : 0;

  return (
    <div className="settings-section">
      <h2>{t("storage.title")}</h2>
      {loadError && <div className="error-text">{loadError}</div>}
      {storage && (
        <div style={{ fontSize: 12, display: "flex", flexDirection: "column", gap: 4 }}>
          <div>
            {t("storage.currentPath")}: <code>{storage.media_dir}</code>
          </div>
          <div className="progress-bar" title={t("storage.diskUsageTitle")}>
            <div className="progress-bar-fill" style={{ width: `${(storage.disk.used / storage.disk.total) * 100}%` }} />
          </div>
          <div style={{ color: "var(--text-dim)" }}>
            {t("storage.diskUsage", { used: formatBytes(storage.disk.used), total: formatBytes(storage.disk.total), free: formatBytes(storage.disk.free) })}
          </div>
          <div style={{ color: "var(--text-dim)" }}>
            {t("storage.mediaSize", { size: formatBytes(storage.size_bytes), files: storage.file_count, assets: storage.asset_count })}
          </div>
        </div>
      )}

      {migrating && migration && (
        <div style={{ marginTop: 10, fontSize: 12 }}>
          <div>{migration.status === "copying" ? t("storage.phaseCopying") : t("storage.phaseVerifying")}</div>
          <div className="progress-bar" style={{ marginTop: 4 }}>
            <div className="progress-bar-fill" style={{ width: `${filesPct}%` }} />
          </div>
          <div style={{ color: "var(--text-dim)", marginTop: 2 }}>
            {t("storage.progressCounts", {
              filesDone: migration.files_done,
              filesTotal: migration.files_total,
              bytesDone: formatBytes(migration.bytes_done),
              bytesTotal: formatBytes(migration.bytes_total),
            })}
          </div>
        </div>
      )}

      {!migrating && migration?.status === "done" && (
        <div style={{ marginTop: 10, fontSize: 12, color: "var(--success, #4caf50)" }}>
          {t("storage.migrationDone", { path: migration.new_path ?? "" })}{" "}
          <button onClick={() => setMigration(null)}>{t("common.close")}</button>
        </div>
      )}

      {!migrating && migration?.status === "error" && (
        <div style={{ marginTop: 10, fontSize: 12 }}>
          <div className="error-text">{t("storage.migrationError", { error: migration.error ?? "" })}</div>
          <div style={{ color: "var(--text-dim)" }}>{t("storage.oldUntouched")}</div>
          <button style={{ marginTop: 4 }} onClick={() => setMigration(null)}>
            {t("common.close")}
          </button>
        </div>
      )}

      {!migrating && !browsing && !target && !migrateError && (
        <div className="node-actions" style={{ marginTop: 10 }}>
          <button onClick={openBrowser}>{t("storage.changeLocation")}</button>
          <button disabled={scanning} onClick={runScan}>
            {scanning ? t("storage.scanning") : t("storage.scan")}
          </button>
        </div>
      )}

      {browsing && (
        <div className="inline-form" style={{ flexDirection: "column", alignItems: "stretch", gap: 6, marginTop: 10 }}>
          {browseError && <div className="error-text">{browseError}</div>}
          <div style={{ display: "flex", gap: 6 }}>
            <input style={{ flex: 1 }} value={manualPath} onChange={(e) => setManualPath(e.target.value)} placeholder={t("storage.pathPlaceholder")} />
            <button onClick={() => loadDir(manualPath)}>{t("storage.go")}</button>
          </div>
          {browse && (
            <>
              <div style={{ maxHeight: 220, overflowY: "auto", border: "1px solid var(--border)", borderRadius: 4 }}>
                {browse.parent && (
                  <div style={{ padding: "4px 8px", cursor: "pointer" }} onClick={() => loadDir(browse.parent!)}>
                    .. ({t("storage.up")})
                  </div>
                )}
                {browse.entries.map((entry) => (
                  <div
                    key={entry.path}
                    style={{ padding: "4px 8px", cursor: "pointer", opacity: entry.writable ? 1 : 0.5 }}
                    title={entry.writable ? undefined : t("storage.notWritable")}
                    onClick={() => loadDir(entry.path)}
                  >
                    {entry.name}
                  </div>
                ))}
                {browse.entries.length === 0 && <div style={{ padding: "4px 8px", color: "var(--text-dim)" }}>{t("storage.noSubfolders")}</div>}
              </div>
              <div style={{ display: "flex", gap: 6 }}>
                <input
                  style={{ flex: 1 }}
                  value={newFolderName}
                  onChange={(e) => setNewFolderName(e.target.value)}
                  placeholder={t("storage.folderNamePlaceholder")}
                />
                <button disabled={!newFolderName.trim()} onClick={createFolder}>
                  {t("storage.createFolder")}
                </button>
              </div>
              <div className="node-actions">
                <button className="primary" onClick={chooseTarget}>
                  {t("storage.useThisFolder")}
                </button>
                <button onClick={() => setBrowsing(false)}>{t("common.cancel")}</button>
              </div>
            </>
          )}
        </div>
      )}

      {target && !migrating && (
        <div className="inline-form" style={{ flexDirection: "column", alignItems: "stretch", gap: 6, marginTop: 10 }}>
          {migrateError && <div className="error-text">{migrateError}</div>}
          <div style={{ fontSize: 12 }}>
            {t("storage.confirmBody", {
              target,
              files: storage?.file_count ?? 0,
              size: formatBytes(storage?.size_bytes ?? 0),
              old: storage?.media_dir ?? "",
            })}
          </div>
          <div className="node-actions">
            <button className="primary" onClick={startMigrate}>
              {t("storage.confirmButton")}
            </button>
            <button onClick={() => setTarget(null)}>{t("common.cancel")}</button>
          </div>
        </div>
      )}

      {scanError && <div className="error-text" style={{ marginTop: 10 }}>{scanError}</div>}

      {leftovers && (
        <div style={{ marginTop: 10 }}>
          <div className="node-cell-hint">{t("storage.leftoverHint")}</div>
          {leftovers.scanErrors.length > 0 && (
            <div className="error-text" style={{ margin: "6px 0" }}>
              {t("storage.leftoverScanIncomplete", { count: leftovers.scanErrors.length })}
              <ul style={{ margin: "4px 0 0", paddingLeft: 18 }}>
                {leftovers.scanErrors.map((e, i) => (
                  <li key={i}>{e}</li>
                ))}
              </ul>
            </div>
          )}
          <div style={{ fontSize: 12, color: "var(--text-dim)", marginTop: 6 }}>
            {t("storage.leftoverSummary", { count: leftovers.items.length, size: formatBytes(leftoverSize) })}
          </div>
          {leftovers.missingFileCount > 0 && (
            <div style={{ fontSize: 12, color: "var(--text-dim)" }}>{t("storage.leftoverMissingCount", { count: leftovers.missingFileCount })}</div>
          )}
          {leftovers.items.length === 0 ? (
            <div style={{ fontSize: 12, color: "var(--text-dim)", marginTop: 6 }}>{t("storage.leftoverEmpty")}</div>
          ) : (
            <div style={{ marginTop: 8 }}>
              {leftovers.items.map((it) => (
                <LeftoverRow key={it.key} item={it} projects={projects} onRemoved={removeLeftover} />
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/** The "fetch a list, clear/set the load error, stash the result" shape
 * repeated for every section below (backends/capabilities/templates/API
 * keys) -- factored out so there's one place to get the error handling
 * right instead of four copies that could drift. */
function makeReloader<T>(fetcher: () => Promise<T>, setItems: (v: T) => void, setError: (msg: string | null) => void): () => Promise<void> {
  return () =>
    fetcher()
      .then((v) => {
        setError(null);
        setItems(v);
      })
      .catch((err) => setError(describeError(err)));
}

/** A paid provider's key lives on the api_provider Backend row itself -- one
 * key per Backend, shared by every node type whose Capability points at it
 * (see db/models.py's Backend.provider/api_key docstring). Want a second key
 * (a second Gemini account, say)? Add a second api_provider backend, don't
 * grant the same key twice. */
function BackendsSection({ items, reload }: { items: Backend[]; reload: () => void }) {
  const t = useT();
  const [name, setName] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [kind, setKind] = useState<"comfyui" | "api_provider">("comfyui");
  const [provider, setProvider] = useState(KNOWN_PROVIDERS[0].value);
  const [apiKey, setApiKey] = useState("");
  const [dailyLimit, setDailyLimit] = useState("");

  const create = async () => {
    if (!name.trim()) return;
    if (kind === "api_provider" && !apiKey.trim()) return;
    await backendsApi.create({
      name: name.trim(),
      kind,
      base_url: kind === "comfyui" ? baseUrl : null,
      provider: kind === "api_provider" ? provider : null,
      api_key: kind === "api_provider" ? apiKey.trim() : null,
      daily_limit: kind === "api_provider" && dailyLimit.trim() ? Number(dailyLimit) : null,
    });
    setName("");
    setBaseUrl("");
    setApiKey("");
    setDailyLimit("");
    reload();
  };

  return (
    <div className="settings-section">
      <h2>{t("backends.title")}</h2>
      <table>
        <thead>
          <tr>
            <th>{t("backends.colName")}</th>
            <th>{t("backends.colKind")}</th>
            <th>{t("backends.colUrl")}</th>
            <th>{t("backends.colActive")}</th>
            <th>{t("backends.colDailyLimit")}</th>
            <th>{t("backends.colUsed")}</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {items.map((b) => (
            <tr key={b.id}>
              <td>{b.name}</td>
              <td>{b.kind}</td>
              <td>
                {b.kind === "api_provider" ? (
                  <>
                    {KNOWN_PROVIDERS.find((p) => p.value === b.provider)?.label ?? b.provider ?? t("backends.noProvider")}
                    {" — "}
                    {b.has_api_key ? t("backends.keySet") : <span className="error-text">{t("backends.noKey")}</span>}
                  </>
                ) : (
                  b.base_url
                )}
              </td>
              <td>
                <input
                  type="checkbox"
                  checked={b.is_active}
                  onChange={async (e) => {
                    await backendsApi.update(b.id, { is_active: e.target.checked });
                    reload();
                  }}
                />
              </td>
              <td>
                {b.kind === "api_provider" && (
                  <input
                    type="number"
                    min={0}
                    style={{ width: 70 }}
                    placeholder="∞"
                    defaultValue={b.daily_limit ?? ""}
                    onBlur={async (e) => {
                      const v = e.target.value.trim();
                      await backendsApi.update(b.id, { daily_limit: v ? Number(v) : null });
                      reload();
                    }}
                  />
                )}
              </td>
              <td style={{ color: b.daily_limit != null && b.used_today >= b.daily_limit ? "var(--danger)" : undefined }}>
                {b.kind === "api_provider" ? `${b.used_today}${b.daily_limit != null ? ` / ${b.daily_limit}` : ""}` : ""}
              </td>
              <td>
                <button onClick={() => tryDelete(() => backendsApi.remove(b.id), reload)}>{t("common.deleteLower")}</button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="inline-form">
        <select value={kind} onChange={(e) => setKind(e.target.value as typeof kind)}>
          <option value="comfyui">comfyui</option>
          <option value="api_provider">api_provider</option>
        </select>
        <input placeholder={t("backends.namePlaceholder")} value={name} onChange={(e) => setName(e.target.value)} />
        {kind === "comfyui" ? (
          <input placeholder="http://host:8188" value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} style={{ width: 220 }} />
        ) : (
          <>
            <select value={provider} onChange={(e) => setProvider(e.target.value)}>
              {KNOWN_PROVIDERS.map((p) => (
                <option key={p.value} value={p.value}>
                  {p.label}
                </option>
              ))}
            </select>
            <input
              placeholder={t("backends.apiKeyPlaceholder")}
              type="password"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              style={{ width: 220 }}
            />
            <input
              placeholder={t("backends.dailyLimitPlaceholder")}
              style={{ width: 130 }}
              value={dailyLimit}
              onChange={(e) => setDailyLimit(e.target.value)}
            />
          </>
        )}
        <button className="primary" onClick={create}>
          {t("backends.add")}
        </button>
      </div>
    </div>
  );
}

/** Edits the literal prompt-shaped text values baked directly into a
 * capability's own workflow_json (CLIPTextEncode/TextEncodeQwenImageEditPlus
 * text, or a titled PrimitiveString(Multiline) value) that aren't already
 * exposed as a param_schema field -- see workflow_analyzer.py's
 * find_editable_text_fields. Until now the only way to change one of these
 * was re-uploading the whole workflow.json for this capability. */
function CapabilityTextFieldsModal({
  capability,
  onClose,
  readOnly = false,
  leaderName,
}: {
  capability: Capability;
  onClose: () => void;
  readOnly?: boolean;
  leaderName?: string;
}) {
  const t = useT();
  const [fields, setFields] = useState<DetectedField[] | null>(null);
  const [values, setValues] = useState<Record<string, string>>({});
  const [savingKey, setSavingKey] = useState<string | null>(null);
  const [savedKey, setSavedKey] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  // Dragging a <textarea>'s native resize handle past the modal's edge lands
  // the mouseup (and the click it synthesizes) directly on the backdrop --
  // same element a real "click outside to close" click would target -- so a
  // plain onClick={onClose} on the backdrop closed the modal mid-resize.
  // Only treating it as "outside" when the *mousedown* also started on the
  // backdrop itself (not just where the pointer happened to end up) tells
  // the two apart.
  const mouseDownOnBackdrop = useRef(false);
  // When this capability's workflow loads the Multiple-Angles LoRA, offer a
  // prompt-grammar builder under each field (the angle prompt is a baked-in
  // text node these fields edit -- see multiAngleLora.ts).
  const isMultiAngle = capabilityUsesMultiAngleLora(capability);

  useEffect(() => {
    capabilitiesApi
      .textFields(capability.id)
      .then((fs) => {
        setFields(fs);
        setValues(Object.fromEntries(fs.map((f) => [f.key, String(f.default ?? "")])));
      })
      .catch((err) => setLoadError(describeError(err)));
  }, [capability.id]);

  const save = async (f: DetectedField) => {
    setSavingKey(f.key);
    setSavedKey(null);
    try {
      if (f.is_variable) {
        await capabilitiesApi.updateVariableDefault(capability.id, { field_name: f.key, value: values[f.key] ?? "" });
      } else {
        await capabilitiesApi.updateTextField(capability.id, { node_id: f.node_id, input_key: f.input_key, value: values[f.key] ?? "" });
      }
      setSavedKey(f.key);
    } catch (err) {
      alert(describeError(err));
    } finally {
      setSavingKey(null);
    }
  };

  return (
    <div
      className="image-modal-backdrop"
      onMouseDown={(e) => {
        mouseDownOnBackdrop.current = e.target === e.currentTarget;
      }}
      onClick={() => {
        if (mouseDownOnBackdrop.current) onClose();
      }}
    >
      <div
        className="image-modal-content"
        onClick={(e) => e.stopPropagation()}
        style={{ width: 860, maxWidth: "92vw", maxHeight: "85vh", overflowY: "auto", display: "flex", flexDirection: "column" }}
      >
        <div className="node-cell-header">
          <span>{readOnly ? t("caps.promptFieldsLinked") : t("caps.promptFields")}</span>
          <button className="image-modal-close" onClick={onClose} title={t("common.close")}>
            ×
          </button>
        </div>
        {readOnly && (
          <div className="node-cell-hint">{t("caps.leaderHint", { name: leaderName ?? t("caps.leaderFallback") })}</div>
        )}
        {loadError && <div className="error-text">{loadError}</div>}
        {fields === null && !loadError && <div style={{ fontSize: 12, color: "var(--text-dim)" }}>{t("common.loading")}</div>}
        {fields?.length === 0 && <div style={{ fontSize: 12, color: "var(--text-dim)" }}>{t("caps.noTextFields")}</div>}
        {fields?.map((f) => (
          <div key={f.key} style={{ marginTop: 12 }}>
            <label style={{ display: "block", fontSize: 12, marginBottom: 4, color: "var(--text-dim)" }}>{f.label}</label>
            {f.is_variable && <div className="node-cell-hint" style={{ marginBottom: 4 }}>{t("caps.variableDefaultHint")}</div>}
            <textarea
              rows={14}
              readOnly={readOnly}
              style={{ width: "100%", minHeight: 240, resize: "vertical", ...(readOnly ? { opacity: 0.7 } : {}) }}
              value={values[f.key] ?? ""}
              onChange={(e) => {
                setValues((v) => ({ ...v, [f.key]: e.target.value }));
                setSavedKey(null);
              }}
            />
            {isMultiAngle && !readOnly && (
              <MultiAngleBuilder
                value={values[f.key] ?? ""}
                onChange={(next) => {
                  setValues((v) => ({ ...v, [f.key]: next }));
                  setSavedKey(null);
                }}
              />
            )}
            {!readOnly && (
              <div style={{ marginTop: 4, display: "flex", alignItems: "center", gap: 8 }}>
                <button disabled={savingKey === f.key} onClick={() => save(f)}>
                  {savingKey === f.key ? t("common.saving") : t("common.save")}
                </button>
                {savedKey === f.key && <span style={{ fontSize: 11, color: "var(--text-dim)" }}>{t("common.saved")}</span>}
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

/** Per-ComfyUI-instance prompt-link picker: make this instance follow another
 * instance's baked prompts (leader -> follower), or stay independent. Leaders
 * and followers are both scoped to one node type. A capability that already
 * leads others can't also follow someone (no chains). */
function PromptLinkControl({
  capability,
  siblings,
  backendName,
  reload,
}: {
  capability: Capability;
  siblings: Capability[];
  backendName: (id: string) => string;
  reload: () => void;
}) {
  const t = useT();
  const leaderId = (capability.config.prompt_leader_id as string | undefined) ?? "";
  const comfy = siblings.filter((s) => s.execution_type === "comfyui_workflow");
  const followers = comfy.filter((s) => s.config.prompt_leader_id === capability.id);

  if (followers.length > 0) {
    return (
      <span className="node-cell-hint" style={{ marginLeft: 6 }} title={t("caps.leadsTitle")}>
        {t("caps.leads", { n: followers.length })}
      </span>
    );
  }

  const leaders = comfy.filter((s) => s.id !== capability.id && !s.config.prompt_leader_id);
  if (leaders.length === 0 && !leaderId) return null;
  const hasLeaderOption = leaders.some((l) => l.id === leaderId);

  return (
    <select
      value={leaderId}
      title={t("caps.followTitle")}
      style={{ marginLeft: 6, maxWidth: 160 }}
      onChange={async (e) => {
        await capabilitiesApi.setPromptLink(capability.id, e.target.value || null);
        reload();
      }}
    >
      <option value="">{t("caps.independent")}</option>
      {leaders.map((l) => (
        <option key={l.id} value={l.id}>
          {t("caps.follow", { name: backendName(l.backend_id) })}
        </option>
      ))}
      {leaderId && !hasLeaderOption && <option value={leaderId}>{t("caps.followLinked")}</option>}
    </select>
  );
}

const ASPECT_RATIOS = ["Auto", "1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9", "2:1"];
// generationConfig.imageConfig.imageSize -- reliably honored only by
// gemini-3-pro-image-preview as of 2026-07; the flash variants largely
// ignore it and stay ~1K regardless (see api_backend.py's docstring).
const IMAGE_SIZES = ["Auto", "1K", "2K", "4K"];

// Gemini's own image-generation model ids -- not guessable, so a picker
// instead of a free-text box. "Custom…" escapes to a text input for
// whatever ships next that isn't in this list yet.
const KNOWN_GEMINI_MODELS = [
  { value: "gemini-2.5-flash-image", label: "Gemini 2.5 Flash Image (“nano banana”)" },
  { value: "gemini-3.1-flash-image-preview", label: "Gemini 3.1 Flash Image Preview (“nano banana 2”)" },
  { value: "gemini-3-pro-image-preview", label: "Gemini 3 Pro Image Preview (“nano banana pro”)" },
];
const CUSTOM_MODEL_OPTION = "__custom__";

/** Attaches a paid api_call Capability to an EXISTING template, against a
 * backend already picked by NodeTypeCard's unified "+ Add instance" chooser
 * -- node types themselves are still only ever created from a ComfyUI
 * workflow.json (NodeTypeWizard); this is the "bonus" second backend a
 * template can additionally offer. aspect_ratio/image_size are always baked
 * on (Gemini-specific, nothing on a ComfyUI-derived template to map to --
 * see 2026-07-20 incident where reusing a same-named ComfyUI resolution
 * field silently sent it an invalid value). The prompt has two shapes: "master" bakes a
 * fixed default instruction into a new "prompt" field (e.g. a Back View
 * node's "rotate this character 180°..."); "match" instead points
 * param_mapping.prompt at an existing text field on the template (e.g. a
 * generic CreateImage node's own "Text String (User Prompt)" field detected
 * from its ComfyUI workflow) so the user's own typed prompt flows straight
 * through instead of being shadowed by a second baked field. Either way the
 * field ends up rendered by NodeCell's existing param form (⚙), editable
 * per-node afterward. */
function AddApiInstanceForm({
  template,
  backend,
  onSaved,
  onCancel,
}: {
  template: NodeTemplate;
  backend: Backend;
  onSaved: () => void;
  onCancel: () => void;
}) {
  const t = useT();
  const [modelId, setModelId] = useState(KNOWN_GEMINI_MODELS[0].value);
  const [customModel, setCustomModel] = useState(false);
  const [masterPrompt, setMasterPrompt] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fields = template.param_schema.fields ?? [];
  // Reserved, namespaced names for whatever this form injects -- a plain
  // "prompt"/"aspect_ratio" once silently collided with a ComfyUI-derived
  // field of the same name (a workflow's own "Resolution Selector" node had
  // already claimed "aspect_ratio" as a free-text field holding ComfyUI-
  // style values like "1:1 (Square)"; ASPECT_RATIOS' Gemini-shaped enum was
  // then never added, and the field silently sent that ComfyUI string as
  // Gemini's aspectRatio -- not a valid value, and no new control ever
  // appeared for the user to notice or fix). "api_"-prefixed names can't
  // collide with anything workflow_analyzer derives from a node title.
  const PROMPT_FIELD = "api_prompt";
  const ASPECT_FIELD = "api_aspect_ratio";
  const IMAGE_SIZE_FIELD = "api_image_size";
  const hasPromptField = fields.some((f) => f.name === PROMPT_FIELD);
  const hasAspectField = fields.some((f) => f.name === ASPECT_FIELD);
  const hasImageSizeField = fields.some((f) => f.name === IMAGE_SIZE_FIELD);
  // A ComfyUI-derived template can already have its own free-text field
  // (e.g. a "Text String (User Prompt)" PrimitiveString detected by
  // workflow_analyzer) -- for a genuinely free-prompt node type like
  // CreateImage, that field IS the user's prompt and should just be mapped
  // to, not shadowed by a second baked prompt field nobody asked for.
  // Only offered when such a field actually exists; otherwise there's
  // nothing to map to and this collapses back to the plain master-prompt case.
  const existingTextFields = fields.filter((f) => f.type === "text");
  const [promptMode, setPromptMode] = useState<"master" | "match">("master");
  const [matchedField, setMatchedField] = useState(existingTextFields[0]?.name ?? "");

  const save = async () => {
    if (promptMode === "master" && !masterPrompt.trim()) return;
    if (promptMode === "match" && !matchedField) return;
    setSaving(true);
    setError(null);
    try {
      const promptFieldName = promptMode === "match" ? matchedField : PROMPT_FIELD;
      const needsNewPromptField = promptMode === "master" && !hasPromptField;
      if (needsNewPromptField || !hasAspectField || !hasImageSizeField) {
        const newFields = [...fields];
        if (needsNewPromptField) newFields.push({ name: PROMPT_FIELD, type: "text", label: "Prompt" });
        if (!hasAspectField) {
          newFields.push({ name: ASPECT_FIELD, type: "enum", label: "Aspect ratio", options: ASPECT_RATIOS });
        }
        if (!hasImageSizeField) {
          newFields.push({ name: IMAGE_SIZE_FIELD, type: "enum", label: "Image size (Gemini)", options: IMAGE_SIZES });
        }
        await nodeTemplatesApi.update(template.id, {
          param_schema: { ...template.param_schema, fields: newFields },
          defaults: {
            ...template.defaults,
            ...(needsNewPromptField ? { [PROMPT_FIELD]: masterPrompt } : {}),
            ...(!hasAspectField ? { [ASPECT_FIELD]: "Auto" } : {}),
            ...(!hasImageSizeField ? { [IMAGE_SIZE_FIELD]: "Auto" } : {}),
          },
        });
      }
      await capabilitiesApi.create({
        backend_id: backend.id,
        node_type_slug: template.node_type_slug,
        execution_type: "api_call",
        config: {
          model_id: modelId,
          param_mapping: { prompt: promptFieldName, aspect_ratio: ASPECT_FIELD, image_size: IMAGE_SIZE_FIELD },
        },
      });
      onSaved();
    } catch (err) {
      setError(describeError(err));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="inline-form" style={{ flexDirection: "column", alignItems: "stretch", gap: 6 }}>
      {error && <div className="error-text">{error}</div>}
      <div style={{ fontSize: 12, color: "var(--text-dim)" }}>
        {backend.name} ({KNOWN_PROVIDERS.find((p) => p.value === backend.provider)?.label ?? backend.provider})
      </div>
      {customModel ? (
        <input placeholder={t("api.modelIdPlaceholder")} value={modelId} onChange={(e) => setModelId(e.target.value)} />
      ) : (
        <select
          value={modelId}
          onChange={(e) => {
            if (e.target.value === CUSTOM_MODEL_OPTION) {
              setCustomModel(true);
              setModelId("");
            } else {
              setModelId(e.target.value);
            }
          }}
        >
          {KNOWN_GEMINI_MODELS.map((m) => (
            <option key={m.value} value={m.value}>
              {m.label}
            </option>
          ))}
          <option value={CUSTOM_MODEL_OPTION}>{t("api.otherModel")}</option>
        </select>
      )}
      {existingTextFields.length > 0 && (
        <select value={promptMode} onChange={(e) => setPromptMode(e.target.value as "master" | "match")}>
          <option value="master">{t("api.promptModeMaster")}</option>
          <option value="match">{t("api.promptModeMatch")}</option>
        </select>
      )}
      {promptMode === "match" && existingTextFields.length > 0 ? (
        <select value={matchedField} onChange={(e) => setMatchedField(e.target.value)}>
          {existingTextFields.map((f) => (
            <option key={f.name} value={f.name}>
              {f.label ?? f.name}
            </option>
          ))}
        </select>
      ) : (
        <textarea
          placeholder={t("api.masterPromptPlaceholder")}
          rows={3}
          value={masterPrompt}
          onChange={(e) => setMasterPrompt(e.target.value)}
        />
      )}
      <div className="node-actions">
        <button
          className="primary"
          disabled={saving || (promptMode === "master" ? !masterPrompt.trim() : !matchedField)}
          onClick={save}
        >
          {saving ? t("common.saving") : t("api.saveInstance")}
        </button>
        <button onClick={onCancel}>{t("common.cancel")}</button>
      </div>
    </div>
  );
}

function NodeTypeCard({
  template,
  backends,
  capabilities,
  wizardOpen,
  onOpenWizard,
  onCancelWizard,
  onSaved,
  reloadCapabilities,
  reloadTemplates,
}: {
  template: NodeTemplate;
  backends: Backend[];
  capabilities: Capability[];
  wizardOpen: boolean;
  onOpenWizard: () => void;
  onCancelWizard: () => void;
  onSaved: () => void;
  reloadCapabilities: () => void;
  reloadTemplates: () => void;
}) {
  const t = useT();
  const backendName = (id: string) => backends.find((b) => b.id === id)?.name ?? id;
  const [promptsFor, setPromptsFor] = useState<Capability | null>(null);
  // Single "+ Add instance" entry point: pick any not-yet-attached backend
  // first, then branch by its kind -- comfyui hands off to the existing
  // NodeTypeWizard (workflow upload + field mapping), api_provider to the
  // lighter AddApiInstanceForm (model + prompt). Two buttons asking the user
  // to already know which kind they want up front was the wrong shape --
  // the backend they pick already says that.
  const [pickerOpen, setPickerOpen] = useState(false);
  const [apiBackendId, setApiBackendId] = useState<string | null>(null);
  const [comfyBackendId, setComfyBackendId] = useState<string | null>(null);
  const [copySourceId, setCopySourceId] = useState("");
  const attachedBackendIds = capabilities.map((c) => c.backend_id);
  const pickableBackends = backends.filter((b) => !attachedBackendIds.includes(b.id));
  // Existing ComfyUI instances whose workflow this new one can be copied from,
  // instead of re-uploading a (usually identical) workflow.json.
  const copyableSources = capabilities.filter(
    (c) => c.execution_type === "comfyui_workflow" && Boolean((c.config as Record<string, unknown>).workflow_json),
  );

  const closeAll = () => {
    setPickerOpen(false);
    setApiBackendId(null);
    setComfyBackendId(null);
    setCopySourceId("");
  };
  const pickBackend = (backendId: string) => {
    const backend = backends.find((b) => b.id === backendId);
    if (!backend) return;
    setPickerOpen(false);
    if (backend.kind === "api_provider") {
      setApiBackendId(backendId);
    } else {
      setComfyBackendId(backendId);
      // No existing workflow to copy -> straight to the upload wizard, as
      // before. Otherwise let the choice panel below offer upload-vs-copy.
      if (copyableSources.length === 0) onOpenWizard();
    }
  };
  // Create a new ComfyUI instance for the picked backend by cloning an existing
  // instance's workflow_json + param_mapping (dropping any prompt link, so the
  // clone starts independent). One click, no wizard.
  const doCopy = async (sourceId: string) => {
    const source = capabilities.find((c) => c.id === sourceId);
    if (!source || !comfyBackendId) return;
    const { prompt_leader_id: _drop, ...cfg } = source.config as Record<string, unknown>;
    await capabilitiesApi.create({
      backend_id: comfyBackendId,
      node_type_slug: template.node_type_slug,
      execution_type: "comfyui_workflow",
      enabled: true,
      config: cfg,
    });
    closeAll();
    reloadCapabilities();
    reloadTemplates();
  };
  const anyFormOpen = pickerOpen || apiBackendId !== null || comfyBackendId !== null || wizardOpen;

  return (
    <div className="settings-section node-type-card">
      <div className="node-cell-header">
        <span>{template.name}</span>
        <span className="status-pill">{template.node_type_slug}</span>
      </div>
      <div style={{ fontSize: 11, color: "var(--text-dim)" }}>
        {(template.param_schema.fields ?? []).map((f) => `${f.name}: ${f.type}`).join(", ") || t("nodetype.noFields")}
      </div>

      <NodeTypeDescription template={template} onChanged={reloadTemplates} />

      <table>
        <thead>
          <tr>
            <th>{t("nodetype.colBackend")}</th>
            <th>{t("nodetype.colExecution")}</th>
            <th>{t("nodetype.colEnabled")}</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {capabilities.map((c) => (
            <tr key={c.id}>
              <td>{backendName(c.backend_id)}</td>
              <td>{c.execution_type}</td>
              <td>
                <input
                  type="checkbox"
                  checked={c.enabled}
                  onChange={async (e) => {
                    await capabilitiesApi.update(c.id, { enabled: e.target.checked });
                    reloadCapabilities();
                  }}
                />
              </td>
              <td>
                {/* Baked-in workflow_json text (prompts) is a comfyui_workflow-only
                    concept -- api_call/native capabilities have no workflow graph
                    for find_editable_text_fields to walk. */}
                {c.execution_type === "comfyui_workflow" && (
                  <>
                    <button
                      onClick={() => setPromptsFor(c)}
                      title={c.config.prompt_leader_id ? t("nodetype.promptsViewTitle") : t("nodetype.promptsEditTitle")}
                    >
                      {c.config.prompt_leader_id ? t("nodetype.promptsLinked") : t("nodetype.prompts")}
                    </button>
                    <PromptLinkControl capability={c} siblings={capabilities} backendName={backendName} reload={reloadCapabilities} />
                  </>
                )}
                <button onClick={() => tryDelete(() => capabilitiesApi.remove(c.id), reloadCapabilities)}>{t("common.deleteLower")}</button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      {promptsFor &&
        (() => {
          const leaderId = promptsFor.config.prompt_leader_id as string | undefined;
          const leader = leaderId ? capabilities.find((c) => c.id === leaderId) : undefined;
          return (
            <CapabilityTextFieldsModal
              capability={promptsFor}
              onClose={() => setPromptsFor(null)}
              readOnly={Boolean(leaderId)}
              leaderName={leader ? backendName(leader.backend_id) : undefined}
            />
          );
        })()}

      {pickerOpen && (
        <div className="inline-form">
          <select defaultValue="" onChange={(e) => e.target.value && pickBackend(e.target.value)}>
            <option value="" disabled>
              {t("nodetype.whichBackend")}
            </option>
            {pickableBackends.map((b) => (
              <option key={b.id} value={b.id}>
                {b.name} ({b.kind === "api_provider" ? KNOWN_PROVIDERS.find((p) => p.value === b.provider)?.label ?? b.provider : "comfyui"})
              </option>
            ))}
          </select>
          <button onClick={closeAll}>{t("common.cancel")}</button>
          {pickableBackends.length === 0 && (
            <span style={{ fontSize: 12, color: "var(--text-dim)" }}>{t("nodetype.noUnattached")}</span>
          )}
        </div>
      )}

      {/* ComfyUI backend picked and there's an existing workflow to clone:
          offer copy-vs-upload instead of forcing a re-upload of the (usually
          identical) workflow.json. */}
      {comfyBackendId && !wizardOpen && copyableSources.length > 0 && (
        <div className="inline-form">
          <span style={{ fontSize: 12 }}>{t("nodetype.configure", { name: backendName(comfyBackendId) })}</span>
          <button className="primary" onClick={() => onOpenWizard()}>
            {t("nodetype.uploadWorkflow")}
          </button>
          <span style={{ fontSize: 12, color: "var(--text-dim)" }}>{t("nodetype.orCopyFrom")}</span>
          <select value={copySourceId} onChange={(e) => setCopySourceId(e.target.value)}>
            <option value="">{t("nodetype.chooseInstance")}</option>
            {copyableSources.map((c) => (
              <option key={c.id} value={c.id}>
                {backendName(c.backend_id)}
              </option>
            ))}
          </select>
          <button disabled={!copySourceId} onClick={() => doCopy(copySourceId)}>
            {t("common.copy")}
          </button>
          <button onClick={closeAll}>{t("common.cancel")}</button>
        </div>
      )}

      {apiBackendId &&
        (() => {
          const backend = backends.find((b) => b.id === apiBackendId);
          return backend ? (
            <AddApiInstanceForm
              template={template}
              backend={backend}
              onCancel={closeAll}
              onSaved={() => {
                closeAll();
                reloadCapabilities();
                reloadTemplates();
              }}
            />
          ) : null;
        })()}

      {wizardOpen && comfyBackendId && (
        <NodeTypeWizard
          backends={backends}
          mode={{
            kind: "add-instance",
            template,
            // Narrows NodeTypeWizard's own backend picker down to exactly the
            // one already chosen in the unified picker above -- everything
            // else (including already-attached backends) stays excluded.
            excludeBackendIds: backends.map((b) => b.id).filter((id) => id !== comfyBackendId),
          }}
          onCancel={() => {
            onCancelWizard();
            closeAll();
          }}
          onSaved={() => {
            onSaved();
            closeAll();
          }}
        />
      )}

      {!anyFormOpen && (
        <div className="node-actions">
          <button className="primary" onClick={() => setPickerOpen(true)}>
            {t("nodetype.addInstance")}
          </button>
          <button
            onClick={() => tryDelete(() => nodeTemplatesApi.remove(template.id), reloadTemplates)}
            disabled={capabilities.length > 0}
            title={capabilities.length > 0 ? t("nodetype.deleteBlocked") : t("nodetype.deleteTitle")}
          >
            {t("nodetype.delete")}
          </button>
        </div>
      )}
    </div>
  );
}

function NodeTypesSection({
  templates,
  backends,
  capabilities,
  reloadTemplates,
  reloadCapabilities,
}: {
  templates: NodeTemplate[];
  backends: Backend[];
  capabilities: Capability[];
  reloadTemplates: () => void;
  reloadCapabilities: () => void;
}) {
  const t = useT();
  const [wizard, setWizard] = useState<{ kind: "create" } | { kind: "add-instance"; templateId: string } | null>(null);

  const closeWizard = () => setWizard(null);
  const saved = () => {
    reloadTemplates();
    reloadCapabilities();
    setWizard(null);
  };

  // GET /api/node-templates merges real DB rows with a synthesized entry
  // per code-registry native type (node_templates.py's _native_template_read
  // -- see memory/native_execution_type.md), so this list isn't DB rows
  // only. A native.* entry has no Capability concept (it runs in-process,
  // never via a ComfyUI backend) and no DB row to delete -- "+ Add ComfyUI
  // instance" is meaningless for one, and "delete node type" 404s instead
  // of doing anything (db.get on its synthesized uuid5 id finds nothing,
  // and it just reappears next reload anyway, regenerated straight from the
  // registry). This tab is for the DB-backed template types those two
  // actions actually apply to.
  const editableTemplates = templates.filter((t) => !t.node_type.startsWith("native."));

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      <h2 style={{ margin: 0 }}>{t("nodetype.sectionTitle")}</h2>
      {editableTemplates.map((t) => (
        <NodeTypeCard
          key={t.id}
          template={t}
          backends={backends}
          capabilities={capabilities.filter((c) => c.node_type_slug === t.node_type_slug)}
          wizardOpen={wizard?.kind === "add-instance" && wizard.templateId === t.id}
          onOpenWizard={() => setWizard({ kind: "add-instance", templateId: t.id })}
          onCancelWizard={closeWizard}
          onSaved={saved}
          reloadCapabilities={reloadCapabilities}
          reloadTemplates={reloadTemplates}
        />
      ))}

      {wizard?.kind === "create" ? (
        <NodeTypeWizard backends={backends} mode={{ kind: "create" }} onCancel={closeWizard} onSaved={saved} />
      ) : (
        <button className="primary" style={{ alignSelf: "flex-start" }} onClick={() => setWizard({ kind: "create" })}>
          {t("nodetype.new")}
        </button>
      )}
    </div>
  );
}

export function Settings() {
  const [backends, setBackends] = useState<Backend[]>([]);
  const [capabilities, setCapabilities] = useState<Capability[]>([]);
  const [templates, setTemplates] = useState<NodeTemplate[]>([]);
  // Without a .catch, a failed load (bad token, unreachable backend, ...)
  // just leaves these lists empty with zero indication anything went wrong
  // -- indistinguishable from "nothing configured yet".
  const [loadError, setLoadError] = useState<string | null>(null);

  const reloadBackends = makeReloader(backendsApi.list, setBackends, setLoadError);
  const reloadCapabilities = makeReloader(capabilitiesApi.list, setCapabilities, setLoadError);
  const reloadTemplates = makeReloader(nodeTemplatesApi.list, setTemplates, setLoadError);

  useEffect(() => {
    reloadBackends();
    reloadCapabilities();
    reloadTemplates();
  }, []);

  return (
    <div className="settings-panel">
      {loadError && <div className="error-text">{loadError}</div>}
      <LanguageSection />
      <StorageSection />
      <BackendsSection items={backends} reload={reloadBackends} />
      <NodeTypesSection
        templates={templates}
        backends={backends}
        capabilities={capabilities}
        reloadTemplates={reloadTemplates}
        reloadCapabilities={reloadCapabilities}
      />
    </div>
  );
}
