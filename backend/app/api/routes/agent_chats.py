"""Agent chats: a thin proxy to the agent runner (agent_runner/runner.py).

The runner is a separate process on purpose -- a chat has to outlive this
service restarting (every deploy restarts it). So nothing about a chat lives
here: this module only authenticates the browser (the usual API token, via
auth_middleware), fills in what the runner can't know on its own (for a
project chat: the project's name, this instance's MCP URL and token), and
relays. Which kinds of chat exist ("project", and "dev" when the runner has it
enabled) and what each may do is the runner's business, not this module's.

The event stream is the one piece worth a note: it is Server-Sent Events end
to end, keyed by the runner's per-chat sequence number. When this service
restarts, the browser's EventSource reconnects on its own and sends
Last-Event-ID, which is forwarded, so the transcript resumes exactly where it
stopped while the agent kept working in the runner the whole time.
"""

import asyncio
import io
import re
import uuid
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from starlette.datastructures import Headers, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.api.routes.assets import asset_download_name
from app.api.routes.nodes import upload_asset_to_node
from app.db.base import get_db
from app.core.storage import get_storage
from app.core.asset_preview import build_fit
from app.db.models import Asset, Project

router = APIRouter(prefix="/api/agent-chats", tags=["agent-chats"])


class AgentChatCreate(BaseModel):
    kind: str = "project"
    # Project chats only; a dev chat works on the codebase, not on a project.
    project_id: uuid.UUID | None = None
    # None = the runner's default. The runner owns the lists and validates.
    model: str | None = None
    permission_mode: str | None = None  # dev chats only
    # Which agent CLI answers (claude | codex | agy); None = the runner's default.
    provider: str | None = None
    effort: str | None = None  # the runner validates against the provider's list
    fast_mode: bool | None = None


class AgentChatUpdate(BaseModel):
    model: str | None = None
    permission_mode: str | None = None
    effort: str | None = None  # "" = the CLI's own default
    fast_mode: bool | None = None
    done: bool | None = None  # hidden from the list unless "show done" is on


class PermissionDecision(BaseModel):
    decision: str  # allow | allow_session | deny -- the runner validates


class QuestionAnswer(BaseModel):
    answers: dict[str, str]  # question text -> answer; the runner validates


class HandoffCreate(BaseModel):
    target: str
    task: str = Field(max_length=4000)
    reason: str = Field(max_length=300)
    done_so_far: str = Field(default="", max_length=2000)
    focus: list[str] = Field(default_factory=list, max_length=20)


class HandoffDecision(BaseModel):
    decision: str  # allow_once | allow_always | deny_once | deny_always


class HandoffRulesUpdate(BaseModel):
    rules: dict[str, str] = Field(default_factory=dict)


class AgentImage(BaseModel):
    media_type: str
    data: str  # base64; the runner validates type, size and which chats take them


class AgentMediaAttach(BaseModel):
    asset_id: uuid.UUID
    caption: str = Field(default="", max_length=1000)


class ChatAttachmentImport(BaseModel):
    node_id: uuid.UUID
    name: str = Field(pattern=r"^[0-9a-f]{32}\.(png|jpg|gif|webp)$")


class AgentMessage(BaseModel):
    text: str = ""
    images: list[AgentImage] = []


def _runner() -> httpx.AsyncClient:
    settings = get_settings()
    return httpx.AsyncClient(
        base_url=settings.agent_runner_url,
        headers={"Authorization": f"Bearer {settings.api_token}"},
        timeout=httpx.Timeout(30.0),
    )


async def _call(method: str, path: str, **kwargs) -> Response:
    try:
        async with _runner() as client:
            res = await client.request(method, path, **kwargs)
    except httpx.TransportError as exc:
        raise HTTPException(503, f"Agent runner is not reachable ({exc.__class__.__name__}). Is the comfy-agent-runner user service running?")
    return Response(content=res.content, status_code=res.status_code, media_type=res.headers.get("content-type"))


@router.get("")
async def list_chats(project_id: uuid.UUID | None = None):
    return await _call("GET", "/chats", params={"project_id": str(project_id)} if project_id else None)


# Declared before "/{chat_id}" so "models"/"kinds" aren't taken for a chat id.
@router.get("/models")
async def list_models():
    return await _call("GET", "/models")


@router.get("/kinds")
async def list_kinds():
    """Which kinds of chat this runner offers -- "dev" only when enabled there."""
    return await _call("GET", "/kinds")


@router.get("/limits")
async def get_limits():
    """The last usage-limit reading per provider (session / weekly windows)."""
    return await _call("GET", "/limits")


@router.post("/limits/{provider}")
async def refresh_limits(provider: str):
    """Take a fresh reading now: a tiny turn on that CLI, a few seconds."""
    return await _call("POST", f"/limits/{provider}", timeout=httpx.Timeout(120.0))


@router.get("/handoff-rules")
async def get_handoff_rules():
    return await _call("GET", "/handoff/rules")


@router.put("/handoff-rules")
async def update_handoff_rules(payload: HandoffRulesUpdate):
    return await _call("PUT", "/handoff/rules", json=payload.model_dump())


