import { useEffect, useState } from "react";
import { nightApi } from "../api/endpoints";
import { useT } from "../i18n";
import type { NightSession, NightStatus } from "../types";
import { cx } from "../utils";

const POLL_MS = 5000;

const time = (ts: number) => new Date(ts * 1000).toLocaleString();

/** Night mode (agent_runner/night.py): bug reports from other agents worked
 * unattended -- backed up, fixed, deployed, committed on a night branch --
 * and decided here in the morning, for the whole night at once. */
export function NightPanel({ onOpenChat, onBack }: { onOpenChat: (id: string) => void; onBack: () => void }) {
  const t = useT();
  const [status, setStatus] = useState<NightStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [diff, setDiff] = useState<string | null>(null);
  const [finishing, setFinishing] = useState<"accept" | "reject" | null>(null);
  const [result, setResult] = useState<string[] | null>(null);

  const reload = () =>
    nightApi
      .status()
      .then((s) => {
        setError(null);
        setStatus(s);
      })
      .catch((err) => setError(err instanceof Error ? err.message : String(err)));

  useEffect(() => {
    reload();
    const id = setInterval(reload, POLL_MS);
    return () => clearInterval(id);
  }, []);

  const toggle = async (accept: boolean) => {
    try {
      setStatus(await nightApi.setAcceptReports(accept));
    } catch (err) {
      alert(err instanceof Error ? err.message : String(err));
    }
  };

  const finish = async (action: "accept" | "reject") => {
    if (!confirm(t(action === "accept" ? "night.confirmAccept" : "night.confirmReject"))) return;
    setFinishing(action);
    setResult(null);
    try {
      const r = await nightApi.finish(action);
      setResult(r.steps);
    } catch (err) {
      // Reject redeploys main and restarts this server under the request;
      // the runner finishes regardless, so a dropped connection isn't a
      // failure -- the status poll shows how it ended.
      setResult([t("night.finishDropped", { error: err instanceof Error ? err.message : String(err) })]);
    } finally {
      setFinishing(null);
      setDiff(null);
      reload();
    }
  };

  const session = status?.session ?? null;

  return (
    <>
      <div className="agent-header">
        <button className="agent-back" onClick={onBack}>
          ←
        </button>
        <span className="agent-header-title">🌙 {t("night.title")}</span>
        {status?.busy && <span className="node-cell-hint">· {t("night.busy")}</span>}
      </div>
      <div className="agent-transcript night-panel">
        {error && <div className="error-text">{error}</div>}
        {status && !status.available && <div className="node-cell-hint">{t("night.unavailable")}</div>}
        {status?.available && (
          <div className="night-card">
            <label className="night-toggle">
              <input type="checkbox" checked={status.accept_reports} onChange={(e) => toggle(e.target.checked)} />
              <strong>{t("night.accept")}</strong>
            </label>
            <div className="node-cell-hint">{t("night.explain")}</div>
            <div className="node-cell-hint">{t("night.commitFirst")}</div>
            {status.queued > 0 && <div>{t("night.queued", { n: status.queued })}</div>}
          </div>
        )}

        {session && (
          <div className="night-card">
            <div>
              <strong>{t("night.session", { id: session.id })}</strong>{" "}
              <span className="node-cell-hint">{time(session.started_at)}</span>
            </div>
            <div className="node-cell-hint">
              {session.branch_created ? t("night.onBranch", { branch: session.branch }) : t("night.noBranch")}
              {" · "}
              {session.stash ? t("night.stashed") : t("night.nothingStashed")}
            </div>
            {session.notes.map((n, i) => (
              <div key={i} className="agent-msg agent-msg-error">
                {n.text}
              </div>
            ))}
            <ReportList session={session} onOpenChat={onOpenChat} />
            {session.commits.length > 0 && (
              <>
                <div className="night-subhead">{t("night.commits")}</div>
                {session.commits.map((c) => (
                  <div key={c.sha} className="night-commit">
                    <code>{c.sha.slice(0, 10)}</code> {c.summary}
                    <div className="node-cell-hint">{c.files.join(", ")}</div>
                  </div>
                ))}
                {diff === null ? (
                  <button onClick={() => nightApi.diff().then((d) => setDiff(d.diff))}>{t("night.showDiff")}</button>
                ) : (
                  <DiffView diff={diff} />
                )}
              </>
            )}
            <div className="agent-permission-actions">
              <button className="primary" disabled={!!finishing || status?.busy} onClick={() => finish("accept")}>
                {finishing === "accept" ? t("night.working") : session.commits.length ? t("night.acceptAll") : t("night.close")}
              </button>
              {session.commits.length > 0 && (
                <button className="agent-stop" disabled={!!finishing || status?.busy} onClick={() => finish("reject")}>
                  {finishing === "reject" ? t("night.working") : t("night.rejectAll")}
                </button>
              )}
            </div>
            <div className="node-cell-hint">{t(session.commits.length ? "night.decisionHint" : "night.closeHint")}</div>
          </div>
        )}

        {result && (
          <div className="night-card">
            {result.map((line, i) => (
              <div key={i}>{line}</div>
            ))}
          </div>
        )}

        {status && status.history.length > 0 && (
          <>
            <div className="night-subhead">{t("night.history")}</div>
            {status.history.map((h) => (
              <details key={h.id} className="night-card">
                <summary>
                  {h.id} — {h.outcome === "accepted" ? t("night.accepted") : t("night.rejected")} ·{" "}
                  {t("night.fixCount", { n: h.commits.length })}
                </summary>
                <ReportList session={h} onOpenChat={onOpenChat} />
                <pre className="night-detail">{h.outcome_detail}</pre>
              </details>
            ))}
          </>
        )}
      </div>
    </>
  );
}

function ReportList({ session, onOpenChat }: { session: NightSession; onOpenChat: (id: string) => void }) {
  const t = useT();
  if (!session.reports.length) return <div className="node-cell-hint">{t("night.noReports")}</div>;
  return (
    <div className="night-reports">
      {session.reports.map((r) => (
        <div key={r.id} className="agent-chat-item" onClick={() => onOpenChat(r.id)}>
          <span className={cx("night-outcome", `night-outcome-${r.outcome ?? r.stage}`)}>{r.outcome ?? r.stage}</span>
          <span className="agent-chat-title">{r.title}</span>
        </div>
      ))}
    </div>
  );
}

function DiffView({ diff }: { diff: string }) {
  return (
    <pre className="night-diff">
      {diff.split("\n").map((line, i) => (
        <div
          key={i}
          className={cx(
            line.startsWith("+") && !line.startsWith("+++") && "add",
            line.startsWith("-") && !line.startsWith("---") && "del",
            line.startsWith("@@") && "hunk",
            line.startsWith("diff ") && "file",
          )}
        >
          {line || " "}
        </div>
      ))}
    </pre>
  );
}
