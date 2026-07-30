import { useEffect, useState } from "react";
import { projectsApi } from "../api/endpoints";
import { useT } from "../i18n";
import type { Project } from "../types";

export function ProjectPicker({
  projectId,
  onSelect,
}: {
  projectId: string | null;
  onSelect: (id: string) => void;
}) {
  const t = useT();
  const [projects, setProjects] = useState<Project[]>([]);
  const [newName, setNewName] = useState("");
  const [loadError, setLoadError] = useState<string | null>(null);

  const reload = () =>
    projectsApi
      .list()
      .then((loaded) => {
        setLoadError(null);
        setProjects(loaded);
        // The selected project id can come back from localStorage (see
        // App.tsx) after a reload -- if it was deleted in the meantime
        // (this browser or another), fall back to the picker instead of
        // leaving Grid pointed at a project that 404s.
        if (projectId && !loaded.some((p) => p.id === projectId)) onSelect("");
      })
      // An empty dropdown from a swallowed error looks identical to "you
      // have no projects yet" -- surface it instead (see ConnectionBar for
      // the same problem on the token-entry path).
      .catch((err) => setLoadError(err instanceof Error ? err.message : t("project.loadFailed")));

  useEffect(() => {
    reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const createProject = async () => {
    if (!newName.trim()) return;
    const project = await projectsApi.create(newName.trim());
    setNewName("");
    await reload();
    onSelect(project.id);
  };

  const deleteProject = async () => {
    if (!projectId) return;
    const project = projects.find((p) => p.id === projectId);
    if (!confirm(t("project.confirmDelete", { name: project?.name ?? projectId }))) return;
    await projectsApi.remove(projectId);
    await reload();
    onSelect("");
  };

  return (
    <div className="inline-form" style={{ margin: 0 }}>
      {loadError && (
        <span className="error-text" title={loadError}>
          {loadError}
        </span>
      )}
      <select value={projectId ?? ""} onChange={(e) => onSelect(e.target.value)}>
        <option value="" disabled>
          {t("project.select")}
        </option>
        {projects.map((p) => (
          <option key={p.id} value={p.id}>
            {p.name}
          </option>
        ))}
      </select>
      {projectId && (
        <button onClick={deleteProject} title={t("project.deleteTitle")}>
          {t("project.delete")}
        </button>
      )}
      <input placeholder={t("project.newPlaceholder")} value={newName} onChange={(e) => setNewName(e.target.value)} style={{ width: 140 }} />
      <button onClick={createProject}>{t("project.new")}</button>
    </div>
  );
}
