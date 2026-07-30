import { useEffect, useRef, useState } from "react";
import { logsApi } from "../api/endpoints";
import { clearClientLog, getClientLog, subscribeClientLog, type ClientLogEntry } from "../clientLog";
import { useT } from "../i18n";
import { cx } from "../utils";

type Source = "server" | "browser";

export function Logs() {
  const t = useT();
  const [source, setSource] = useState<Source>("server");

  return (
    <div className="settings-panel">
      <div className="settings-section">
        <div className="node-cell-header">
          <div style={{ display: "flex", gap: 6 }}>
            <button className={cx(source === "server" && "active")} onClick={() => setSource("server")}>
              {t("logs.server")}
            </button>
            <button className={cx(source === "browser" && "active")} onClick={() => setSource("browser")}>
              {t("logs.browser")}
            </button>
          </div>
        </div>
        {source === "server" ? <ServerLog /> : <BrowserLog />}
      </div>
    </div>
  );
}

function ServerLog() {
  const t = useT();
  const [lines, setLines] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [autoRefresh, setAutoRefresh] = useState(true);
  const boxRef = useRef<HTMLPreElement>(null);

  const reload = () =>
    logsApi
      .tail(500)
      .then((r) => {
        setError(null);
        setLines(r.lines);
      })
      .catch((err) => setError(err instanceof Error ? err.message : t("logs.loadFailed")));

  useEffect(() => {
    reload();
    if (!autoRefresh) return;
    const id = setInterval(reload, 4000);
    return () => clearInterval(id);
  }, [autoRefresh]);

  // Keep the view pinned to the newest lines unless the user has scrolled up
  // to read older output -- otherwise every auto-refresh yanks them back down.
  useEffect(() => {
    const el = boxRef.current;
    if (!el) return;
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 60;
    if (nearBottom) el.scrollTop = el.scrollHeight;
  }, [lines]);

  return (
    <>
      <div className="node-cell-header" style={{ justifyContent: "flex-end" }}>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <label style={{ fontWeight: 400, fontSize: 12, display: "flex", gap: 4, alignItems: "center" }}>
            <input type="checkbox" checked={autoRefresh} onChange={(e) => setAutoRefresh(e.target.checked)} />
            {t("logs.autoRefresh")}
          </label>
          <button onClick={reload}>{t("common.refresh")}</button>
          <button
            onClick={async () => {
              if (!confirm(t("logs.confirmClear"))) return;
              await logsApi.clear();
              reload();
            }}
            title={t("logs.clearTitle")}
          >
            {t("common.clear")}
          </button>
        </div>
      </div>
      {error && <div className="error-text">{error}</div>}
      <pre ref={boxRef} className="log-viewer">
        {lines.length > 0 ? lines.join("\n") : t("logs.empty")}
      </pre>
    </>
  );
}

// Errors/warnings captured in this browser session (see clientLog.ts) -- lets
// phone-only crashes be read back on the site, since a mobile browser has no
// dev console.
function BrowserLog() {
  const t = useT();
  const [entries, setEntries] = useState<ClientLogEntry[]>(getClientLog());
  const boxRef = useRef<HTMLPreElement>(null);

  useEffect(() => subscribeClientLog(() => setEntries([...getClientLog()])), []);

  useEffect(() => {
    const el = boxRef.current;
    if (!el) return;
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 60;
    if (nearBottom) el.scrollTop = el.scrollHeight;
  }, [entries]);

  const text = entries
    .map((e) => `${e.t.slice(11, 23)} [${e.level}] ${e.msg}`)
    .join("\n");

  return (
    <>
      <div className="node-cell-header" style={{ justifyContent: "flex-end" }}>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <span className="node-cell-hint">{t("logs.entries", { n: entries.length })}</span>
          <button
            onClick={() => {
              navigator.clipboard?.writeText(text).then(
                () => alert(t("logs.copied")),
                () => alert(t("logs.copyFailed")),
              );
            }}
          >
            {t("common.copy")}
          </button>
          <button onClick={() => setEntries([...getClientLog()])}>{t("common.refresh")}</button>
          <button onClick={() => clearClientLog()}>{t("common.clear")}</button>
        </div>
      </div>
      <pre ref={boxRef} className="log-viewer">
        {entries.length > 0 ? text : t("logs.browserEmpty")}
      </pre>
    </>
  );
}
