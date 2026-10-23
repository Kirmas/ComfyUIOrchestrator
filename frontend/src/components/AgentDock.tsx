import { useEffect, useState } from "react";
import { agentChatsApi } from "../api/endpoints";
import { useT } from "../i18n";
import { useAgentDock } from "../state/agentDock";
import { useProjectStore } from "../state/projectStore";
import type { AgentChat as Chat, AgentProviderInfo } from "../types";
import { cx } from "../utils";
import { ChatView, createAgentChat, rememberAgentChat, statusLabel } from "./AgentChat";
import { AgentProviderIcon } from "./AgentProviderIcon";

const POLL_MS = 5000;
// Statuses worth a dot on the closed bubble: working, waiting on you, broken.
const SIGNAL = new Set(["running", "awaiting_approval", "error"]);
const dockChatKey = (projectId: string) => `comfy-orchestrator:dockChat:${projectId}`;
const VIEW_NAMES = { grid: "Grid", board: "idea Board", doc: "Design doc" } as const;

export type DockView = keyof typeof VIEW_NAMES;

/** The project's agent chats without leaving the grid, board or design doc:
 * a bubble that opens a sheet over the bottom of the page (a side panel on a
 * wide screen). Project chats only -- dev chats stay in the Agent tab. Every
 * message sent from here says which view it came from; a message without
 * that context is what the Agent tab is for. */
export function AgentDock({
  projectId,
  projectName,
  view,
  onExpand,
}: {
  projectId: string;
  projectName: string | null;
  view: DockView;
  onExpand: () => void;
}) {
  const t = useT();
  const { open, picking, refs: allRefs, setOpen, setPicking, removeRef, clearRefs } = useAgentDock();
  const refs = allRefs.filter((r) => r.projectId === projectId);
  const [chats, setChats] = useState<Chat[]>([]);
  const [providers, setProviders] = useState<AgentProviderInfo[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(() => localStorage.getItem(dockChatKey(projectId)));

  useEffect(() => {
    agentChatsApi
      .providers()
      .then((r) => setProviders((r.providers ?? []).filter((p) => p.available && p.supports_project)))
      .catch(() => setProviders([]));
  }, []);

  const reload = () =>
    agentChatsApi
      .list()
      .then((list) => setChats(list.filter((c) => c.kind === "project" && c.project_id === projectId)))
      .catch(() => {});

  useEffect(() => {
    setSelectedId(localStorage.getItem(dockChatKey(projectId)));
    reload();
    const id = setInterval(reload, POLL_MS);
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId]);

  const select = (id: string | null) => {
    setSelectedId(id);
    if (id) localStorage.setItem(dockChatKey(projectId), id);
    else localStorage.removeItem(dockChatKey(projectId));
  };

  const openChats = chats.filter((c) => !c.done);
  // The remembered chat, else the newest open one (the list comes newest first).
  const chat = chats.find((c) => c.id === selectedId) ?? openChats[0] ?? null;
  const busy = chats.find((c) => c.status === "awaiting_approval") ?? null;

  const createChat = async (provider: AgentProviderInfo) => {
    try {
      const created = await createAgentChat("project", projectId, provider);
      setChats((cs) => [created, ...cs]);
      select(created.id);
    } catch (err) {
      alert(err instanceof Error ? err.message : String(err));
    }
  };

  const origin = () => {
    const { dashboardId, navStack } = useProjectStore.getState();
    const where =
      view === "grid" && dashboardId
        ? `the Grid, inside sub-dashboard "${navStack[navStack.length - 1]?.name ?? ""}" (dashboard_id=${dashboardId})`
        : `the ${VIEW_NAMES[view]}`;
    return `[Sent from the in-page agent panel while the person was on ${where} of project "${projectName ?? ""}" (project_id=${projectId}).]`;
  };

  if (picking) {
    return (
      <div className="agent-dock-pickbar">
        <span>🎯 {t("agentDock.pickHint")}</span>
        <button onClick={() => setPicking(false)}>{t("common.cancel")}</button>
      </div>
    );
  }

  if (!open) {
    const status = busy?.status ?? chat?.status;
    return (
      <button className="agent-dock-bubble" title={t("agentDock.open")} onClick={() => setOpen(true)}>
        💬
        {status && SIGNAL.has(status) && <span className={cx("agent-status-dot", `agent-status-${status}`)} />}
        {refs.length > 0 && <span className="agent-dock-badge">{refs.length}</span>}
      </button>
    );
  }

  return (
    <div className="agent-dock-sheet">
      <div className="agent-dock-bar">
        <select value={chat?.id ?? ""} onChange={(e) => select(e.target.value || null)}>
          {!chat && <option value="">{t("agentDock.noChat")}</option>}
          {chats
            .filter((c) => !c.done || c.id === chat?.id)
            .map((c) => (
              <option key={c.id} value={c.id}>
                {c.title || t("agent.untitled")} · {statusLabel(t, c.status)}
              </option>
            ))}
        </select>
        <span className="agent-provider-buttons">
          {providers.map((p) => (
            <button key={p.id} onClick={() => createChat(p)} title={t("agent.newProjectChatWith", { provider: p.label })}>
              <AgentProviderIcon provider={p.id} />
            </button>
          ))}
        </span>
        {chat && (
          <button
            title={t("agentDock.expand")}
            onClick={() => {
              rememberAgentChat(chat.id);
              setOpen(false);
              onExpand();
            }}
          >
            ⤢
          </button>
        )}
        <button title={t("agentDock.close")} onClick={() => setOpen(false)}>
          ✕
        </button>
      </div>
      {chat ? (
        <div className="agent-dock-chat">
          <ChatView
            key={chat.id}
            chat={chat}
            provider={providers.find((p) => p.id === (chat.provider ?? "claude"))}
            onBack={() => setOpen(false)}
            onChanged={reload}
            onSetDone={async (done) => {
              await agentChatsApi.setDone(chat.id, done);
              reload();
            }}
            onOpenChat={select}
            dock={{
              origin,
              refs,
              onRemoveRef: removeRef,
              onSent: () => clearRefs(projectId),
              // Picking on the board or in the doc would have nothing to tap.
              onPick: () => (view === "grid" ? setPicking(true) : alert(t("agentDock.pickGridOnly"))),
            }}
          />
        </div>
      ) : (
        <div className="agent-empty">{t("agentDock.createHint")}</div>
      )}
    </div>
  );
}
