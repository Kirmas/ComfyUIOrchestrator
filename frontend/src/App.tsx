import { useEffect, useState } from "react";
import { getApiToken } from "./api/client";
import { projectsApi } from "./api/endpoints";
import { ConnectionBar } from "./components/ConnectionBar";
import { Grid } from "./components/Grid";
import { Logs } from "./components/Logs";
import { ProjectPicker } from "./components/ProjectPicker";
import { Settings } from "./components/Settings";

type View = "grid" | "settings" | "logs";
type AuthStatus = "checking" | "unauthenticated" | "authenticated";

const LAST_PROJECT_KEY = "comfy-orchestrator:lastProjectId";

export default function App() {
  const [projectId, setProjectId] = useState<string | null>(() => localStorage.getItem(LAST_PROJECT_KEY));
  const [view, setView] = useState<View>("grid");
  // Gates the whole app behind a working connection: rendering the topbar +
  // Grid/Settings speculatively and letting each of their own API calls
  // fail individually (silently or not) is how a missing/wrong token used
  // to look like "connected, but everything's empty" instead of "not
  // connected". A saved token is only a claim until something authenticated
  // actually succeeds against it.
  const [authStatus, setAuthStatus] = useState<AuthStatus>(getApiToken() ? "checking" : "unauthenticated");

  useEffect(() => {
    if (!getApiToken()) return;
    projectsApi
      .list()
      .then(() => setAuthStatus("authenticated"))
      .catch(() => setAuthStatus("unauthenticated"));
  }, []);

  const selectProject = (id: string) => {
    setProjectId(id || null);
    if (id) localStorage.setItem(LAST_PROJECT_KEY, id);
    else localStorage.removeItem(LAST_PROJECT_KEY);
  };

  if (authStatus !== "authenticated") {
    return (
      <div className="app-shell">
        <div className="main-area" style={{ display: "flex", alignItems: "center", justifyContent: "center" }}>
          <div style={{ display: "flex", flexDirection: "column", gap: 14, alignItems: "center" }}>
            <h1 style={{ margin: 0 }}>ComfyUI Orchestrator</h1>
            {authStatus === "checking" ? <span style={{ color: "var(--text-dim)" }}>Connecting…</span> : <ConnectionBar forceOpen />}
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="app-shell">
      <div className="topbar">
        <h1>ComfyUI Orchestrator</h1>
        <ProjectPicker projectId={projectId} onSelect={selectProject} />
        <div className="topbar-spacer" />
        {view !== "grid" && <button onClick={() => setView("grid")}>Back to grid</button>}
        <button onClick={() => setView(view === "logs" ? "grid" : "logs")} className={view === "logs" ? "active" : ""}>
          Logs
        </button>
        <button onClick={() => setView(view === "settings" ? "grid" : "settings")} className={view === "settings" ? "active" : ""}>
          Settings
        </button>
        <ConnectionBar />
      </div>
      {view === "settings" ? (
        <div className="main-area">
          <Settings />
        </div>
      ) : view === "logs" ? (
        <div className="main-area">
          <Logs />
        </div>
      ) : projectId ? (
        <Grid projectId={projectId} />
      ) : (
        <div className="main-area" style={{ padding: 24, color: "var(--text-dim)" }}>
          Select or create a project to get started.
        </div>
      )}
    </div>
  );
}
