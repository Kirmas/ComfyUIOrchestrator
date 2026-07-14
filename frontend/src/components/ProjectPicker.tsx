import { useEffect, useState } from "react";
import { projectsApi } from "../api/endpoints";
import type { Project } from "../types";

export function ProjectPicker({
  projectId,
  onSelect,
}: {
  projectId: string | null;
  onSelect: (id: string) => void;
}) {
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
      .catch((err) => setLoadError(err instanceof Error ? err.message : "Failed to load projects."));

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
    if (!confirm(`Delete project "${project?.name ?? projectId}" and everything in it? This can't be undone.`)) return;
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
          Select project…
        </option>
        {projects.map((p) => (
          <option key={p.id} value={p.id}>
            {p.name}
          </option>
        ))}
      </select>
      {projectId && (
        <button onClick={deleteProject} title="Delete this project">
          Delete project
        </button>
      )}
      <input placeholder="New project name" value={newName} onChange={(e) => setNewName(e.target.value)} style={{ width: 140 }} />
      <button onClick={createProject}>+ New</button>
    </div>
  );
}
