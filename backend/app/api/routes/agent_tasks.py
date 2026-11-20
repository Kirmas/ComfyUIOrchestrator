"""Where an agent working a native.agent_task node hands its result in
(core/agent_task.py). One endpoint; the MCP tool `finish_task` is a thin call
to it, like every other MCP tool."""
import uuid

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.core.agent_task import TaskError, finish_task

router = APIRouter(prefix="/api/agent-tasks", tags=["agent-tasks"])


class TaskResult(BaseModel):
    asset_ids: list[uuid.UUID] = Field(default_factory=list, max_length=16)
    message: str = Field(default="", max_length=4000)


@router.post("/{task_id}/result")
async def submit_result(task_id: str, body: TaskResult) -> dict:
    try:
        return await finish_task(task_id, [str(a) for a in body.asset_ids], body.message)
    except TaskError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
