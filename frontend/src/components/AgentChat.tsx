import { useEffect, useMemo, useRef, useState } from "react";
import { agentChatsApi } from "../api/endpoints";
import { useT } from "../i18n";
import type { TKey } from "../locales/en";
import { renderMarkdown } from "../markdown";
import { NightPanel } from "./NightPanel";
import type { AgentChat as Chat, AgentChatKind, AgentEvent, AgentModel, PermissionDecision } from "../types";
import { cx } from "../utils";

const LAST_CHAT_KEY = "comfy-orchestrator:lastAgentChat";
// A new chat starts on whatever model was picked last, not always the default.
const LAST_MODEL_KEY = "comfy-orchestrator:lastAgentModel";
// Same for a dev chat's permission mode.
const LAST_MODE_KEY = "comfy-orchestrator:lastAgentPermissionMode";
// The runner owns the list (and its order); these are just the names shown.
const MODE_LABELS: Record<string, TKey> = {
  auto: "agent.mode.auto",
  default: "agent.mode.default",
  acceptEdits: "agent.mode.acceptEdits",
};
const MODE_HINTS: Record<string, TKey> = {
  auto: "agent.modeHint.auto",
  default: "agent.modeHint.default",
  acceptEdits: "agent.modeHint.acceptEdits",
};
const LIST_POLL_MS = 5000;
const RECONNECT_MS = 3000;
// Either the CLI is working or it is paused on a question for the person;
// in both a new message would be refused and Stop is what's on offer.
const BUSY = new Set(["running", "awaiting_approval"]);
const DECIDED: Record<string, TKey> = {
  allow: "agent.permission.decided.allow",
  allow_session: "agent.permission.decided.allow_session",
  deny: "agent.permission.decided.deny",
  cancelled: "agent.permission.decided.cancelled",
  deny_unattended: "agent.permission.decided.deny_unattended",
};
// Sidebar entry for the night mode panel, selected like a chat.
const NIGHT_ID = "__night__";

/** Agent chats. Two kinds, listed in their own sections:
 * - project: an agent bound to the selected project, working only through the
 *   orchestrator's MCP tools;
 * - dev: Claude Code in the dev copy of this app, able to change the site
 *   itself; tool calls it isn't already allowed to make come here as
 *   Allow / Deny cards. Offered only when the runner has it enabled.
 * The chats live in the agent runner (agent_runner/runner.py), so they keep
 * going while this page is closed or the orchestrator restarts -- reopening
 * just replays the transcript. */
