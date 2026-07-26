import { useEffect, useState } from "react";
import { getApiToken } from "./api/client";
import { projectsApi } from "./api/endpoints";
import { ConnectionBar } from "./components/ConnectionBar";
import { Grid } from "./components/Grid";
import { Logs } from "./components/Logs";
import { ProjectPicker } from "./components/ProjectPicker";
import { Settings } from "./components/Settings";
import { cx } from "./utils";

type View = "grid" | "settings" | "logs";
type AuthStatus = "checking" | "unauthenticated" | "authenticated";

const LAST_PROJECT_KEY = "comfy-orchestrator:lastProjectId";

export default function App() {
  const [projectId, setProjectId] = useState<string | null>(() => localStorage.getItem(LAST_PROJECT_KEY));
  const [view, setView] = useState<View>("grid");
  // On phones the whole topbar (project picker + nav + connection) collapses
  // behind a hamburger -- it's rarely needed mid-session and eats scarce
  // screen width. No effect on desktop, where CSS keeps .topbar-menu always
  // visible and hides the burger.
  const [menuOpen, setMenuOpen] = useState(false);
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
    setMenuOpen(false);
  };

  const goTo = (next: View) => {
    setView(next);
    setMenuOpen(false);
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
        <button
          className="topbar-burger"
          onClick={() => setMenuOpen((o) => !o)}
          aria-label="Menu"
          aria-expanded={menuOpen}
        >
          {menuOpen ? "✕" : "☰"}
        </button>
        <div className={cx("topbar-menu", menuOpen && "open")}>
          <ProjectPicker projectId={projectId} onSelect={selectProject} />
          <div className="topbar-spacer" />
          {view !== "grid" && <button onClick={() => goTo("grid")}>Back to grid</button>}
          <button onClick={() => goTo(view === "logs" ? "grid" : "logs")} className={view === "logs" ? "active" : ""}>
            Logs
          </button>
          <button onClick={() => goTo(view === "settings" ? "grid" : "settings")} className={view === "settings" ? "active" : ""}>
            Settings
          </button>
          <ConnectionBar />
        </div>
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
