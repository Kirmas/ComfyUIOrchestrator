import { useEffect, useState } from "react";
import { AgentChat } from "./components/AgentChat";
import { ApiError, getApiToken } from "./api/client";
import { designDocApi, projectsApi } from "./api/endpoints";
import type { DesignDocSummary, Project } from "./types";
import { Board } from "./components/Board";
import { ConnectionBar } from "./components/ConnectionBar";
import { DesignDoc, type DocLanding, type DocLang } from "./components/DesignDoc";
import { Grid } from "./components/Grid";
import { Logs } from "./components/Logs";
import { ProjectsPage } from "./components/ProjectsPage";
import { Settings } from "./components/Settings";
import { useT } from "./i18n";
import { cx } from "./utils";

// "gdoc": a global design doc, opened from its folder on the projects page.
type View = "projects" | "grid" | "board" | "doc" | "gdoc" | "agent" | "settings" | "logs";
type AuthStatus = "checking" | "unauthenticated" | "authenticated";
type DocPlace = { view: "doc"; projectId: string } | { view: "gdoc"; docId: string };

const LAST_PROJECT_KEY = "comfy-orchestrator:lastProjectId";
const LAST_VIEW_KEY = "comfy-orchestrator:lastView";
const LAST_GLOBAL_DOC_KEY = "comfy-orchestrator:lastGlobalDoc";

