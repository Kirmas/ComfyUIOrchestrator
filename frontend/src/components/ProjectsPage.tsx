import { useEffect, useMemo, useState } from "react";
import { createPortal } from "react-dom";
import { resolveAssetUrl } from "../api/client";
import { projectCategoriesApi, projectsApi } from "../api/endpoints";
import { useT } from "../i18n";
import type { Project, ProjectCategory, ProjectImage } from "../types";
import { cx } from "../utils";

const FOLDER_KEY = "comfy-orchestrator:projectsFolder";

type Target = { kind: "project"; item: Project } | { kind: "folder"; item: ProjectCategory };

/** The projects page: folders (they nest -- "Babylon" > "Characters") and a
 * card per project. Replaced the topbar dropdown once projects stopped being a
 * short flat list. Folders are organisation only; nothing in a grid reads them. */
export function ProjectsPage({
  projectId,
  onOpen,
  onProjectsLoaded,
}: {
  projectId: string | null;
  onOpen: (id: string) => void;
  onProjectsLoaded: (projects: Project[]) => void;
}) {
  const t = useT();
  const [projects, setProjects] = useState<Project[]>([]);
  const [folders, setFolders] = useState<ProjectCategory[]>([]);
  const [folderId, setFolderId] = useState<string | null>(() => localStorage.getItem(FOLDER_KEY));
  const [loadError, setLoadError] = useState<string | null>(null);
  const [actionsFor, setActionsFor] = useState<Target | null>(null);
  const [previewFor, setPreviewFor] = useState<Project | null>(null);

  const reload = () =>
    Promise.all([projectsApi.list(), projectCategoriesApi.list()])
      .then(([p, f]) => {
        setLoadError(null);
        setProjects(p);
        setFolders(f);
        onProjectsLoaded(p);
      })
      .catch((err) => setLoadError(err instanceof Error ? err.message : t("project.loadFailed")));

  useEffect(() => {
    reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const enter = (id: string | null) => {
    setFolderId(id);
    if (id) localStorage.setItem(FOLDER_KEY, id);
    else localStorage.removeItem(FOLDER_KEY);
  };

  const byId = useMemo(() => new Map(folders.map((f) => [f.id, f])), [folders]);
  // A remembered folder can have been deleted since (here or by an agent).
  const current = folderId ? byId.get(folderId) ?? null : null;
  const currentId = current?.id ?? null;

  const breadcrumb: ProjectCategory[] = [];
  for (let f = current; f; f = f.parent_id ? byId.get(f.parent_id) ?? null : null) breadcrumb.unshift(f);

  /** A folder and every folder below it -- for counts, the collage, and to
   * keep a folder from being moved into its own subtree. */
  const subtree = (id: string): Set<string> => {
    const out = new Set([id]);
    let grew = true;
    while (grew) {
      grew = false;
      for (const f of folders) {
        if (f.parent_id && out.has(f.parent_id) && !out.has(f.id)) {
          out.add(f.id);
          grew = true;
        }
      }
    }
    return out;
  };

  const subfolders = folders.filter((f) => f.parent_id === currentId).sort((a, b) => a.name.localeCompare(b.name));
  const here = projects.filter((p) => p.category_id === currentId).sort((a, b) => a.name.localeCompare(b.name));

  const createProject = async () => {
    const name = prompt(t("projects.newProjectPrompt"))?.trim();
    if (!name) return;
    const project = await projectsApi.create(name, currentId);
    await reload();
    onOpen(project.id);
  };

  const createFolder = async () => {
    const name = prompt(t("projects.newFolderPrompt"))?.trim();
    if (!name) return;
    await projectCategoriesApi.create(name, currentId);
    await reload();
  };

  const run = async (action: () => Promise<unknown>) => {
    try {
      await action();
    } catch (err) {
      alert(err instanceof Error ? err.message : String(err));
    }
    setActionsFor(null);
    await reload();
  };

  const rename = (target: Target) => {
    const name = prompt(t("projects.renamePrompt"), target.item.name)?.trim();
    if (!name || name === target.item.name) return;
    run(() =>
      target.kind === "project"
        ? projectsApi.update(target.item.id, { name })
        : projectCategoriesApi.update(target.item.id, { name }),
    );
  };

  const move = (target: Target, to: string) => {
    const dest = to || null;
    run(() =>
      target.kind === "project"
        ? projectsApi.update(target.item.id, { category_id: dest })
        : projectCategoriesApi.update(target.item.id, { parent_id: dest }),
    );
  };

  const remove = (target: Target) => {
    if (target.kind === "project") {
      if (!confirm(t("project.confirmDelete", { name: target.item.name }))) return;
      run(() => projectsApi.remove(target.item.id));
    } else {
      if (!confirm(t("projects.confirmDeleteFolder", { name: target.item.name }))) return;
      run(() => projectCategoriesApi.remove(target.item.id));
    }
  };

  /** Every folder as a "Move to" option, indented by depth; a folder can't go
   * into itself or below itself. */
  const moveOptions = (target: Target) => {
    const excluded = target.kind === "folder" ? subtree(target.item.id) : new Set<string>();
    const out: { id: string; label: string }[] = [];
    const walk = (parent: string | null, depth: number) => {
      for (const f of folders.filter((x) => x.parent_id === parent).sort((a, b) => a.name.localeCompare(b.name))) {
        if (excluded.has(f.id)) continue;
        out.push({ id: f.id, label: `${"  ".repeat(depth)}📁 ${f.name}` });
        walk(f.id, depth + 1);
      }
    };
    walk(null, 1);
    return out;
  };

  const targetParent = (target: Target) =>
    target.kind === "project" ? target.item.category_id : target.item.parent_id;

  return (
    <div className="main-area projects-page">
      <div className="projects-toolbar">
        <div className="projects-breadcrumb">
          <button className={cx("projects-crumb", !current && "active")} onClick={() => enter(null)}>
            {t("projects.root")}
          </button>
          {breadcrumb.map((f) => (
            <span key={f.id}>
              <span className="projects-sep">/</span>
              <button className={cx("projects-crumb", f.id === currentId && "active")} onClick={() => enter(f.id)}>
                {f.name}
              </button>
            </span>
          ))}
        </div>
        <div className="topbar-spacer" />
        <button onClick={createFolder}>{t("projects.newFolder")}</button>
        <button onClick={createProject}>{t("projects.newProject")}</button>
      </div>
      {loadError && <div className="error-text">{loadError}</div>}

      <div className="projects-tiles">
        {subfolders.map((f) => {
          const inside = subtree(f.id);
          const contained = projects.filter((p) => p.category_id && inside.has(p.category_id));
          const collage = contained.filter((p) => p.preview_url).slice(0, 4);
          return (
            <div key={f.id} className="project-tile folder" onClick={() => enter(f.id)}>
              <div className={cx("project-tile-image", "collage", `n${collage.length}`)}>
                {collage.length === 0 ? (
                  <span className="project-tile-placeholder">📁</span>
                ) : (
                  collage.map((p) => <img key={p.id} src={resolveAssetUrl(p.preview_url)} loading="lazy" alt="" />)
                )}
              </div>
              <div className="project-tile-footer">
                <span className="project-tile-name">📁 {f.name}</span>
                <span className="project-tile-count">{contained.length}</span>
                <button
                  className="project-tile-more"
                  onClick={(e) => {
                    e.stopPropagation();
                    setActionsFor({ kind: "folder", item: f });
                  }}
                  title={t("projects.actions")}
                >
                  ⋯
                </button>
              </div>
            </div>
          );
        })}
        {here.map((p) => (
          <div key={p.id} className={cx("project-tile", p.id === projectId && "current")} onClick={() => onOpen(p.id)}>
            <div className="project-tile-image">
              {p.preview_url ? (
                <img src={resolveAssetUrl(p.preview_url)} loading="lazy" alt="" />
              ) : (
                <span className="project-tile-placeholder">🖼</span>
              )}
            </div>
            <div className="project-tile-footer">
              <span className="project-tile-name">{p.name}</span>
              <button
                className="project-tile-more"
                onClick={(e) => {
                  e.stopPropagation();
                  setActionsFor({ kind: "project", item: p });
                }}
                title={t("projects.actions")}
              >
                ⋯
              </button>
            </div>
          </div>
        ))}
      </div>
      {subfolders.length === 0 && here.length === 0 && !loadError && (
        <div className="projects-empty">{t("projects.empty")}</div>
      )}

      {actionsFor &&
        createPortal(
          <div className="image-modal-backdrop" onClick={() => setActionsFor(null)}>
            <div className="projects-sheet" onClick={(e) => e.stopPropagation()}>
              <div className="projects-sheet-title">
                {actionsFor.kind === "folder" ? "📁 " : ""}
                {actionsFor.item.name}
              </div>
              <button onClick={() => rename(actionsFor)}>{t("projects.rename")}</button>
              {actionsFor.kind === "project" && (
                <button
                  onClick={() => {
                    setPreviewFor(actionsFor.item);
                    setActionsFor(null);
                  }}
                >
                  {t("projects.choosePreview")}
                </button>
              )}
              <label className="projects-sheet-move">
                {t("projects.moveTo")}
                <select value={targetParent(actionsFor) ?? ""} onChange={(e) => move(actionsFor, e.target.value)}>
                  <option value="">{t("projects.root")}</option>
                  {moveOptions(actionsFor).map((o) => (
                    <option key={o.id} value={o.id}>
                      {o.label}
                    </option>
                  ))}
                </select>
              </label>
              <button className="projects-sheet-danger" onClick={() => remove(actionsFor)}>
                {actionsFor.kind === "project" ? t("project.delete") : t("projects.deleteFolder")}
              </button>
              <button onClick={() => setActionsFor(null)}>{t("projects.close")}</button>
            </div>
          </div>,
          document.body,
        )}

      {previewFor && (
        <PreviewPicker
          project={previewFor}
          onClose={() => setPreviewFor(null)}
          onPick={(assetId) =>
            projectsApi
              .update(previewFor.id, { preview_asset_id: assetId })
              .then(() => {
                setPreviewFor(null);
                return reload();
              })
              .catch((err) => alert(err instanceof Error ? err.message : String(err)))
          }
        />
      )}
    </div>
  );
}

/** Every image in the project (all its dashboards plus the board library),
 * newest first, with "random" as the first choice. */
function PreviewPicker({
  project,
  onClose,
  onPick,
}: {
  project: Project;
  onClose: () => void;
  onPick: (assetId: string | null) => void;
}) {
  const t = useT();
  const [images, setImages] = useState<ProjectImage[] | null>(null);

  useEffect(() => {
    projectsApi
      .images(project.id)
      .then(setImages)
      .catch(() => setImages([]));
  }, [project.id]);

  return createPortal(
    <div className="image-modal-backdrop" onClick={onClose}>
      <div className="params-modal-content" onClick={(e) => e.stopPropagation()}>
        <div className="projects-toolbar">
          <strong>{t("projects.previewTitle", { name: project.name })}</strong>
          <div className="topbar-spacer" />
          <button onClick={onClose}>✕</button>
        </div>
        <div className="projects-picker-grid">
          <button
            className={cx("projects-picker-item", "random", project.preview_asset_id === null && "current")}
            onClick={() => onPick(null)}
          >
            🎲 {t("projects.randomPreview")}
          </button>
          {images?.map((img) => (
            <button
              key={img.id}
              className={cx("projects-picker-item", img.id === project.preview_asset_id && "current")}
              onClick={() => onPick(img.id)}
            >
              <img src={resolveAssetUrl(img.preview_url)} loading="lazy" alt="" />
            </button>
          ))}
        </div>
        {images === null && <div className="node-cell-hint">{t("projects.loading")}</div>}
        {images?.length === 0 && <div className="node-cell-hint">{t("projects.noImages")}</div>}
      </div>
    </div>,
    document.body,
  );
}
