import { useEffect, useRef, useState } from "react";
import { logsApi } from "../api/endpoints";

export function Logs() {
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
      .catch((err) => setError(err instanceof Error ? err.message : "Failed to load logs."));

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
    <div className="settings-panel">
      <div className="settings-section">
        <div className="node-cell-header">
          <span>Logs</span>
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <label style={{ fontWeight: 400, fontSize: 12, display: "flex", gap: 4, alignItems: "center" }}>
              <input type="checkbox" checked={autoRefresh} onChange={(e) => setAutoRefresh(e.target.checked)} />
              auto-refresh
            </label>
            <button onClick={reload}>Refresh</button>
            <button
              onClick={async () => {
                if (!confirm("Clear the log file? This can't be undone.")) return;
                await logsApi.clear();
                reload();
              }}
              title="Truncates the log file on disk"
            >
              Clear
            </button>
          </div>
        </div>
        {error && <div className="error-text">{error}</div>}
        <pre ref={boxRef} className="log-viewer">
          {lines.length > 0 ? lines.join("\n") : "(no log output yet)"}
        </pre>
      </div>
    </div>
  );
}
