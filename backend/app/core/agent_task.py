"""native.agent_task: a node that hands a job to an agent instead of running a
model -- an alternative to typing the same request into a chat.

All inputs are optional and of any kind; the person writes what to do; running
the node opens an ordinary *project chat* (so it shows up in the Agent tab and
can be followed or stopped there), gives it the task and the input assets'
ids, and waits. Whatever the agent passes to the `finish_task` MCP tool
becomes the node's output. The node never talks to Blender (or anything else)
itself: it is only the agent's entry point from the grid, and the agent uses
the same MCP tools any project chat has (live Blender scenes, view_asset, ...).

How it plugs into the job machinery (worker/tasks.py run_variant_job):
  * `agentic = True` -> its slots arrive as `AssetInput` (ids), not bytes, plus
    a `_task` dict saying which node/project this is for;
  * submit() returns quickly with the CHAT ID as the external job id (so the
    page can link a running node to its chat) and a background watcher carries
    on; the worker just polls status();
  * `retryable = False`: the worker's automatic retries would start a second
    agent on the same job and spend tokens twice;
  * `job_timeout_s` is far above the worker's 10 minute network budget; the
    node's own "time limit" param is what actually bounds a run.

State is in memory (TASKS), like core/native_backend.py's results: a deploy
mid-task orphans the job (worker/tasks.py recover_orphaned_jobs) but not the
chat -- the runner is a separate service and the agent keeps working; its
finish_task call then answers "no running task" and says so. That matches the
app's existing rule that a restart mid-generation needs a manual re-roll.
"""
import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field

from app.core.blender import find_blender_binary
from app.core.job_backend import AssetInput, AssetRef, CapacityInfo, JobStatus
from app.core.storage import get_storage
from app.db.base import async_session_maker
from app.db.models import Asset, Project
from app.mcp.client import get_client, raise_for_api_error

logger = logging.getLogger(__name__)

INPUT_SLOTS = [f"input_{i}" for i in range(1, 7)]
POLL_S = 3
# One nudge, then give up: an agent that stops twice without finishing is not
# going to; more prompting would only spend more tokens.
MAX_NUDGES = 1
# A project chat is MCP-only, so nothing should ask for approval; if one does,
# nobody is there to answer.
APPROVAL_PATIENCE_S = 120
RUNNER_FAILURES_BEFORE_GIVING_UP = 5
STATE_KEEP_S = 24 * 3600


@dataclass
class TaskState:
    task_id: str  # == the chat id
    node_id: str
    project_id: str
    started: float = field(default_factory=time.monotonic)
    status: str = "running"  # running | done | error | cancelled
    asset_ids: list[str] = field(default_factory=list)
    message: str = ""
    error: str | None = None
    nudges: int = 0
    watcher: asyncio.Task | None = None


TASKS: dict[str, TaskState] = {}


class TaskError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


async def _api(method: str, path: str, body: dict | None = None) -> dict | None:
    """This app's own REST API, in-process (the same client the MCP tools use)."""
    async with get_client() as client:
        r = await client.request(method, path, json=body)
        raise_for_api_error(r)
        return r.json() if r.content else None


def _first_line(text: str, width: int = 70) -> str:
    line = text.strip().splitlines()[0] if text.strip() else "task"
    return line if len(line) <= width else line[: width - 1] + "…"


def build_prompt(task_id: str, project_name: str, instruction: str, inputs: list[tuple[str, AssetInput]]) -> str:
    lines = [
        f"Task: {_first_line(instruction)}",
        "",
        f"This is an unattended TASK started from a node in the project \"{project_name}\". Nobody is watching "
        "this chat live and nobody will answer questions: make sensible choices and carry on.",
        "",
        "WHAT TO DO (the person's own words):",
        instruction.strip(),
        "",
    ]
    if inputs:
        lines.append("INPUTS -- library assets you can use by asset_id:")
        for label, item in inputs:
            lines.append(f"- {label}: asset_id={item.asset_id}  ({item.kind}, {item.mime_type})")
        lines.append("")
    else:
        lines += ["INPUTS: none -- create the result from the description alone.", ""]
    lines += [
        "HOW TO WORK:",
        "- To look at an input picture or mask: view_asset(asset_id). Judge your work by looking, not by guessing.",
    ]
    if find_blender_binary() is not None:
        lines += [
            "- For 3D (meshes, rigs, animation): call scene_help once, then scene_create(asset_ids=[...]) and work "
            "with scene_run / scene_render / scene_info. LOOK at a scene_render after every real change. Export "
            "each result with scene_export (it returns an asset id) and scene_destroy your scenes when done.",
        ]
    lines += [
        "- Other orchestrator tools are available too, but do NOT place anything in the grid yourself: the "
        "node's output is exactly what you hand to finish_task.",
        "",
        "WHEN DONE -- required:",
        f'finish_task(task_id="{task_id}", asset_ids=[...], message="what you did, in a sentence or two").',
        "Pass one asset per distinct result; if there are several the person will pick between them. If you "
        "cannot complete the task, still call finish_task with asset_ids=[] and a message saying why -- "
        "do not just stop.",
    ]
    return "\n".join(lines)