@router.post("")
async def create_chat(payload: AgentChatCreate, db: AsyncSession = Depends(get_db)):
    # api_url/api_token (this instance's own, same as the night self-check
    # below) let the runner call back POST /api/push/notify when a turn
    # finishes -- see runner.py's Chat.set_status.
    body: dict = {
        "kind": payload.kind,
        "provider": payload.provider,
        "model": payload.model,
        "effort": payload.effort,
        "fast_mode": payload.fast_mode,
        "permission_mode": payload.permission_mode,
        **_self_check(),
    }
    if payload.kind == "project":
        if not payload.project_id:
            raise HTTPException(400, "A project chat needs project_id")
        project = await db.get(Project, payload.project_id)
        if not project:
            raise HTTPException(404, "Project not found")
        settings = get_settings()
        body |= {
            "project_id": str(project.id),
            "project_name": project.name,
            "mcp_url": settings.agent_mcp_url,
            "mcp_token": settings.api_token,
        }
    # Any other kind goes through as-is: the runner owns the list of kinds
    # and refuses what it doesn't offer (e.g. "dev" when not enabled there).
    return await _call("POST", "/chats", json=body)


@router.get("/{chat_id}")
async def get_chat(chat_id: str):
    return await _call("GET", f"/chats/{chat_id}")


@router.patch("/{chat_id}")
async def update_chat(chat_id: str, payload: AgentChatUpdate):
    return await _call("PATCH", f"/chats/{chat_id}", json=payload.model_dump(exclude_none=True))


@router.delete("/{chat_id}")
async def delete_chat(chat_id: str):
    return await _call("DELETE", f"/chats/{chat_id}")


@router.post("/{chat_id}/messages")
async def post_message(chat_id: str, payload: AgentMessage):
    return await _call("POST", f"/chats/{chat_id}/messages", json=payload.model_dump())


@router.get("/{chat_id}/attachments/{name}")
async def get_attachment(chat_id: str, name: str, preview: bool = False, download: bool = False):
    """A picture sent in a message, for the transcript (<img>, so the token
    comes as ?token=, which auth_middleware accepts)."""
    res = await _call("GET", f"/chats/{chat_id}/attachments/{name}")
    if res.status_code == 200:
        if preview and not download and res.headers.get("content-type", "").startswith("image/"):
            fitted = await asyncio.to_thread(build_fit, res.body, 768)
            if fitted is not None:
                res = Response(fitted, media_type="image/webp")
        res.headers["Cache-Control"] = "private, max-age=31536000, immutable"
        if download:
            res.headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(name, safe='')}"
    return res


@router.get("/{chat_id}/attachments")
async def list_chat_attachments(chat_id: str):
    if not re.fullmatch(r"[0-9a-f]{12}|[0-9a-fA-F-]{36}", chat_id):
        raise HTTPException(400, "Invalid chat id or session UUID")
    return await _call("GET", f"/chats/{chat_id}/attachments")


@router.post("/{chat_id}/import-attachment", status_code=201)
async def import_chat_attachment(chat_id: str, payload: ChatAttachmentImport, db: AsyncSession = Depends(get_db)):
    """Runner reads its private file; reuse the ordinary node upload rules.

    Never accept local paths or URLs, and never request a preview rendition.
    """
    if not re.fullmatch(r"[0-9a-f]{12}|[0-9a-fA-F-]{36}", chat_id):
        raise HTTPException(400, "Invalid chat id or session UUID")
    res = await _call("GET", f"/chats/{chat_id}/attachments/{payload.name}")
    if res.status_code != 200:
        return res
    file = UploadFile(filename=payload.name, file=io.BytesIO(res.body),
                      headers=Headers({"content-type": res.headers.get("content-type", "application/octet-stream")}))
    try:
        return await upload_asset_to_node(payload.node_id, file, kind=None, db=db)
    finally:
        await file.close()


@router.post("/{chat_id}/media")
async def attach_media(chat_id: str, payload: AgentMediaAttach, db: AsyncSession = Depends(get_db)):
    """Show an existing asset in the Agent transcript, for any provider.

    Only asset ids/metadata cross the runner boundary; files remain in storage
    and the browser uses normal authenticated asset routes, including ranges.
    """
    if not re.fullmatch(r"[0-9a-f]{12}|[0-9a-fA-F-]{36}", chat_id):
        raise HTTPException(400, "Invalid chat id or session UUID")
    asset = await db.get(Asset, payload.asset_id)
    if not asset:
        raise HTTPException(404, "Asset not found")
    if not get_storage().path_of(asset.storage_key).is_file():
        raise HTTPException(404, "Asset file missing")
    name = asset_download_name(asset)
    return await _call("POST", f"/chats/{chat_id}/media", json={
        "asset_id": str(asset.id), "mime_type": asset.mime_type,
        "name": name, "caption": payload.caption,
    })


@router.post("/{chat_id}/stop")
async def stop_chat(chat_id: str):
    return await _call("POST", f"/chats/{chat_id}/stop")


