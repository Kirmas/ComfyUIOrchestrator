import { useEffect, useMemo, useRef, useState } from "react";
import { agentChatsApi } from "../api/endpoints";
import { useT } from "../i18n";
import type { TKey } from "../locales/en";
import { renderMarkdown } from "../markdown";
import { AgentProviderIcon } from "./AgentProviderIcon";
import { NightPanel } from "./NightPanel";
import type {
  AgentChat as Chat,
  AgentLimits,
  AgentLimitWindow,
  AgentChatKind,
  AgentEvent,
  AgentImageUpload,
  AgentProviderId,
  AgentProviderInfo,
  AskUserQuestionInput,
  AskUserQuestionItem,
  PermissionDecision,
} from "../types";
import { cx } from "../utils";

const LAST_CHAT_KEY = "comfy-orchestrator:lastAgentChat";
// A new chat starts on whatever model was picked last, not always the default.
// Per provider: a Claude alias means nothing to Codex.
const lastModelKey = (provider: AgentProviderId) => `comfy-orchestrator:lastAgentModel:${provider}`;
// Same for a dev chat's permission mode.
const lastModeKey = (provider: AgentProviderId) => `comfy-orchestrator:lastAgentPermissionMode:${provider}`;
const lastEffortKey = (provider: AgentProviderId) => `comfy-orchestrator:lastAgentEffort:${provider}`;
const lastFastKey = (provider: AgentProviderId) => `comfy-orchestrator:lastAgentFast:${provider}`;
const LIMITS_POLL_MS = 30000;
// The CLI's own picker calls xhigh "Extra"; ids stay what --effort takes.
const EFFORT_LABELS: Record<string, string> = { xhigh: "xhigh (Extra)", ultracode: "ultracode ✦" };
const LIMIT_LABELS: Record<string, TKey> = {
  five_hour: "agent.limits.fiveHour",
  seven_day: "agent.limits.sevenDay",
  "gemini-weekly": "agent.limits.weekly",
  "gemini-5h": "agent.limits.fiveHour",
  "3p-weekly": "agent.limits.weekly",
  "3p-5h": "agent.limits.fiveHour",
  "gemini-argon-wtu-weekly": "agent.limits.argonWeekly",
};
const LIMIT_GROUP_LABELS: Record<string, TKey> = {
  "Gemini Models": "agent.limits.groupGemini",
  "Claude and GPT models": "agent.limits.group3p",
  "Gemini 4 Argon": "agent.limits.groupArgon",
};
const SHOW_DONE_KEY = "comfy-orchestrator:agentShowDone";
// The runner owns the list (and its order); these are just the names shown.
const MODE_LABELS: Record<string, TKey> = {
  auto: "agent.mode.auto",
  default: "agent.mode.default",
  acceptEdits: "agent.mode.acceptEdits",
  workspace: "agent.mode.workspace",
  readOnly: "agent.mode.readOnly",
  full: "agent.mode.full",
  edits: "agent.mode.edits",
  plan: "agent.mode.plan",
};
const MODE_HINTS: Record<string, TKey> = {
  auto: "agent.modeHint.auto",
  default: "agent.modeHint.default",
  acceptEdits: "agent.modeHint.acceptEdits",
  workspace: "agent.modeHint.workspace",
  readOnly: "agent.modeHint.readOnly",
  full: "agent.modeHint.full",
  edits: "agent.modeHint.edits",
  plan: "agent.modeHint.plan",
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
// The model sees at most ~1568px on the long side anyway; bigger only costs
// upload time from a phone. Past the byte cap a PNG falls back to JPEG.
const IMAGE_MAX_SIDE = 1568;
const IMAGE_MAX_BYTES = 3.5 * 1024 * 1024;

type Attachment = AgentImageUpload & { preview: string };

/** A picture from the person's machine, shrunk to what the model can use. */
async function readAttachment(file: File): Promise<Attachment> {
  const bitmap = await createImageBitmap(file);
  const scale = Math.min(1, IMAGE_MAX_SIDE / Math.max(bitmap.width, bitmap.height));
  let blob: Blob = file;
  if (scale < 1 || file.size > IMAGE_MAX_BYTES || !["image/png", "image/jpeg", "image/webp"].includes(file.type)) {
    const canvas = document.createElement("canvas");
    canvas.width = Math.round(bitmap.width * scale);
    canvas.height = Math.round(bitmap.height * scale);
    canvas.getContext("2d")!.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
    const encode = (type: string) => new Promise<Blob | null>((ok) => canvas.toBlob(ok, type, 0.9));
    blob = (file.type === "image/png" && (await encode("image/png"))) || file;
    if (blob === file || blob.size > IMAGE_MAX_BYTES) blob = (await encode("image/jpeg"))!;
  }
  bitmap.close();
  const dataUrl = await new Promise<string>((ok, fail) => {
    const reader = new FileReader();
    reader.onload = () => ok(reader.result as string);
    reader.onerror = () => fail(reader.error);
    reader.readAsDataURL(blob);
  });
  return { media_type: blob.type, data: dataUrl.slice(dataUrl.indexOf(",") + 1), preview: dataUrl };
}

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
  const [providers, setProviders] = useState<AgentProviderInfo[]>([]);
  const [kinds, setKinds] = useState<AgentChatKind[]>([]);
  const [showDone, setShowDone] = useState(() => localStorage.getItem(SHOW_DONE_KEY) === "1");

  useEffect(() => {
    agentChatsApi
      .providers()
      .then((r) => setProviders(r.providers ?? []))
      .catch(() => setProviders([]));
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

  const createChat = async (kind: AgentChatKind, provider: AgentProviderInfo) => {
    try {
      const lastModel = localStorage.getItem(lastModelKey(provider.id));
      const model = provider.models.some((m) => m.id === lastModel)
        ? lastModel!
        : (provider.models.find((m) => lastModel?.startsWith(m.id))?.id ?? undefined);
      const effectiveModelId = model ?? provider.default;
      const modelInfo = provider.models.find((m) => m.id === effectiveModelId);
      const modelEfforts = modelInfo?.efforts ?? provider.efforts;
      const lastMode = localStorage.getItem(lastModeKey(provider.id));
      const permissionMode = kind === "dev" && lastMode && provider.permission_modes.includes(lastMode) ? lastMode : undefined;
      const lastEffort = localStorage.getItem(lastEffortKey(provider.id));
      const effort = lastEffort && modelEfforts.includes(lastEffort) ? lastEffort : undefined;
      const fastMode = provider.fast_models.length > 0 && localStorage.getItem(lastFastKey(provider.id)) === "1";
      const chat = await agentChatsApi.create(kind, kind === "project" ? projectId : null, {
        provider: provider.id,
        model,
        permissionMode,
        effort,
        fastMode,
      });
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

  const setDone = async (chat: Chat, done: boolean) => {
    await agentChatsApi.setDone(chat.id, done);
    reload();
  };

  const toggleShowDone = (on: boolean) => {
    setShowDone(on);
    localStorage.setItem(SHOW_DONE_KEY, on ? "1" : "0");
  };

  // Project chats need a CLI that can be limited to the orchestrator's tools;
  // dev chats can run on any installed one.
  const projectProviders = providers.filter((p) => p.available && p.supports_project);
  const devProviders = providers.filter((p) => p.available);
  const allProjectChats = chats.filter((c) => c.kind === "project" && c.project_id === projectId);
  const allDevChats = chats.filter((c) => c.kind === "dev");
  // A remembered project chat from another project isn't in any visible list.
  // A done one stays open until you leave it; it's only the list that hides it.
  const selected = [...allProjectChats, ...allDevChats].find((c) => c.id === selectedId) ?? null;
  const listed = (c: Chat) => showDone || !c.done;
  const projectChats = allProjectChats.filter(listed);
  const devChats = allDevChats.filter(listed);
  const doneCount = chats.filter((c) => c.done && (c.kind === "dev" || c.project_id === projectId)).length;
  const nightSelected = selectedId === NIGHT_ID && kinds.includes("dev");

  const renderItem = (c: Chat) => (
    <div key={c.id} className={cx("agent-chat-item", c.id === selectedId && "active", c.done && "done")} onClick={() => select(c.id)}>
      <span className={cx("agent-status-dot", `agent-status-${c.status}`)} title={statusLabel(t, c.status)} />
      <span className="agent-chat-title">
        {c.origin === "agent" && <span title={t("agent.fromAgent")}>🤖 </span>}
        <AgentProviderIcon provider={c.provider} size={13} />{" "}
        {c.title || t("agent.untitled")}
      </span>
      <button
        className="agent-chat-delete"
        title={c.done ? t("agent.undone") : t("agent.markDone")}
        onClick={(e) => {
          e.stopPropagation();
          setDone(c, !c.done);
        }}
      >
        {c.done ? "↺" : "✓"}
      </button>
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
            <span className="agent-provider-buttons">
              {projectProviders.map((p) => (
                <button key={p.id} onClick={() => createChat("project", p)} title={t("agent.newProjectChatWith", { provider: p.label })}>
                  <AgentProviderIcon provider={p.id} />
                </button>
              ))}
            </span>
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
              <span className="agent-provider-buttons">
                {devProviders.map((p) => (
                  <button key={p.id} onClick={() => createChat("dev", p)} title={t("agent.newDevChatWith", { provider: p.label })}>
                    <AgentProviderIcon provider={p.id} />
                  </button>
                ))}
              </span>
            </div>
            <div className={cx("agent-chat-item", nightSelected && "active")} onClick={() => select(NIGHT_ID)}>
              <span className="agent-chat-title">🌙 {t("night.title")}</span>
            </div>
            {devChats.length === 0 ? <div className="node-cell-hint">{t("agent.noDevChats")}</div> : devChats.map(renderItem)}
          </>
        )}
        <LimitsPanel providers={providers} />
        {doneCount > 0 && (
          <label className="agent-show-done">
            <input type="checkbox" checked={showDone} onChange={(e) => toggleShowDone(e.target.checked)} />
            {t("agent.showDone", { count: doneCount })}
          </label>
        )}
      </div>
      <div className="agent-main">
        {nightSelected ? (
          <NightPanel onOpenChat={select} onBack={() => select(null)} />
        ) : selected ? (
          <ChatView
            key={selected.id}
            chat={selected}
            provider={providers.find((p) => p.id === (selected.provider ?? "claude"))}
            onBack={() => select(null)}
            onChanged={reload}
            onSetDone={(done) => setDone(selected, done)}
          />
        ) : (
          <div className="agent-empty">{t("agent.pickOrCreate")}</div>
        )}
      </div>
    </div>
  );
}

/** "in 2 h 2 min" / "in 3 d 4 h" until an epoch-seconds moment. */
function untilText(t: ReturnType<typeof useT>, epochSeconds: number): string {
  const minutes = Math.max(0, Math.round((epochSeconds * 1000 - Date.now()) / 60000));
  if (minutes < 60) return t("agent.limits.inMin", { m: minutes });
  if (minutes < 24 * 60) return t("agent.limits.inHourMin", { h: Math.floor(minutes / 60), m: minutes % 60 });
  return t("agent.limits.inDayHour", { d: Math.floor(minutes / 1440), h: Math.floor((minutes % 1440) / 60) });
}

/** "5 min ago" for an epoch-seconds moment in the past. */
function agoText(t: ReturnType<typeof useT>, epochSeconds: number): string {
  const minutes = Math.max(0, Math.round((Date.now() - epochSeconds * 1000) / 60000));
  return minutes < 60 ? t("agent.limits.minAgo", { m: minutes }) : t("agent.limits.hourAgo", { h: Math.floor(minutes / 60) });
}

/** Plan usage per provider: how much of each window is used and when it
 * resets. The runner keeps the last reading (every turn brings a fresh one for
 * free); the refresh button spends a tiny turn to take one now. Only providers
 * that report limits show up. */
function LimitsPanel({ providers }: { providers: AgentProviderInfo[] }) {
  const t = useT();
  const [limits, setLimits] = useState<Record<string, AgentLimits>>({});
  const [busy, setBusy] = useState<string | null>(null);

  useEffect(() => {
    const load = () => agentChatsApi.limits().then(setLimits, () => {});
    load();
    const id = setInterval(load, LIMITS_POLL_MS);
    return () => clearInterval(id);
  }, []);

  const refresh = async (provider: AgentProviderInfo) => {
    setBusy(provider.id);
    try {
      setLimits(await agentChatsApi.refreshLimits(provider.id));
    } catch (err) {
      alert(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  };

  const shown = providers.filter((p) => p.available && (limits[p.id] || p.id === "claude"));
  if (shown.length === 0) return null;
  return (
    <div className="agent-limits">
      {shown.map((p) => {
        const reading = limits[p.id];
        const groups: { name?: string; windows: AgentLimitWindow[] }[] = [];
        if (reading?.windows) {
          for (const w of reading.windows) {
            const gName = w.group;
            const last = groups[groups.length - 1];
            if (last && last.name === gName) {
              last.windows.push(w);
            } else {
              groups.push({ name: gName, windows: [w] });
            }
          }
        }
        return (
          <div key={p.id} className="agent-limits-provider">
            <div className="agent-limits-head">
              <AgentProviderIcon provider={p.id} size={13} /> <span>{t("agent.limits.title")}</span>
              <button className="agent-chat-delete" title={t("agent.limits.refresh")} disabled={busy === p.id} onClick={() => refresh(p)}>
                {busy === p.id ? "…" : "↻"}
              </button>
            </div>
            {!reading && <div className="node-cell-hint">{t("agent.limits.none")}</div>}
            {groups.map((grp, idx) => (
              <div key={grp.name ?? idx} className={cx(grp.name && "agent-limit-group")}>
                {grp.name && (
                  <div className="agent-limit-group-header">
                    {LIMIT_GROUP_LABELS[grp.name] ? t(LIMIT_GROUP_LABELS[grp.name]) : grp.name}
                  </div>
                )}
                {grp.windows.map((w) => {
                  const pct = w.disabled ? null : (w.utilization === null ? null : Math.round(w.utilization * 100));
                  return (
                    <div key={w.id} className={cx("agent-limit-row", w.disabled && "disabled")}>
                      <div className="agent-limit-label">
                        <span>{LIMIT_LABELS[w.id] ? t(LIMIT_LABELS[w.id]) : (w.label || w.id)}</span>
                        <span className="node-cell-hint">
                          {w.disabled ? (
                            t("agent.limits.disabled")
                          ) : (
                            <>
                              {w.resets_at ? `${t("agent.limits.resets", { when: untilText(t, w.resets_at) })} · ` : ""}
                              {pct === null ? "?" : `${pct}%`}
                            </>
                          )}
                        </span>
                      </div>
                      <div className="agent-limit-bar">
                        <div
                          className={cx("agent-limit-fill", !w.disabled && pct !== null && pct >= 90 && "hot")}
                          style={{ width: `${w.disabled ? 0 : (pct ?? 0)}%` }}
                        />
                      </div>
                    </div>
                  );
                })}
              </div>
            ))}
            {reading && <div className="node-cell-hint">{t("agent.limits.asOf", { ago: agoText(t, reading.updated_at) })}</div>}
          </div>
        );
      })}
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

/** One question/options card for the built-in AskUserQuestion tool. Picking
 * an option toggles it (multiSelect: several at once, joined with ", " to
 * match exactly what the CLI's own answer format uses -- verified live,
 * 2026-10-01); typing free text clears any picks for that question and vice
 * versa. Submit is disabled until every question has an answer. */
function AskUserQuestionCard({
  input,
  onSubmit,
  t,
}: {
  input: AskUserQuestionInput;
  onSubmit: (answers: Record<string, string>) => void;
  t: ReturnType<typeof useT>;
}) {
  const [picked, setPicked] = useState<Record<string, string[]>>({});
  const [freeText, setFreeText] = useState<Record<string, string>>({});

  const answerFor = (q: AskUserQuestionItem): string => (picked[q.question]?.length ? picked[q.question].join(", ") : (freeText[q.question] ?? "").trim());
  const complete = input.questions.every((q) => answerFor(q));

  const toggle = (q: AskUserQuestionItem, label: string) => {
    setPicked((m) => {
      const cur = m[q.question] ?? [];
      const next = q.multiSelect ? (cur.includes(label) ? cur.filter((l) => l !== label) : [...cur, label]) : cur[0] === label ? [] : [label];
      return { ...m, [q.question]: next };
    });
    setFreeText((m) => ({ ...m, [q.question]: "" }));
  };

  return (
    <div className="agent-question">
      {input.questions.map((q) => (
        <div key={q.question} className="agent-question-item">
          <div className="agent-question-header">{q.header}</div>
          <div className="agent-question-text">{q.question}</div>
          <div className="agent-question-options">
            {q.options.map((opt) => (
              <button
                key={opt.label}
                type="button"
                className={cx("agent-question-option", (picked[q.question] ?? []).includes(opt.label) && "selected")}
                title={opt.description}
                onClick={() => toggle(q, opt.label)}
              >
                {opt.label}
              </button>
            ))}
          </div>
          <input
            className="agent-question-other"
            placeholder={t("agent.question.other")}
            value={freeText[q.question] ?? ""}
            onChange={(e) => {
              const value = e.target.value;
              setFreeText((m) => ({ ...m, [q.question]: value }));
              setPicked((m) => ({ ...m, [q.question]: [] }));
            }}
          />
        </div>
      ))}
      <button
        className="primary"
        disabled={!complete}
        onClick={() => onSubmit(Object.fromEntries(input.questions.map((q) => [q.question, answerFor(q)])))}
      >
        {t("agent.question.submit")}
      </button>
    </div>
  );
}

function formatTokens(count: number | null | undefined): string {
  if (!count) return "0";
  if (count >= 1_000_000) return `${(count / 1_000_000).toFixed(count % 1_000_000 === 0 ? 0 : 1)}M`;
  return `${Math.round(count / 1000)}k`;
}

function ChatView({
  chat,
  provider,
  onBack,
  onChanged,
  onSetDone,
}: {
  chat: Chat;
  provider: AgentProviderInfo | undefined;
  onBack: () => void;
  onChanged: () => void;
  onSetDone: (done: boolean) => void;
}) {
  const t = useT();
  const { events, connected } = useChatEvents(chat.id);
  const [draft, setDraft] = useState("");
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [sending, setSending] = useState(false);
  const boxRef = useRef<HTMLDivElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const attach = async (files: Iterable<File>) => {
    for (const file of files) {
      if (!file.type.startsWith("image/")) continue;
      try {
        const a = await readAttachment(file);
        setAttachments((list) => [...list, a]);
      } catch {
        alert(t("agent.imageFailed", { name: file.name || "image" }));
      }
    }
  };

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

  // "cancelled" shares the generic permission_decision event (the turn-end
  // cleanup in runner.py doesn't know which kind of question a pending
  // request_id was) -- the !map.has guard just means a real answer, which
  // can't coexist with a cancellation for the same request_id, always wins.
  const questionAnswers = useMemo(() => {
    const map = new Map<string, Record<string, string> | "cancelled">();
    for (const e of events) {
      if (e.type === "ask_user_answer") map.set(e.request_id, e.answers);
      else if (e.type === "permission_decision" && e.decision === "cancelled" && !map.has(e.request_id)) map.set(e.request_id, "cancelled");
    }
    return map;
  }, [events]);

  const answerQuestion = (requestId: string, answers: Record<string, string>) =>
    agentChatsApi.answerQuestion(chat.id, requestId, answers).then(onChanged, (err) => alert(err instanceof Error ? err.message : String(err)));

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
    if ((!text && !attachments.length) || running || sending) return;
    setSending(true);
    try {
      await agentChatsApi.send(
        chat.id,
        text,
        attachments.map(({ media_type, data }) => ({ media_type, data })),
      );
      setDraft("");
      setAttachments([]);
      onChanged();
    } catch (err) {
      alert(err instanceof Error ? err.message : String(err));
    } finally {
      setSending(false);
    }
  };

  const compact = async () => {
    if (sending || running) return;
    if (!confirm(t("agent.confirmCompact"))) return;
    setSending(true);
    try {
      await agentChatsApi.send(chat.id, "/compact", []);
      onChanged();
    } catch (err) {
      alert(err instanceof Error ? err.message : String(err));
    } finally {
      setSending(false);
    }
  };

  const lastResultEvent = useMemo(() => {
    for (let i = events.length - 1; i >= 0; i--) {
      if (events[i].type === "result") return events[i];
    }
    return null;
  }, [events]);
  const contextTokens = (lastResultEvent as any)?.context_tokens ?? chat.context_tokens;
  const contextWindow = (lastResultEvent as any)?.context_window ?? chat.context_window;
  const compactPct = contextTokens && contextWindow
    ? Math.min(100, Math.round((contextTokens / contextWindow) * 100))
    : 0;
  const compactFillColor = compactPct >= 80 ? "rgba(229, 72, 77, 0.22)" : "rgba(74, 140, 247, 0.18)";
  const compactStyle = compactPct > 0
    ? { background: `linear-gradient(to right, ${compactFillColor} ${compactPct}%, var(--bg) ${compactPct}%)` }
    : undefined;

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
        <span title={provider?.label}>
          <AgentProviderIcon provider={chat.provider} />
        </span>
        {chat.kind === "dev" && chat.origin !== "agent" && provider && provider.permission_modes.length > 0 && (
          <select
            className="agent-model"
            value={chat.permission_mode}
            title={MODE_HINTS[chat.permission_mode ?? ""] ? t(MODE_HINTS[chat.permission_mode ?? ""]) : undefined}
            onChange={async (e) => {
              const mode = e.target.value;
              localStorage.setItem(lastModeKey(chat.provider ?? "claude"), mode);
              await agentChatsApi.setPermissionMode(chat.id, mode);
              onChanged();
            }}
          >
            {provider.permission_modes.map((m) => (
              <option key={m} value={m}>
                {MODE_LABELS[m] ? t(MODE_LABELS[m]) : m}
              </option>
            ))}
          </select>
        )}
        {provider && (() => {
          const effectiveRequestedModel = provider.models.some((m) => m.id === chat.requested_model)
            ? chat.requested_model
            : (provider.models.find((m) => chat.requested_model?.startsWith(m.id))?.id ?? chat.requested_model);
          const selectedModel = provider.models.find((m) => m.id === effectiveRequestedModel);
          const modelEfforts = selectedModel?.efforts ?? provider.efforts;
          return (
            <>
              {modelEfforts.length > 0 && (
                <select
                  className="agent-model"
                  value={chat.effort ?? ""}
                  title={t("agent.effortHint")}
                  onChange={async (e) => {
                    const effort = e.target.value;
                    localStorage.setItem(lastEffortKey(chat.provider ?? "claude"), effort);
                    await agentChatsApi.setEffort(chat.id, effort);
                    onChanged();
                  }}
                >
                  <option value="">{t("agent.effort.default")}</option>
                  {modelEfforts.map((level) => (
                    <option key={level} value={level}>
                      {EFFORT_LABELS[level] ?? level}
                    </option>
                  ))}
                </select>
              )}
              {provider.fast_models.length > 0 && (
                <button
                  className={cx("agent-done-toggle", chat.fast_mode && "active")}
                  disabled={!provider.fast_models.includes(chat.requested_model)}
                  title={provider.fast_models.includes(chat.requested_model) ? t(provider.id === "codex" ? "agent.fastHintCodex" : "agent.fastHint") : t("agent.fastOnlyOn", { models: provider.fast_models.join(", ") })}
                  onClick={async () => {
                    const on = !chat.fast_mode;
                    localStorage.setItem(lastFastKey(chat.provider ?? "claude"), on ? "1" : "0");
                    await agentChatsApi.setFastMode(chat.id, on);
                    onChanged();
                  }}
                >
                  ⚡ {t("agent.fast")}
                </button>
              )}
              {provider.models.length > 0 && (
                <select
                  className="agent-model"
                  value={effectiveRequestedModel}
                  title={chat.model ? t("agent.lastRanOn", { model: chat.model }) : t("agent.modelHint")}
                  onChange={async (e) => {
                    const model = e.target.value;
                    localStorage.setItem(lastModelKey(chat.provider ?? "claude"), model);
                    await agentChatsApi.setModel(chat.id, model);
                    const targetModel = provider.models.find((m) => m.id === model);
                    const targetEfforts = targetModel?.efforts ?? provider.efforts;
                    if (chat.effort && targetEfforts.length > 0 && !targetEfforts.includes(chat.effort)) {
                      const newEffort = targetEfforts.includes("medium") ? "medium" : targetEfforts[0];
                      localStorage.setItem(lastEffortKey(chat.provider ?? "claude"), newEffort);
                      await agentChatsApi.setEffort(chat.id, newEffort);
                    }
                    onChanged();
                  }}
                >
                  {provider.models.map((m) => (
                    <option key={m.id} value={m.id}>
                      {m.label}
                    </option>
                  ))}
                </select>
              )}
            </>
          );
        })()}
        <button
          className={cx("agent-done-toggle", chat.done && "active")}
          title={chat.done ? t("agent.undone") : t("agent.markDone")}
          onClick={() => onSetDone(!chat.done)}
        >
          ✓ {t("agent.done")}
        </button>
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
                  {e.images && e.images.length > 0 && (
                    <div className="agent-images">
                      {e.images.map((name) => {
                        const url = agentChatsApi.attachmentUrl(chat.id, name);
                        return (
                          <a key={name} href={url} target="_blank" rel="noreferrer">
                            <img src={url} alt="" />
                          </a>
                        );
                      })}
                    </div>
                  )}
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
            case "ask_user_question": {
              const resolved = questionAnswers.get(e.request_id);
              return (
                <div key={e.seq} className={cx("agent-permission", "agent-question-card", !resolved && "pending")}>
                  <strong>{t("agent.permission.asks", { tool: "AskUserQuestion" })}</strong>
                  {resolved === "cancelled" ? (
                    <div className="agent-meta">{t("agent.question.cancelled")}</div>
                  ) : resolved ? (
                    <div className="agent-meta">
                      {t("agent.question.answered")}
                      {Object.entries(resolved).map(([q, a]) => (
                        <div key={q}>
                          — {q}: {a}
                        </div>
                      ))}
                    </div>
                  ) : (
                    <AskUserQuestionCard input={e.input} t={t} onSubmit={(answers) => answerQuestion(e.request_id, answers)} />
                  )}
                </div>
              );
            }
            case "ask_user_answer":
              return null;
            case "model":
              return (
                <div key={e.seq} className="agent-meta">
                  {t("agent.modelSwitched", { model: provider?.models.find((m) => m.id === e.model)?.label ?? e.model })}
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
      {attachments.length > 0 && (
        <div className="agent-images agent-attachments">
          {attachments.map((a, i) => (
            <span key={i} className="agent-attachment">
              <img src={a.preview} alt="" />
              <button
                title={t("agent.removeImage")}
                onClick={() => setAttachments((list) => list.filter((_, j) => j !== i))}
              >
                ×
              </button>
            </span>
          ))}
        </div>
      )}
      <div
        className="agent-input"
        onDragOver={chat.accepts_images ? (e) => e.preventDefault() : undefined}
        onDrop={
          chat.accepts_images
            ? (e) => {
                e.preventDefault();
                attach(e.dataTransfer.files);
              }
            : undefined
        }
      >
        {chat.accepts_images && (
          <>
            <button className="agent-attach" title={t("agent.attach")} onClick={() => fileRef.current?.click()}>
              📎
            </button>
            <input
              ref={fileRef}
              type="file"
              accept="image/*"
              multiple
              hidden
              onChange={(e) => {
                attach(Array.from(e.target.files ?? []));
                e.target.value = "";
              }}
            />
          </>
        )}
        <textarea
          value={draft}
          placeholder={t("agent.placeholder")}
          onChange={(e) => setDraft(e.target.value)}
          onPaste={
            chat.accepts_images
              ? (e) => {
                  const files = Array.from(e.clipboardData.files).filter((f) => f.type.startsWith("image/"));
                  if (files.length) {
                    e.preventDefault();
                    attach(files);
                  }
                }
              : undefined
          }
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
              e.preventDefault();
              send();
            }
          }}
          rows={3}
        />
        <div className="agent-input-actions">
          {chat.supports_compact && (
            <button
              type="button"
              className={cx("agent-compact-btn", compactPct >= 80 && "hot")}
              style={compactStyle}
              title={t("agent.compactHint", {
                used: formatTokens(contextTokens),
                total: formatTokens(contextWindow),
                pct: `${compactPct}%`,
              })}
              onClick={compact}
              disabled={running || sending}
            >
              <span className="agent-compact-label">{t("agent.compact")}</span>
              <span className="agent-compact-pct">{compactPct}%</span>
            </button>
          )}
          {running ? (
            <button className="agent-stop" onClick={() => agentChatsApi.stop(chat.id).then(onChanged)}>
              {t("agent.stop")}
            </button>
          ) : (
            <button className="primary" onClick={send} disabled={(!draft.trim() && !attachments.length) || sending}>
              {t("agent.send")}
            </button>
          )}
        </div>
      </div>
    </>
  );
}
