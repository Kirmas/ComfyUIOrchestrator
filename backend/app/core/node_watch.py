"""Agent subscriptions to a node finishing -- the alternative to polling.

An agent that starts a generation can leave its chat id here; when the node
reaches `done`/`error` the agent's own chat gets one message. The runner queues
a message sent to a busy chat and delivers it as the next turn, so there is
nothing to retry and no 409. State is in memory (like agent_task.TASKS): a
deploy forgets subscriptions, the same "manual re-roll" rule as the jobs.
"""
import logging

from app.core.agent_task import _api  # the app's own REST API, in-process

log = logging.getLogger(__name__)

WATCHES: dict[str, set[str]] = {}  # node_id -> chat ids


def watch(node_ids: list[str], chat_id: str) -> None:
    for node_id in node_ids:
        WATCHES.setdefault(str(node_id), set()).add(chat_id)


async def notify_finished(node_id, status: str, error: str | None) -> None:
    """Called when a node reaches a terminal state; no-op without watchers."""
    chats = WATCHES.pop(str(node_id), None)
    if not chats:
        return
    text = f"[orchestrator] Node {node_id} finished: {status}."
    if error:
        text += f" Reason: {error}"
    text += " (You subscribed to this node; no need to poll.)"
    for chat_id in chats:
        try:
            await _api("POST", f"/api/agent-chats/{chat_id}/messages", {"text": text})
        except Exception:  # noqa: BLE001 -- a dead chat must not break the worker
            log.warning("could not notify chat %s about node %s", chat_id, node_id, exc_info=True)
