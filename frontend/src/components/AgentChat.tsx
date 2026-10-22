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
  HandoffDecision,
  HandoffProfile,
  HandoffProfiles,
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
  ask: "agent.mode.ask",
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
  ask: "agent.modeHint.ask",
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
const HANDOFF_DECIDED: Record<string, TKey> = {
  allow_once: "agent.handoff.decided.allow_once",
  allow_always: "agent.handoff.decided.allow_always",
  deny_once: "agent.handoff.decided.deny_once",
  deny_always: "agent.handoff.decided.deny_always",
  timeout: "agent.handoff.decided.timeout",
};
// Sidebar entry for the night mode panel, selected like a chat.
const NIGHT_ID = "__night__";
const HANDOFF_RULES_ID = "__handoff_rules__";
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
  const handoffRulesSelected = selectedId === HANDOFF_RULES_ID;

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
    <div className={cx("agent-view", (selected || nightSelected || handoffRulesSelected) && "has-selection")}>
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
        <div className={cx("agent-chat-item", handoffRulesSelected && "active")} onClick={() => select(HANDOFF_RULES_ID)}>
          <span className="agent-chat-title">⚙️ {t("agent.handoff.rules")}</span>
        </div>
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
        ) : handoffRulesSelected ? (
          <HandoffRulesPanel onBack={() => select(null)} />
        ) : selected ? (
          <ChatView
            key={selected.id}
            chat={selected}
            provider={providers.find((p) => p.id === (selected.provider ?? "claude"))}
            onBack={() => select(null)}
            onChanged={reload}
            onSetDone={(done) => setDone(selected, done)}
            onOpenChat={select}
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

function renderScarceNotice(t: ReturnType<typeof useT>, e: AgentEvent & { type: "handoff_request" }): string {
  const d = e.limits_detail;
  if (!d) {
    return t("agent.handoff.scarceNoticeSimple", { target: e.target_label || e.target });
  }
  const label = (d.group && LIMIT_GROUP_LABELS[d.group] ? t(LIMIT_GROUP_LABELS[d.group]) : (d.label || d.window_id));
  const pct = Math.round((d.used ?? 0) * 100);
  let winStr = "";
  if (d.duration === 18000 || d.window_id.includes("5h") || d.window_id.includes("five_hour")) {
    winStr = t("agent.handoff.window5h");
  } else if (d.duration === 604800 || d.window_id.includes("weekly") || d.window_id.includes("seven_day")) {
    winStr = t("agent.handoff.window7d");
  } else if (d.duration) {
    winStr = t("agent.handoff.windowGeneric", { h: Math.round(d.duration / 3600) });
  }
  if (d.resets_at) {
    const resetTime = new Date(d.resets_at * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    return t("agent.handoff.scarceNotice", { label, pct, window: winStr, reset: resetTime });
  }
  return t("agent.handoff.scarceNoticeNoReset", { label, pct, window: winStr });
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

function HandoffCountdown({ expiresAt }: { expiresAt: number }) {
  const t = useT();
  const [nowSec, setNowSec] = useState(() => Date.now() / 1000);
  useEffect(() => {
    const iv = setInterval(() => setNowSec(Date.now() / 1000), 1000);
    return () => clearInterval(iv);
  }, []);
  const remaining = Math.max(0, Math.round(expiresAt - nowSec));
  return <span className="agent-handoff-countdown">⏱ {t("agent.handoff.secondsRemaining", { s: remaining })}</span>;
}

function HandoffRulesPanel({ onBack }: { onBack: () => void }) {
  const t = useT();
  const [rules, setRules] = useState<Record<string, "allow" | "deny"> | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    agentChatsApi.getHandoffRules().then(
      (res) => {
        setRules(res.rules || {});
        setLoading(false);
      },
      (err) => {
        console.error("Failed to load handoff rules:", err);
        setLoading(false);
      },
    );
  }, []);

  const updateRule = async (pairKey: string, decision: string) => {
    if (!rules) return;
    const nextRules = { ...rules };
    if (decision === "remove") {
      delete nextRules[pairKey];
    } else if (decision === "allow" || decision === "deny") {
      nextRules[pairKey] = decision;
    }
    setRules(nextRules);
    try {
      await agentChatsApi.updateHandoffRules(nextRules);
    } catch (err) {
      alert(err instanceof Error ? err.message : String(err));
    }
  };

  const entries = Object.entries(rules || {});

  return (
    <div className="agent-handoff-rules-panel">
      <div className="agent-header">
        <button className="agent-back" onClick={onBack}>
          ←
        </button>
        <strong>⚙️ {t("agent.handoff.rules")}</strong>
      </div>
      <div className="agent-handoff-rules-body">
        {loading ? (
          <div className="node-cell-hint">…</div>
        ) : entries.length === 0 ? (
          <div className="node-cell-hint">{t("agent.handoff.noRules")}</div>
        ) : (
          <div className="agent-handoff-rules-list">
            {entries.map(([pair, decision]) => (
              <div key={pair} className="agent-handoff-rule-item">
                <span className="agent-handoff-rule-pair">{pair}</span>
                <select
                  className="agent-model"
                  value={decision}
                  onChange={(e) => updateRule(pair, e.target.value)}
                >
                  <option value="allow">{t("agent.handoff.ruleAllow")}</option>
                  <option value="deny">{t("agent.handoff.ruleDeny")}</option>
                  <option value="remove">{t("agent.handoff.ruleRemove")}</option>
                </select>
              </div>
            ))}
          </div>
        )}
        <HandoffProfilesEditor />
      </div>
    </div>
  );
}

/** What each handoff target is good for, as agents see it. Edited by hand on
 * purpose: nothing infers it at run time. */
function HandoffProfilesEditor() {
  const t = useT();
  const [data, setData] = useState<HandoffProfiles | null>(null);
  const [drafts, setDrafts] = useState<Record<string, HandoffProfile>>({});

  useEffect(() => {
    agentChatsApi.getHandoffProfiles().then(
      (res) => {
        setData(res);
        setDrafts(res.profiles);
      },
      (err) => console.error("Failed to load handoff profiles:", err),
    );
  }, []);

  if (!data) return null;
  const draftOf = (target: string): HandoffProfile =>
    drafts[target] ?? { good_for: "", appetite: "", draws_natively: false };
  const edit = (target: string, patch: Partial<HandoffProfile>) =>
    setDrafts((d) => ({ ...d, [target]: { ...draftOf(target), ...patch } }));
  const save = async (target: string) => {
    try {
      const res = await agentChatsApi.updateHandoffProfile(target, draftOf(target));
      setData((prev) => (prev ? { ...prev, profiles: res.profiles } : prev));
    } catch (err) {
      alert(err instanceof Error ? err.message : String(err));
    }
  };
  const dirty = (target: string) => JSON.stringify(draftOf(target)) !== JSON.stringify(data.profiles[target] ?? null);

  return (
    <div className="agent-handoff-profiles">
      <strong>{t("agent.handoff.profiles")}</strong>
      <div className="node-cell-hint">{t("agent.handoff.profilesHint")}</div>
      {data.targets.map(({ target, label }) => {
        const p = draftOf(target);
        return (
          <div key={target} className="agent-handoff-profile">
            <div className="agent-handoff-rule-pair">{label}</div>
            <input value={p.good_for} placeholder={t("agent.handoff.goodFor")} onChange={(e) => edit(target, { good_for: e.target.value })} />
            <input value={p.appetite} placeholder={t("agent.handoff.appetite")} onChange={(e) => edit(target, { appetite: e.target.value })} />
            <label>
              <input type="checkbox" checked={p.draws_natively} onChange={(e) => edit(target, { draws_natively: e.target.checked })} />{" "}
              {t("agent.handoff.drawsNatively")}
            </label>
            {dirty(target) && (
              <button className="primary" onClick={() => save(target)}>
                {t("agent.handoff.saveProfile")}
              </button>
            )}
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

interface ChatMediaItem {
  id: string;
  url: string;
  mimeType: string;
  name?: string;
  previewUrl?: string;
  downloadUrl?: string;
}

function ChatMedia({
  items,
  large = false,
  caption = "",
  onQuote,
}: {
  items: ChatMediaItem[];
  large?: boolean;
  caption?: string;
  onQuote?: (item: ChatMediaItem) => void;
}) {
  const t = useT();
  const [copiedId, setCopiedId] = useState<string | null>(null);

  const copyLink = async (item: ChatMediaItem) => {
    try {
      await navigator.clipboard.writeText(item.url);
      setCopiedId(item.id);
      setTimeout(() => setCopiedId((c) => (c === item.id ? null : c)), 2000);
    } catch {
      const ta = document.createElement("textarea");
      ta.value = item.url;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      document.body.removeChild(ta);
      setCopiedId(item.id);
      setTimeout(() => setCopiedId((c) => (c === item.id ? null : c)), 2000);
    }
  };

  return (
    <>
      <div className={cx("agent-images", large && "agent-generated-images")}>
        {items.map((item) => (
          <div key={item.id} className="agent-media-item">
            {item.mimeType.startsWith("image/") ? (
              <a href={item.url} target="_blank" rel="noreferrer">
                <img src={item.previewUrl ?? item.url} alt={caption || item.name || ""} loading="lazy" />
              </a>
            ) : item.mimeType.startsWith("video/") ? (
              <video src={item.url} controls preload="metadata" />
            ) : item.mimeType.startsWith("audio/") ? (
              <audio src={item.url} controls preload="metadata" />
            ) : null}
            <div className="agent-media-actions">
              {item.downloadUrl && (
                <a className="agent-media-download" href={item.downloadUrl} download={item.name || true}>
                  ↓ {t("agent.downloadFile")}{item.name ? ` · ${item.name}` : ""}
                </a>
              )}
              {onQuote && (
                <button
                  type="button"
                  className="agent-media-action"
                  title={t("agent.quoteImageTitle")}
                  onClick={() => onQuote(item)}
                >
                  ↩ {t("agent.quote")}
                </button>
              )}
              <button
                type="button"
                className="agent-media-action"
                title={t("agent.copyLinkTitle")}
                onClick={() => copyLink(item)}
              >
                {copiedId === item.id ? `✓ ${t("agent.copied")}` : `🔗 ${t("agent.copyLink")}`}
              </button>
            </div>
          </div>
        ))}
      </div>
      {caption && <div className="agent-media-caption">{caption}</div>}
    </>
  );
}

function ChatImages({
  chatId,
  images,
  generated = false,
  onQuote,
}: {
  chatId: string;
  images: string[];
  generated?: boolean;
  onQuote?: (item: ChatMediaItem) => void;
}) {
  return (
    <ChatMedia
      large={generated}
      onQuote={onQuote}
      items={images.map((name) => {
        const url = agentChatsApi.attachmentUrl(chatId, name);
        return {
          id: name,
          name,
          url,
          mimeType: "image/*",
          previewUrl: generated ? agentChatsApi.attachmentUrl(chatId, name, true) : undefined,
          downloadUrl: generated ? agentChatsApi.attachmentUrl(chatId, name, false, true) : undefined,
        };
      })}
    />
  );
}

function ChatView({
  chat,
  provider,
  onBack,
  onChanged,
  onSetDone,
  onOpenChat,
}: {
  chat: Chat;
  provider: AgentProviderInfo | undefined;
  onBack: () => void;
  onChanged: () => void;
  onSetDone: (done: boolean) => void;
  onOpenChat?: (chatId: string) => void;
}) {
  const t = useT();
  const { events, connected } = useChatEvents(chat.id);
  const [draft, setDraft] = useState("");
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [sending, setSending] = useState(false);
  const boxRef = useRef<HTMLDivElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const isFrozen = !!chat.handed_off_to;

  const handoffDecisions = useMemo(() => {
    const map = new Map<string, string>();
    for (const e of events) {
      if (e.type === "handoff_declined") map.set(e.request_id, e.timeout ? "timeout" : "deny_once");
      else if (e.type === "handoff_out" && e.request_id) map.set(e.request_id, "allow_once");
    }
    return map;
  }, [events]);

  const handleHandoffDecision = (requestId: string, decision: HandoffDecision) => {
    agentChatsApi.decideHandoff(chat.id, requestId, decision).then(
      (res) => {
        onChanged();
        if (res.chat_id && onOpenChat) {
          onOpenChat(res.chat_id);
        }
      },
      (err) => alert(err instanceof Error ? err.message : String(err)),
    );
  };

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

  const quoteMedia = async (item: ChatMediaItem) => {
    if (chat.accepts_images && item.mimeType.startsWith("image/")) {
      try {
        const resp = await fetch(item.previewUrl ?? item.url);
        if (resp.ok) {
          const blob = await resp.blob();
          const file = new File([blob], item.name || `${item.id}.png`, { type: blob.type || "image/png" });
          const a = await readAttachment(file);
          setAttachments((list) => (list.some((existing) => existing.data === a.data) ? list : [...list, a]));
        }
      } catch (err) {
        console.warn("Could not attach media directly", err);
      }
    }
    const label = item.name ? `[${t("agent.image")}: ${item.name}]` : `[${t("agent.image")}]`;
    const quoteLine = `> ${label}\n`;
    setDraft((prev) => (prev ? `${prev.trimEnd()}\n\n${quoteLine}` : quoteLine));
    textareaRef.current?.focus();
  };

  const quoteText = (text: string) => {
    const sel = window.getSelection()?.toString()?.trim();
    const target = sel || text.trim();
    if (!target) return;
    const quoted = target
      .split("\n")
      .map((line) => `> ${line}`)
      .join("\n");
    setDraft((prev) => (prev ? `${prev.trimEnd()}\n\n${quoted}\n\n` : `${quoted}\n\n`));
    textareaRef.current?.focus();
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
    if ((!text && !attachments.length) || running || sending || isFrozen) return;
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
            title={provider.id === "codex" && chat.permission_mode === "auto" ? t("agent.modeHint.codexAuto") : MODE_HINTS[chat.permission_mode ?? ""] ? t(MODE_HINTS[chat.permission_mode ?? ""]) : undefined}
            onChange={async (e) => {
              const mode = e.target.value;
              localStorage.setItem(lastModeKey(chat.provider ?? "claude"), mode);
              await agentChatsApi.setPermissionMode(chat.id, mode);
              onChanged();
            }}
          >
            {provider.permission_modes.map((m) => (
              <option key={m} value={m}>
                {provider.id === "codex" && m === "auto" ? t("agent.mode.codexAuto") : MODE_LABELS[m] ? t(MODE_LABELS[m]) : m}
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
              if (e.origin === "agent") {
                return (
                  <div key={e.seq} className="agent-msg agent-msg-report">
                    <div className="agent-meta">🤖 {t("agent.reportFromAgent")}</div>
                    {e.text}
                  </div>
                );
              }
              if (e.origin === "handoff") {
                return (
                  <details key={e.seq} className="agent-msg agent-msg-handoff" open={false}>
                    <summary className="agent-handoff-summary">
                      🔄 {t("agent.handoff.fromCard", { source: chat.handoff_from?.target_label || chat.handoff_from?.target || "previous chat" })}
                    </summary>
                    <pre className="agent-handoff-body">{e.text}</pre>
                  </details>
                );
              }
              if (e.origin === "system") {
                return (
                  <div key={e.seq} className="agent-msg agent-msg-system">
                    {e.text}
                  </div>
                );
              }
              return (
                <div key={e.seq} className="agent-msg agent-msg-user">
                  {e.images && e.images.length > 0 && (
                    <ChatImages chatId={chat.id} images={e.images} onQuote={quoteMedia} />
                  )}
                  {e.text}
                </div>
              );
            case "media":
              return (
                <div key={e.seq} className="agent-msg agent-msg-assistant">
                  <ChatMedia
                    large
                    caption={e.caption}
                    onQuote={quoteMedia}
                    items={[{
                      id: e.asset_id, name: e.name, mimeType: e.mime_type,
                      url: agentChatsApi.assetMediaUrl(e.asset_id),
                      previewUrl: e.mime_type.startsWith("image/") ? agentChatsApi.assetMediaUrl(e.asset_id, "fit") : undefined,
                      downloadUrl: agentChatsApi.assetMediaUrl(e.asset_id, "file", true),
                    }]}
                  />
                </div>
              );
            case "image":
              return (
                <div key={e.seq} className="agent-msg agent-msg-assistant">
                  <ChatImages chatId={chat.id} images={e.images} generated onQuote={quoteMedia} />
                </div>
              );
            case "text":
              return (
                <div key={e.seq} className="agent-msg agent-msg-assistant">
                  <div
                    // renderMarkdown escapes before it formats (see markdown.ts).
                    dangerouslySetInnerHTML={{ __html: renderMarkdown(e.text) }}
                  />
                  <div className="agent-msg-actions">
                    <button
                      type="button"
                      className="agent-msg-action"
                      title={t("agent.quoteMessageTitle")}
                      onClick={() => quoteText(e.text)}
                    >
                      ↩ {t("agent.quote")}
                    </button>
                  </div>
                </div>
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
                      <button disabled={e.allowed_decisions && !e.allowed_decisions.includes("allow")} className="primary" onClick={() => decide(e.request_id, "allow")}>
                        {t("agent.permission.allow")}
                      </button>
                      {(!e.allowed_decisions || e.allowed_decisions.includes("allow_session")) && <button onClick={() => decide(e.request_id, "allow_session")} title={t("agent.permission.allowSessionHint")}>
                        {t("agent.permission.allowSession")}
                      </button>}
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
            case "handoff_request": {
              const decision = handoffDecisions.get(e.request_id) || (chat.pending_handoff?.request_id === e.request_id ? null : (e.expires_at && e.expires_at < Date.now() / 1000 ? "timeout" : null));
              const isScarce = Boolean(e.scarce || (chat.pending_handoff?.request_id === e.request_id && chat.pending_handoff?.scarce));
              return (
                <div key={e.seq} className={cx("agent-permission", !decision && "pending")}>
                  <div>
                    <strong>🔄 {t("agent.handoff.asks", { target: e.target_label || e.target })}</strong>
                    {e.reason && <span className="node-cell-hint"> — {e.reason}</span>}
                  </div>
                  {isScarce && (
                    <div className="agent-handoff-scarce-notice">
                      ⚠️ {renderScarceNotice(t, e)}
                    </div>
                  )}
                  <pre>{e.task_preview || e.task}</pre>
                  {decision ? (
                    <div className="agent-meta">{HANDOFF_DECIDED[decision] ? t(HANDOFF_DECIDED[decision]) : decision}</div>
                  ) : isScarce ? (
                    <div className="agent-permission-actions">
                      <button className="primary" onClick={() => handleHandoffDecision(e.request_id, "allow_once")}>
                        {t("agent.handoff.yes")}
                      </button>
                      <button className="agent-stop" onClick={() => handleHandoffDecision(e.request_id, "deny_once")}>
                        {t("agent.handoff.no")}
                      </button>
                      {e.expires_at && <HandoffCountdown expiresAt={e.expires_at} />}
                    </div>
                  ) : (
                    <div className="agent-permission-actions">
                      <button className="primary" onClick={() => handleHandoffDecision(e.request_id, "allow_once")}>
                        {t("agent.handoff.allowOnce")}
                      </button>
                      <button onClick={() => handleHandoffDecision(e.request_id, "allow_always")}>
                        {t("agent.handoff.allowAlways")}
                      </button>
                      <button className="agent-stop" onClick={() => handleHandoffDecision(e.request_id, "deny_once")}>
                        {t("agent.handoff.denyOnce")}
                      </button>
                      <button className="agent-stop" onClick={() => handleHandoffDecision(e.request_id, "deny_always")}>
                        {t("agent.handoff.denyAlways")}
                      </button>
                      {e.expires_at && <HandoffCountdown expiresAt={e.expires_at} />}
                    </div>
                  )}
                </div>
              );
            }
            case "handoff_declined":
              return (
                <div key={e.seq} className="agent-msg agent-msg-error">
                  🚫 {t("agent.handoff.declined", { target: e.target_label || e.target })}{e.timeout ? ` (${t("agent.handoff.decided.timeout")})` : ""}
                </div>
              );
            case "handoff_out":
              return null;
            default:
              return null;
          }
        })}
        {status === "running" && <div className="agent-meta">{t("agent.working")}</div>}
      </div>
      {chat.handed_off_to && (
        <div className="agent-handoff-frozen-banner">
          <span>
            🔒 {t("agent.handoff.frozenBanner", { target: chat.handed_off_to.target_label || chat.handed_off_to.target })}
          </span>
          {onOpenChat && (
            <button className="primary" onClick={() => onOpenChat(chat.handed_off_to!.chat_id)}>
              {t("agent.handoff.open")}
            </button>
          )}
        </div>
      )}
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
            <button className="agent-attach" title={t("agent.attach")} disabled={isFrozen || running || sending} onClick={() => fileRef.current?.click()}>
              📎
            </button>
            <input
              ref={fileRef}
              type="file"
              accept="image/*"
              multiple
              hidden
              disabled={isFrozen || running || sending}
              onChange={(e) => {
                attach(Array.from(e.target.files ?? []));
                e.target.value = "";
              }}
            />
          </>
        )}
        <textarea
          ref={textareaRef}
          value={draft}
          placeholder={isFrozen ? t("agent.handoff.chatFrozen") : t("agent.placeholder")}
          disabled={isFrozen || running || sending}
          onChange={(e) => setDraft(e.target.value)}
          onPaste={
            chat.accepts_images && !isFrozen
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
              disabled={running || sending || isFrozen}
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
            <button className="primary" onClick={send} disabled={(!draft.trim() && !attachments.length) || sending || isFrozen}>
              {t("agent.send")}
            </button>
          )}
        </div>
      </div>
    </>
  );
}