const VIEWS: View[] = ["projects", "grid", "board", "doc", "gdoc", "agent", "settings", "logs"];

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
  const t = useT();
  const [projectId, setProjectId] = useState<string | null>(() => localStorage.getItem(LAST_PROJECT_KEY));
  const [view, setView] = useState<View>(storedView);
  const [globalDocId, setGlobalDocId] = useState<string | null>(() => localStorage.getItem(LAST_GLOBAL_DOC_KEY));
  const [globalDoc, setGlobalDoc] = useState<DesignDocSummary | null>(null);
  // Following a doc-to-doc link: where it asked to land (chapter + the
  // language it was clicked in), and the docs visited on the way, for "back".
  const [docLanding, setDocLanding] = useState<DocLanding | null>(null);
  const [docHistory, setDocHistory] = useState<DocPlace[]>([]);
  // Shown on the topbar button that opens the projects page.
  const [projectName, setProjectName] = useState<string | null>(null);
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

  // The selected id can come back from localStorage after a reload -- if the
  // project was deleted in the meantime (this browser or another), drop it
  // instead of leaving Grid pointed at a project that 404s.
  useEffect(() => {
    if (authStatus !== "authenticated" || !projectId) {
      setProjectName(null);
      return;
    }
    projectsApi
      .get(projectId)
      .then((p) => setProjectName(p.name))
      .catch((err) => {
        if (err instanceof ApiError && err.status === 404) selectProject("");
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [authStatus, projectId]);

  // Re-read on open: the title may have changed, or the doc may have been
  // attached to a project since (then it's no longer global, back to the list).
  useEffect(() => {
    if (authStatus !== "authenticated" || !globalDocId) {
      setGlobalDoc(null);
      return;
    }
    designDocApi
      .get(globalDocId)
      .then((d) => setGlobalDoc(d.project_id ? null : d))
      .catch(() => setGlobalDoc(null));
  }, [authStatus, globalDocId]);

  const openGlobalDoc = (id: string) => {
    setGlobalDocId(id);
    localStorage.setItem(LAST_GLOBAL_DOC_KEY, id);
    goTo("gdoc");
  };

  const currentDocPlace = (): DocPlace | null =>
    view === "doc" && projectId
      ? { view: "doc", projectId }
      : view === "gdoc" && globalDocId
        ? { view: "gdoc", docId: globalDocId }
        : null;

  const goToDocPlace = (place: DocPlace) => {
    if (place.view === "doc") {
      selectProject(place.projectId);
      goTo("doc");
    } else openGlobalDoc(place.docId);
  };

  /** A `doc:<id>#chapter` link: open that doc -- a project's (switching to the
   * project) or a global one -- in the same language, at that chapter. */
  const followDocLink = (docId: string, anchor: string | null, lang: DocLang) => {
    designDocApi
      .get(docId)
      .then((doc) => {
        const here = currentDocPlace();
        if (here) setDocHistory((h) => [...h, here]);
        setDocLanding({ anchor, lang, nonce: Date.now() });
        goToDocPlace(doc.project_id ? { view: "doc", projectId: doc.project_id } : { view: "gdoc", docId: doc.id });
      })
      .catch(() => alert(t("doc.linkMissing")));
  };

  const docBack = () => {
    const prev = docHistory[docHistory.length - 1];
    if (!prev) return;
    setDocHistory((h) => h.slice(0, -1));
    setDocLanding(null);
    goToDocPlace(prev);
  };

  const onProjectsLoaded = (projects: Project[]) => {
    const mine = projects.find((p) => p.id === projectId);
    if (projectId && !mine) selectProject("");
    else if (mine) setProjectName(mine.name);
  };

  const goTo = (next: View) => {
    // Leaving the docs ends a link-following trail: coming back to the Doc
    // tab later must not jump to a chapter a link once asked for.
    if (next !== "doc" && next !== "gdoc") {
      setDocLanding(null);
      setDocHistory([]);
    }
    setView(next);
    localStorage.setItem(LAST_VIEW_KEY, next);
    setMenuOpen(false);
  };

  const openProject = (id: string) => {
    selectProject(id);
    goTo("grid");
  };

  if (authStatus !== "authenticated") {
    return (
      <div className="app-shell">
        <div className="main-area" style={{ display: "flex", alignItems: "center", justifyContent: "center" }}>
          <div style={{ display: "flex", flexDirection: "column", gap: 14, alignItems: "center" }}>
            <h1 style={{ margin: 0 }}>ComfyUI Orchestrator</h1>
            {authStatus === "checking" ? <span style={{ color: "var(--text-dim)" }}>{t("app.connecting")}</span> : <ConnectionBar forceOpen />}
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
          aria-label={t("app.menu")}
          aria-expanded={menuOpen}
        >
          {menuOpen ? "✕" : "☰"}
        </button>
        <div className={cx("topbar-menu", menuOpen && "open")}>
          <button
            onClick={() => goTo(view === "projects" && projectId ? "grid" : "projects")}
            className={cx("topbar-project", view === "projects" && "active")}
          >
            📁 {projectName ?? t("project.select")}
          </button>
          <div className="topbar-spacer" />
          {view !== "grid" && <button onClick={() => goTo("grid")}>{t("app.backToGrid")}</button>}
          {/* Pre-production lives here: idea, references, divergence. The grid
              is convergent by construction and can't hold any of it -- see
              roadmap.md §1. */}
          <button onClick={() => goTo(view === "board" ? "grid" : "board")} className={view === "board" ? "active" : ""}>
            {t("app.board")}
          </button>
          <button onClick={() => goTo(view === "doc" ? "grid" : "doc")} className={view === "doc" ? "active" : ""}>
            {t("app.doc")}
          </button>
          <button onClick={() => goTo(view === "agent" ? "grid" : "agent")} className={view === "agent" ? "active" : ""}>
            {t("app.agent")}
          </button>
          <button onClick={() => goTo(view === "logs" ? "grid" : "logs")} className={view === "logs" ? "active" : ""}>
            {t("app.logs")}
          </button>
          <button onClick={() => goTo(view === "settings" ? "grid" : "settings")} className={view === "settings" ? "active" : ""}>
            {t("app.settings")}
          </button>
          <ConnectionBar />
        </div>
      </div>
      {view === "projects" ||
      (!projectId && (view === "grid" || view === "board" || view === "doc")) ||
      (view === "gdoc" && !globalDoc) ? (
        // Also what a grid/board with no project to show falls back to.
        <ProjectsPage
          projectId={projectId}
          onOpen={openProject}
          onOpenDoc={openGlobalDoc}
          onOpenProjectDoc={(id) => {
            selectProject(id);
            goTo("doc");
          }}
          onProjectsLoaded={onProjectsLoaded}
        />
      ) : view === "gdoc" && globalDoc ? (
        <DesignDoc
          key={`g:${globalDoc.id}:${docLanding?.nonce ?? 0}`}
          source={{ kind: "global", docId: globalDoc.id, title: globalDoc.title }}
          landing={docLanding}
          onFollow={followDocLink}
          onBack={docHistory.length ? docBack : undefined}
        />
      ) : view === "settings" ? (
        <div className="main-area">
          <Settings />
        </div>
      ) : view === "logs" ? (
        <div className="main-area">
          <Logs />
        </div>
      ) : view === "agent" ? (
        // Rendered without a project too: dev chats aren't about any project.
        <AgentChat projectId={projectId} />
      ) : view === "doc" && projectId ? (
        <DesignDoc
          key={`p:${projectId}:${docLanding?.nonce ?? 0}`}
          source={{ kind: "project", projectId }}
          landing={docLanding}
          onFollow={followDocLink}
          onBack={docHistory.length ? docBack : undefined}
        />
      ) : view === "board" && projectId ? (
        <Board projectId={projectId} />
      ) : (
        projectId && <Grid projectId={projectId} />
      )}
    </div>
  );
}
