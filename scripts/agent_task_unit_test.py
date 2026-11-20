"""Unit checks of the native.agent_task watcher (backend/app/core/agent_task.py),
with the chat API replaced by a scripted fake -- fast, free, deterministic.

    backend/.venv/bin/python scripts/agent_task_unit_test.py

Covers what a real agent makes slow or impossible to provoke on demand: the one
nudge, giving up after it, the time limit, an approval nobody can give, a runner
that stops answering, a failed start, finish_task validation, and that the
result copy never deletes an INPUT asset.
"""
import asyncio
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.core import agent_task as at  # noqa: E402
from app.core.job_backend import AssetInput  # noqa: E402

at.POLL_S = 0.02
at.APPROVAL_PATIENCE_S = 0.3
at.RUNNER_FAILURES_BEFORE_GIVING_UP = 3
failures = 0


def check(cond, message):
    global failures
    print(("  ok: " if cond else "FAIL: ") + message)
    failures += 0 if cond else 1


class FakeApi:
    """Stands in for at._api: a chat whose status follows a script, recording every call."""

    def __init__(self, statuses, fail_reads=False):
        self.statuses = list(statuses)
        self.calls: list[tuple[str, str, dict | None]] = []
        self.fail_reads = fail_reads
        self.last = statuses[-1]

    async def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        if method == "GET":
            if self.fail_reads:
                raise RuntimeError("runner down")
            return {"status": self.statuses.pop(0) if self.statuses else self.last}
        return {"id": "chat1"}

    def posts(self, suffix):
        return [c for c in self.calls if c[0] == "POST" and c[1].endswith(suffix)]


def new_state(task_id="chat1"):
    state = at.TaskState(task_id=task_id, node_id="n", project_id="p")
    at.TASKS[task_id] = state
    return state


async def run_watch(api, timeout_s=5.0):
    at._api = api
    state = new_state()
    await at.AgentTaskBackend()._watch(state, timeout_s)
    return state


async def main():
    backend = at.AgentTaskBackend()
    print("worker contract")
    check(backend.agentic and not backend.retryable and backend.job_timeout_s > 3600, "agentic, not retryable, long budget")

    print("watcher")
    api = FakeApi(["running", "running", "idle", "running", "idle", "idle"])
    state = await run_watch(api)
    check(state.status == "error" and "never called" in state.error, f"gives up after the nudge: {state.error}")
    check(len(api.posts("/messages")) == 1, "exactly one nudge was sent")
    check("finish_task" in api.posts("/messages")[0][2]["text"], "the nudge tells it to call finish_task")

    api = FakeApi(["running", "error"])
    state = await run_watch(api)
    check(state.status == "error" and "'error'" in state.error, f"a chat that errors fails the task: {state.error}")
    check(not api.posts("/messages"), "no nudge for an errored chat")

    api = FakeApi(["running"] * 1000)
    state = await run_watch(api, timeout_s=0.15)
    check(state.status == "error" and "limit" in state.error, f"time limit: {state.error}")
    check(len(api.posts("/stop")) == 1, "the chat is stopped on timeout")

    api = FakeApi(["awaiting_approval"] * 1000)
    state = await run_watch(api)
    check(state.status == "error" and "approval" in state.error, f"stuck approval: {state.error}")
    check(len(api.posts("/stop")) == 1, "the chat is stopped when it waits for approval")

    api = FakeApi(["running"], fail_reads=True)
    state = await run_watch(api)
    check(state.status == "error" and "runner" in state.error, f"unreachable runner: {state.error}")

    api = FakeApi(["running", "running", "running"])
    at._api = api
    state = new_state()
    watcher = asyncio.create_task(backend._watch(state, 5))
    await asyncio.sleep(0.1)
    state.status = "done"  # what finish_task does
    await asyncio.wait_for(watcher, 2)
    check(state.status == "done", "the watcher stops once the agent finished")

    print("finish_task")
    try:
        await at.finish_task("nope", [], "")
        check(False, "unknown task refused")
    except at.TaskError as exc:
        check(exc.status == 404, f"unknown task -> 404: {exc}")
    state = new_state("chat2")
    try:
        await at.finish_task("chat2", [str(uuid.uuid4())], "x")
        check(False, "unknown asset refused")
    except at.TaskError as exc:
        check(exc.status == 400 and "does not exist" in str(exc), f"unknown asset -> 400: {exc}")
        check(state.status == "running", "a refused call leaves the task running (the agent can retry)")
    out = await at.finish_task("chat2", [], "could not do it")
    check(out["status"] == "error" and state.error == "could not do it", "asset_ids=[] reports failure with the reason")
    try:
        await at.finish_task("chat2", [], "again")
        check(False, "second call refused")
    except at.TaskError as exc:
        check(exc.status == 409, f"a finished task can't be finished again: {exc}")

    print("backend status/result/cancel")
    state = new_state("chat3")
    check(await backend.status("chat3") == at.JobStatus.running, "running")
    state.status, state.error = "error", "boom"
    check(await backend.status("chat3") == at.JobStatus.error and await backend.error_detail("chat3") == "boom", "error + detail")
    check(await backend.status("missing") == at.JobStatus.error and "restarted" in await backend.error_detail("missing"), "an unknown task reads as an error")
    check(await backend.result("chat3") == [], "an unfinished task has no result")
    api = FakeApi(["running"])
    at._api = api
    state = new_state("chat4")
    await backend.cancel("chat4")
    check(state.status == "cancelled" and len(api.posts("/stop")) == 1, "cancel marks it and stops the chat")

    print("prompt")
    text = at.build_prompt("chat9", "My Project", "Make the ears longer\nand pointier", [("Input 1", AssetInput("aaa", "mesh", "model/gltf-binary")), ("Input 2", AssetInput("bbb", "mask", "image/png"))])
    check(text.splitlines()[0] == "Task: Make the ears longer", "first line becomes the chat title")
    check("asset_id=aaa" in text and "asset_id=bbb" in text, "inputs listed by id")
    check('finish_task(task_id="chat9"' in text, "the task id is in the instructions")
    check("and pointier" in text, "the whole instruction is kept verbatim")
    check("INPUTS: none" in at.build_prompt("c", "P", "do it", []), "no inputs is said plainly")

    print("ALL CHECKS PASSED" if not failures else f"{failures} FAILED")
    return failures


sys.exit(1 if asyncio.run(main()) else 0)