async def _chat_meta(chat_id: str) -> dict | None:
    try:
        return await _api("GET", f"/api/agent-chats/{chat_id}")
    except Exception as exc:  # the runner being briefly unreachable is not the end of a task
        logger.warning("agent task %s: cannot read chat: %s", chat_id, exc)
        return None


class AgentTaskBackend:
    agentic = True
    retryable = False
    job_timeout_s = 6 * 3600

    async def submit(self, execution_config: dict, inputs: dict) -> str:
        ctx = inputs.get("_task") or {}
        instruction = (inputs.get("instruction") or "").strip()
        if not instruction:
            raise ValueError("write what the agent should do in the instruction field")
        labelled = [(f"Input {name.rsplit('_', 1)[1]}", inputs[name]) for name in INPUT_SLOTS if isinstance(inputs.get(name), AssetInput)]
        provider = (inputs.get("provider") or "claude").strip()
        model = (inputs.get("model") or "").strip() or None
        timeout_min = min(max(int(inputs.get("timeout_min") or 30), 2), 240)

        async with async_session_maker() as db:
            project = await db.get(Project, uuid.UUID(ctx["project_id"]))
        project_name = project.name if project else "?"

        chat = await _api("POST", "/api/agent-chats", {
            "kind": "project", "project_id": ctx["project_id"], "provider": provider, "model": model})
        chat_id = chat["id"]
        state = TaskState(task_id=chat_id, node_id=ctx.get("node_id", ""), project_id=ctx["project_id"])
        self._forget_old()
        TASKS[chat_id] = state
        try:
            await _api("POST", f"/api/agent-chats/{chat_id}/messages", {"text": build_prompt(chat_id, project_name, instruction, labelled)})
        except BaseException:  # incl. a cancel arriving mid-submit: don't leave an orphan chat behind
            TASKS.pop(chat_id, None)
            await self._try(_api("DELETE", f"/api/agent-chats/{chat_id}"))
            raise
        state.watcher = asyncio.create_task(self._watch(state, timeout_min * 60))
        logger.info("agent task %s started (%s) for node %s", chat_id, provider, state.node_id)
        return chat_id

    @staticmethod
    def _forget_old() -> None:
        cutoff = time.monotonic() - STATE_KEEP_S
        for key in [k for k, s in TASKS.items() if s.started < cutoff and s.status != "running"]:
            TASKS.pop(key, None)

    @staticmethod
    async def _try(coro) -> None:
        try:
            await coro
        except Exception as exc:
            logger.warning("agent task: best-effort call failed: %s", exc)

    async def _watch(self, state: TaskState, timeout_s: float) -> None:
        deadline = state.started + timeout_s
        approval_since: float | None = None
        runner_failures = 0
        try:
            while state.status == "running":
                await asyncio.sleep(POLL_S)
                if state.status != "running":
                    return
                if time.monotonic() > deadline:
                    await self._try(_api("POST", f"/api/agent-chats/{state.task_id}/stop"))
                    self._fail(state, f"the agent did not finish within the {timeout_s / 60:g} minute limit")
                    return
                meta = await _chat_meta(state.task_id)
                if meta is None:
                    runner_failures += 1
                    if runner_failures >= RUNNER_FAILURES_BEFORE_GIVING_UP:
                        self._fail(state, "the agent runner stopped answering")
                        return
                    continue
                runner_failures = 0
                status = meta.get("status")
                if status == "awaiting_approval":
                    approval_since = approval_since or time.monotonic()
                    if time.monotonic() - approval_since > APPROVAL_PATIENCE_S:
                        await self._try(_api("POST", f"/api/agent-chats/{state.task_id}/stop"))
                        self._fail(state, "the agent is waiting for an approval nobody can give in an unattended task")
                        return
                    continue
                approval_since = None
                if status in ("idle", "error", "stopped", "interrupted") and state.status == "running":
                    if status in ("error", "stopped", "interrupted"):
                        self._fail(state, f"the agent's chat ended as '{status}' without finishing the task")
                        return
                    if state.nudges < MAX_NUDGES:
                        state.nudges += 1
                        await self._try(_api("POST", f"/api/agent-chats/{state.task_id}/messages", {"text": (
                            f'You stopped without calling finish_task. Call it now: finish_task(task_id="{state.task_id}", '
                            "asset_ids=[the assets that are your result], message=\"...\") -- or with asset_ids=[] and "
                            "the reason if you could not do it.")}))
                        continue
                    self._fail(state, "the agent stopped without handing in a result (finish_task was never called)")
                    return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("agent task %s watcher failed", state.task_id)
            self._fail(state, f"internal error while waiting for the agent: {exc}")

    @staticmethod
    def _fail(state: TaskState, reason: str) -> None:
        if state.status == "running":
            state.status, state.error = "error", reason
            logger.warning("agent task %s failed: %s", state.task_id, reason)

    # ----------------------------------------------------------- JobBackend --
    async def status(self, job_id: str) -> JobStatus:
        state = TASKS.get(job_id)
        if state is None:
            return JobStatus.error
        return {"running": JobStatus.running, "done": JobStatus.done, "error": JobStatus.error,
                "cancelled": JobStatus.cancelled}[state.status]

    async def error_detail(self, job_id: str) -> str | None:
        state = TASKS.get(job_id)
        if state is None:
            return "the task is no longer known here (the server restarted); the agent's chat may still be open in the Agent tab"
        return state.error

    async def capacity(self) -> CapacityInfo:
        return CapacityInfo(is_alive=True, queue_length=0, max_queue_length=None)

    async def cancel(self, job_id: str) -> None:
        state = TASKS.get(job_id)
        if state is not None and state.status == "running":
            state.status = "cancelled"
            await self._try(_api("POST", f"/api/agent-chats/{job_id}/stop"))

    async def result(self, job_id: str) -> list[AssetRef]:
        """The agent's chosen assets, copied so the node owns them. The agent's own
        temporary exports (marked source=blender_scene) are deleted afterwards so
        nothing is left behind in the library; any other asset -- above all an
        INPUT the agent passed straight back -- is only ever copied, never deleted."""
        state = TASKS.get(job_id)
        if state is None or state.status != "done":
            return []
        storage = get_storage()
        refs: list[AssetRef] = []
        async with async_session_maker() as db:
            temporary = []
            for asset_id in state.asset_ids:
                asset = await db.get(Asset, uuid.UUID(asset_id))
                if asset is None:
                    continue
                data = await asyncio.to_thread(storage.get_object, asset.storage_key)
                meta = {"from_agent_task": job_id}
                if state.message:
                    meta["agent_note"] = state.message
                if (asset.meta or {}).get("filename"):
                    meta["filename"] = asset.meta["filename"]
                refs.append(AssetRef(data=data, mime_type=asset.mime_type, kind=str(getattr(asset.kind, "value", asset.kind)), meta=meta))
                if (asset.meta or {}).get("source") == "blender_scene" and asset.node_id is None:
                    temporary.append(asset)
            for asset in temporary:
                storage.delete_object(asset.storage_key)
                await db.delete(asset)
            await db.commit()
        TASKS.pop(job_id, None)
        return refs


async def finish_task(task_id: str, asset_ids: list[str], message: str) -> dict:
    """What the agent's finish_task call does. Raises TaskError for the agent to read."""
    state = TASKS.get(task_id)
    if state is None:
        raise TaskError("no running task with that id -- it may have finished, been cancelled, timed out, or the "
                        "server restarted", 404)
    if state.status != "running":
        raise TaskError(f"this task is already {state.status}", 409)
    async with async_session_maker() as db:
        for asset_id in asset_ids:
            if await db.get(Asset, uuid.UUID(asset_id)) is None:
                raise TaskError(f"asset {asset_id} does not exist -- pass the ids that scene_export returned")
    state.message = (message or "").strip()
    if asset_ids:
        state.asset_ids, state.status = list(dict.fromkeys(asset_ids)), "done"
    else:
        state.status, state.error = "error", state.message or "the agent reported that it could not complete the task"
    logger.info("agent task %s finished: %s (%d asset(s))", task_id, state.status, len(asset_ids))
    return {"task_id": task_id, "status": state.status, "accepted_assets": len(state.asset_ids)}
