import { useEffect, useState } from "react";
import { getApiToken } from "./api/client";
import { projectsApi } from "./api/endpoints";
import { Board } from "./components/Board";
import { ConnectionBar } from "./components/ConnectionBar";
import { Grid } from "./components/Grid";
import { Logs } from "./components/Logs";
import { ProjectPicker } from "./components/ProjectPicker";
import { Settings } from "./components/Settings";
import { cx } from "./utils";

type View = "grid" | "board" | "settings" | "logs";
type AuthStatus = "checking" | "unauthenticated" | "authenticated";

const LAST_PROJECT_KEY = "comfy-orchestrator:lastProjectId";
const LAST_VIEW_KEY = "comfy-orchestrator:lastView";

const VIEWS: View[] = ["grid", "board", "settings", "logs"];

/** Reloading should put you back where you were, the same way the project
 * picker already remembers its selection -- landing back on the grid after
 * every F5 means losing your place on the board or halfway down settings.
 * Validated rather than trusted: a stored value can outlive the view it names
 * (a renamed or removed tab), and an unrecognised one must not leave the app
 * rendering nothing. */
const storedView = (): View => {
  const saved = localStorage.getItem(LAST_VIEW_KEY) as View | null;
  return saved && VIEWS.includes(saved) ? saved : "grid";
};

export default function App() {
  const [projectId, setProjectId] = useState<string | null>(() => localStorage.getItem(LAST_PROJECT_KEY));
  const [view, setView] = useState<View>(storedView);
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
    localStorage.setItem(LAST_VIEW_KEY, next);
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
          {/* Pre-production lives here: idea, references, divergence. The grid
              is convergent by construction and can't hold any of it -- see
              roadmap.md §1. */}
          <button onClick={() => goTo(view === "board" ? "grid" : "board")} className={view === "board" ? "active" : ""}>
            Board
          </button>
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
      ) : view === "board" ? (
        projectId ? (
          <Board projectId={projectId} />
        ) : (
          <div className="main-area" style={{ padding: 24, color: "var(--text-dim)" }}>
            Select a project to open its idea board.
          </div>
        )
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