export function AgentChat({ projectId }: { projectId: string | null }) {
  const t = useT();
  const [chats, setChats] = useState<Chat[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(() => localStorage.getItem(LAST_CHAT_KEY));
  const [error, setError] = useState<string | null>(null);
  const [models, setModels] = useState<AgentModel[]>([]);
  const [permissionModes, setPermissionModes] = useState<string[]>([]);
  const [kinds, setKinds] = useState<AgentChatKind[]>([]);

  useEffect(() => {
    agentChatsApi
      .models()
      .then((r) => {
        setModels(r.models);
        setPermissionModes(r.permission_modes ?? []);
      })
      .catch(() => setModels([]));
    agentChatsApi
      .kinds()
      .then((r) => setKinds(r.kinds))
      .catch(() => setKinds([]));
  }, []);

  const reload = () =>
    agentChatsApi
      .list()
      .then((list) => {
        setError(null);
        setChats(list);
      })
      .catch((err) => setError(err instanceof Error ? err.message : String(err)));

  useEffect(() => {
    reload();
    const id = setInterval(reload, LIST_POLL_MS);
    return () => clearInterval(id);
  }, []);

  const select = (id: string | null) => {
    setSelectedId(id);
    if (id) localStorage.setItem(LAST_CHAT_KEY, id);
    else localStorage.removeItem(LAST_CHAT_KEY);
  };

  const createChat = async (kind: AgentChatKind) => {
    try {
      const lastModel = localStorage.getItem(LAST_MODEL_KEY);
      const model = models.some((m) => m.id === lastModel) ? lastModel! : undefined;
      const lastMode = localStorage.getItem(LAST_MODE_KEY);
      const mode = kind === "dev" && lastMode && permissionModes.includes(lastMode) ? lastMode : undefined;
      const chat = await agentChatsApi.create(kind, kind === "project" ? projectId : null, model, mode);
      setChats((cs) => [chat, ...cs]);
      select(chat.id);
    } catch (err) {
      alert(err instanceof Error ? err.message : String(err));
    }
  };

  const removeChat = async (chat: Chat) => {
    if (!confirm(t("agent.confirmDelete", { title: chat.title || t("agent.untitled") }))) return;
    await agentChatsApi.remove(chat.id);
    if (selectedId === chat.id) select(null);
    reload();
  };

  const projectChats = chats.filter((c) => c.kind === "project" && c.project_id === projectId);
  const devChats = chats.filter((c) => c.kind === "dev");
  // A remembered project chat from another project isn't in any visible list.
  const selected = [...projectChats, ...devChats].find((c) => c.id === selectedId) ?? null;
  const nightSelected = selectedId === NIGHT_ID && kinds.includes("dev");

  const renderItem = (c: Chat) => (
    <div key={c.id} className={cx("agent-chat-item", c.id === selectedId && "active")} onClick={() => select(c.id)}>
      <span className={cx("agent-status-dot", `agent-status-${c.status}`)} title={statusLabel(t, c.status)} />
      <span className="agent-chat-title">
        {c.origin === "agent" && <span title={t("agent.fromAgent")}>🤖 </span>}
        {c.title || t("agent.untitled")}
      </span>
      <button
        className="agent-chat-delete"
        title={t("agent.delete")}
        onClick={(e) => {
          e.stopPropagation();
          removeChat(c);
        }}
      >
        ✕
      </button>
    </div>
  );

  return (
    <div className={cx("agent-view", (selected || nightSelected) && "has-selection")}>
      <div className="agent-sidebar">
        {error && <div className="error-text">{error}</div>}
        <div className="agent-section-head">
          <span>{t("agent.section.project")}</span>
          {projectId && (
            <button onClick={() => createChat("project")} title={t("agent.newProjectChat")}>
              +
            </button>
          )}
        </div>
        {!projectId ? (
          <div className="node-cell-hint">{t("app.pickProjectForAgent")}</div>
        ) : projectChats.length === 0 && !error ? (
          <div className="node-cell-hint">{t("agent.noChats")}</div>
        ) : (
          projectChats.map(renderItem)
        )}
        {kinds.includes("dev") && (
          <>
            <div className="agent-section-head">
              <span>{t("agent.section.dev")}</span>
              <button onClick={() => createChat("dev")} title={t("agent.newDevChat")}>
                +
              </button>
            </div>
            <div className={cx("agent-chat-item", nightSelected && "active")} onClick={() => select(NIGHT_ID)}>
              <span className="agent-chat-title">🌙 {t("night.title")}</span>
            </div>
            {devChats.length === 0 ? <div className="node-cell-hint">{t("agent.noDevChats")}</div> : devChats.map(renderItem)}
          </>
        )}
      </div>
      <div className="agent-main">
        {nightSelected ? (
          <NightPanel onOpenChat={select} onBack={() => select(null)} />
        ) : selected ? (
          <ChatView key={selected.id} chat={selected} models={models} permissionModes={permissionModes} onBack={() => select(null)} onChanged={reload} />
        ) : (
          <div className="agent-empty">{t("agent.pickOrCreate")}</div>
        )}
      </div>
    </div>
  );
}

function statusLabel(t: ReturnType<typeof useT>, status: string): string {
  switch (status) {
    case "running":
      return t("agent.status.running");
    case "awaiting_approval":
      return t("agent.status.awaitingApproval");
    case "stopped":
      return t("agent.status.stopped");
    case "error":
      return t("agent.status.error");
    case "interrupted":
      return t("agent.status.interrupted");
    default:
      return t("agent.status.idle");
  }
}

/** Streams one chat's transcript over SSE. EventSource reconnects by itself
 * after a dropped connection (sending Last-Event-ID); a hard failure (the
 * orchestrator answering 503 while the runner is down, say) closes it for
 * good, so that case is retried here with `after` = the last event we hold. */
function useChatEvents(chatId: string) {
  const [events, setEvents] = useState<AgentEvent[]>([]);
  const [connected, setConnected] = useState(false);
  const lastSeq = useRef(0);

  useEffect(() => {
    let source: EventSource | null = null;
    let retry: ReturnType<typeof setTimeout> | null = null;
    let disposed = false;

    const open = () => {
      source = new EventSource(agentChatsApi.eventsUrl(chatId, lastSeq.current));
      source.onopen = () => setConnected(true);
      source.onmessage = (msg) => {
        const event = JSON.parse(msg.data) as AgentEvent;
        if (event.seq <= lastSeq.current) return;
        lastSeq.current = event.seq;
        setEvents((es) => [...es, event]);
      };
      source.onerror = () => {
        setConnected(false);
        if (source?.readyState === EventSource.CLOSED && !disposed) {
          retry = setTimeout(open, RECONNECT_MS);
        }
      };
    };
    open();
    return () => {
      disposed = true;
      source?.close();
      if (retry) clearTimeout(retry);
    };
  }, [chatId]);

  return { events, connected };
}

function ChatView({
  chat,
  models,
  permissionModes,
  onBack,
  onChanged,
}: {
  chat: Chat;
  models: AgentModel[];
  permissionModes: string[];
  onBack: () => void;
  onChanged: () => void;
}) {
  const t = useT();
  const { events, connected } = useChatEvents(chat.id);
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const boxRef = useRef<HTMLDivElement>(null);

  // The stream is the source of truth for status; the list's copy may be up
  // to one poll behind.
  const status = useMemo(() => {
    for (let i = events.length - 1; i >= 0; i--) {
      const e = events[i];
      if (e.type === "status") return e.status;
    }
    return chat.status;
  }, [events, chat.status]);
  const running = BUSY.has(status);

  const decisions = useMemo(() => {
    const map = new Map<string, string>();
    for (const e of events) if (e.type === "permission_decision") map.set(e.request_id, e.decision);
    return map;
  }, [events]);

  const decide = (requestId: string, decision: PermissionDecision) =>
    agentChatsApi.decide(chat.id, requestId, decision).then(onChanged, (err) => alert(err instanceof Error ? err.message : String(err)));

  const results = useMemo(() => {
    const map = new Map<string, Extract<AgentEvent, { type: "tool_result" }>>();
    for (const e of events) if (e.type === "tool_result") map.set(e.tool_use_id, e);
    return map;
  }, [events]);

  useEffect(() => {
    const el = boxRef.current;
    if (!el) return;
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 120;
    if (nearBottom) el.scrollTop = el.scrollHeight;
  }, [events]);

  // Jump to the bottom once the replay of an opened chat has arrived.
  useEffect(() => {
    const el = boxRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [connected]);

  const send = async () => {
    const text = draft.trim();
    if (!text || running || sending) return;
    setSending(true);
    try {
      await agentChatsApi.send(chat.id, text);
      setDraft("");
      onChanged();
    } catch (err) {
      alert(err instanceof Error ? err.message : String(err));
    } finally {
      setSending(false);
    }
  };

  return (
    <>
      <div className="agent-header">
        <button className="agent-back" onClick={onBack}>
          ←
        </button>
        <span className="agent-header-title">{chat.title || t("agent.untitled")}</span>
        <span className={cx("agent-status-dot", `agent-status-${status}`)} />
        <span className="node-cell-hint">{statusLabel(t, status)}</span>
        {!connected && <span className="node-cell-hint">· {t("agent.reconnecting")}</span>}
        <span className="agent-header-controls">
        {chat.kind === "dev" && chat.origin !== "agent" && permissionModes.length > 0 && (
          <select
            className="agent-model"
            value={chat.permission_mode}
            title={MODE_HINTS[chat.permission_mode ?? ""] ? t(MODE_HINTS[chat.permission_mode ?? ""]) : undefined}
            onChange={async (e) => {
              const mode = e.target.value;
              localStorage.setItem(LAST_MODE_KEY, mode);
              await agentChatsApi.setPermissionMode(chat.id, mode);
              onChanged();
            }}
          >
            {permissionModes.map((m) => (
              <option key={m} value={m}>
                {MODE_LABELS[m] ? t(MODE_LABELS[m]) : m}
              </option>
            ))}
          </select>
        )}
        {models.length > 0 && (
          <select
            className="agent-model"
            value={chat.requested_model}
            title={chat.model ? t("agent.lastRanOn", { model: chat.model }) : t("agent.modelHint")}
            onChange={async (e) => {
              const model = e.target.value;
              localStorage.setItem(LAST_MODEL_KEY, model);
              await agentChatsApi.setModel(chat.id, model);
              onChanged();
            }}
          >
            {models.map((m) => (
              <option key={m.id} value={m.id}>
                {m.label}
              </option>
            ))}
          </select>
        )}
        </span>
      </div>
      <div className="agent-transcript" ref={boxRef}>
        {events.map((e) => {
          switch (e.type) {
            case "user":
              return e.origin === "agent" ? (
                <div key={e.seq} className="agent-msg agent-msg-report">
                  <div className="agent-meta">🤖 {t("agent.reportFromAgent")}</div>
                  {e.text}
                </div>
              ) : (
                <div key={e.seq} className="agent-msg agent-msg-user">
                  {e.text}
                </div>
              );
            case "text":
              return (
                <div
                  key={e.seq}
                  className="agent-msg agent-msg-assistant"
                  // renderMarkdown escapes before it formats (see markdown.ts).
                  dangerouslySetInnerHTML={{ __html: renderMarkdown(e.text) }}
                />
              );
            case "tool_use": {
              const result = results.get(e.id);
              return (
                <details key={e.seq} className={cx("agent-tool", result?.is_error && "error")}>
                  <summary>
                    {result ? (result.is_error ? "✗" : "✓") : "…"} {e.name.replace(/^mcp__orchestrator__/, "")}
                  </summary>
                  <pre>{e.input}</pre>
                  {result && <pre className="agent-tool-result">{result.content}</pre>}
                </details>
              );
            }
            case "result":
              return (
                <div key={e.seq} className="agent-meta">
                  {e.duration_ms != null && `${(e.duration_ms / 1000).toFixed(1)}s`}
                  {e.cost_usd != null && ` · $${e.cost_usd.toFixed(3)}`}
                </div>
              );
            case "night":
              return (
                <div key={e.seq} className="agent-msg agent-msg-night">
                  🌙 {e.text}
                </div>
              );
            case "error":
              return (
                <div key={e.seq} className="agent-msg agent-msg-error">
                  {e.text}
                </div>
              );
            case "permission_request": {
              const decision = decisions.get(e.request_id);
              return (
                <div key={e.seq} className={cx("agent-permission", !decision && "pending")}>
                  <div>
                    <strong>{t("agent.permission.asks", { tool: e.tool })}</strong>
                    {e.description && <span className="node-cell-hint"> — {e.description}</span>}
                  </div>
                  <pre>{e.input}</pre>
                  {decision ? (
                    <div className="agent-meta">{DECIDED[decision] ? t(DECIDED[decision]) : decision}</div>
                  ) : (
                    <div className="agent-permission-actions">
                      <button className="primary" onClick={() => decide(e.request_id, "allow")}>
                        {t("agent.permission.allow")}
                      </button>
                      <button onClick={() => decide(e.request_id, "allow_session")} title={t("agent.permission.allowSessionHint")}>
                        {t("agent.permission.allowSession")}
                      </button>
                      <button className="agent-stop" onClick={() => decide(e.request_id, "deny")}>
                        {t("agent.permission.deny")}
                      </button>
                    </div>
                  )}
                </div>
              );
            }
            case "permission_decision":
              return null;
            case "model":
              return (
                <div key={e.seq} className="agent-meta">
                  {t("agent.modelSwitched", { model: models.find((m) => m.id === e.model)?.label ?? e.model })}
                </div>
              );
            case "mode":
              return (
                <div key={e.seq} className="agent-meta">
                  {t("agent.modeSwitched", { mode: MODE_LABELS[e.mode] ? t(MODE_LABELS[e.mode]) : e.mode })}
                </div>
              );
            case "status":
              return e.status === "stopped" ? (
                <div key={e.seq} className="agent-meta">
                  {t("agent.status.stopped")}
                </div>
              ) : null;
            default:
              return null;
          }
        })}
        {status === "running" && <div className="agent-meta">{t("agent.working")}</div>}
      </div>
      <div className="agent-input">
        <textarea
          value={draft}
          placeholder={t("agent.placeholder")}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
              e.preventDefault();
              send();
            }
          }}
          rows={3}
        />
        {running ? (
          <button className="agent-stop" onClick={() => agentChatsApi.stop(chat.id).then(onChanged)}>
            {t("agent.stop")}
          </button>
        ) : (
          <button className="primary" onClick={send} disabled={!draft.trim() || sending}>
            {t("agent.send")}
          </button>
        )}
      </div>
    </>
  );
}
