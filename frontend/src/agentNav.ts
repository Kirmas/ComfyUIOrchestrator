/** Jumping from a grid cell to an agent's chat in the Agent tab.
 *
 * The tab remembers which chat it had open in localStorage and reads that when
 * it mounts, so "open this chat" is: write the id, ask App to switch view. A
 * window event rather than a prop, because the gesture lives in NodeCell, deep
 * inside Grid, and the view switch lives in App. */
export const LAST_AGENT_CHAT_KEY = "comfy-orchestrator:lastAgentChat";
export const OPEN_AGENT_EVENT = "comfy-orchestrator:open-agent";

export function openAgentChat(chatId: string): void {
  localStorage.setItem(LAST_AGENT_CHAT_KEY, chatId);
  window.dispatchEvent(new Event(OPEN_AGENT_EVENT));
}