@router.post("/{chat_id}/permissions/{request_id}")
async def decide_permission(chat_id: str, request_id: str, payload: PermissionDecision):
    return await _call("POST", f"/chats/{chat_id}/permissions/{request_id}", json={"decision": payload.decision})


@router.post("/{chat_id}/questions/{request_id}")
async def answer_question(chat_id: str, request_id: str, payload: QuestionAnswer):
    return await _call("POST", f"/chats/{chat_id}/questions/{request_id}", json={"answers": payload.answers})


@router.get("/{chat_id}/handoff-targets")
async def get_handoff_targets(chat_id: str):
    if not re.fullmatch(r"[0-9a-f]{12}|[0-9a-fA-F-]{36}", chat_id):
        raise HTTPException(400, "Invalid chat id or session UUID")
    return await _call("GET", f"/chats/{chat_id}/handoff-targets")


@router.post("/{chat_id}/handoff")
async def request_handoff(chat_id: str, payload: HandoffCreate):
    if not re.fullmatch(r"[0-9a-f]{12}|[0-9a-fA-F-]{36}", chat_id):
        raise HTTPException(400, "Invalid chat id or session UUID")
    return await _call("POST", f"/chats/{chat_id}/handoff", json=payload.model_dump())


@router.post("/{chat_id}/handoffs/{request_id}")
async def decide_handoff(chat_id: str, request_id: str, payload: HandoffDecision):
    if not re.fullmatch(r"[0-9a-f]{12}|[0-9a-fA-F-]{36}", chat_id):
        raise HTTPException(400, "Invalid chat id or session UUID")
    return await _call("POST", f"/chats/{chat_id}/handoffs/{request_id}", json=payload.model_dump())


# --- night mode (agent_runner/night.py) -------------------------------------
# Bug reports from agents (the MCP report_bug tool) and the morning decision.
# All of it happens in the runner; this only proxies, and adds what the runner
# needs to health-check this instance after a deploy: its URL and token.

reports_router = APIRouter(prefix="/api/agent-reports", tags=["agent-chats"])
night_router = APIRouter(prefix="/api/agent-night", tags=["agent-chats"])


class BugReport(BaseModel):
    summary: str
    details: str


class NightUpdate(BaseModel):
    accept_reports: bool


def _self_check() -> dict:
    settings = get_settings()
    return {"api_url": settings.agent_mcp_url.split("/mcp")[0], "api_token": settings.api_token}


@reports_router.post("")
async def create_report(payload: BugReport):
    return await _call("POST", "/reports", json={**payload.model_dump(), **_self_check()})


@reports_router.get("/{report_id}")
async def get_report(report_id: str):
    return await _call("GET", f"/reports/{report_id}")


@night_router.get("")
async def night_status():
    return await _call("GET", "/night")


@night_router.patch("")
async def night_update(payload: NightUpdate):
    return await _call("PATCH", "/night", json=payload.model_dump())


@night_router.get("/diff")
async def night_diff():
    return await _call("GET", "/night/diff")


@night_router.post("/{action}")
async def night_finish(action: str):
    """accept | reject -- the morning decision for the whole night session.
    Can take minutes on reject (it redeploys), hence the long timeout."""
    try:
        async with httpx.AsyncClient(
            base_url=get_settings().agent_runner_url,
            headers={"Authorization": f"Bearer {get_settings().api_token}"},
            timeout=httpx.Timeout(2400.0),
        ) as client:
            res = await client.post(f"/night/{action}", json=_self_check())
    except httpx.TransportError as exc:
        raise HTTPException(503, f"Agent runner is not reachable ({exc.__class__.__name__})")
    return Response(content=res.content, status_code=res.status_code, media_type=res.headers.get("content-type"))


@router.get("/{chat_id}/events")
async def chat_events(chat_id: str, request: Request):
    settings = get_settings()
    headers = {"Authorization": f"Bearer {settings.api_token}"}
    last_id = request.headers.get("last-event-id")
    if last_id:
        headers["Last-Event-ID"] = last_id
    client = httpx.AsyncClient(base_url=settings.agent_runner_url, timeout=httpx.Timeout(10.0, read=None))
    try:
        # Forward `after` only -- never the query-string token the browser
        # authenticated with.
        params = {"after": after} if (after := request.query_params.get("after")) else None
        upstream = await client.send(client.build_request("GET", f"/chats/{chat_id}/events", headers=headers, params=params), stream=True)
    except httpx.TransportError:
        await client.aclose()
        # EventSource treats a non-200 as fatal and stops retrying; the page
        # shows the runner as down and offers a manual reconnect instead.
        raise HTTPException(503, "Agent runner is not reachable")
    if upstream.status_code != 200:
        body = await upstream.aread()
        await upstream.aclose()
        await client.aclose()
        return Response(content=body, status_code=upstream.status_code, media_type=upstream.headers.get("content-type"))

    async def relay():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        except httpx.TransportError:
            # Runner went away mid-stream; ending the response makes the
            # browser reconnect with its Last-Event-ID.
            pass
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(relay(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
