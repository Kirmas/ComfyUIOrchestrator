"""A stand-in for comfy-agent-runner, for testing native.agent_task without
spending a real agent's tokens.

    backend/.venv/bin/python scripts/fake_agent_runner.py PORT API_URL TOKEN

Speaks just enough of the runner's HTTP surface (create chat, post message, read
chat, stop, delete) and, when it gets the task prompt, *plays the agent*: it uses
the real MCP protocol against the orchestrator, exactly as a project-chat agent
would (view_asset, scene_*, finish_task), so the tool wiring is exercised too.

What it does is chosen by a marker in the task's instruction text:
    [ok]      scale the mesh, export it, finish_task with the export   (default)
    [input]   finish_task with the INPUT asset itself (must never be deleted)
    [fail]    finish_task with no assets and a reason
    [silent]  do nothing (the turn simply ends)
    [slow]    keep "working" for two minutes (for testing cancel)
GET /_log returns everything it has seen.
"""
import asyncio
import json
import re
import sys
import uuid

import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

PORT, API_URL, TOKEN = int(sys.argv[1]), sys.argv[2].rstrip("/"), sys.argv[3]
CHATS: dict[str, dict] = {}
LOG: list[dict] = []


def text_of(result) -> str:
    return "".join(b.text for b in result.content if getattr(b, "type", "") == "text")


async def play_agent(chat_id: str, prompt: str) -> None:
    chat = CHATS[chat_id]
    try:
        scenario = next((m for m in ("[input]", "[fail]", "[silent]", "[slow]") if m in prompt), "[ok]")
        chat["scenario"] = scenario
        if scenario == "[silent]":
            return
        if scenario == "[slow]":
            await asyncio.sleep(120)
            return
        ids = re.findall(r"asset_id=([0-9a-f-]{36})\s+\((\w+),", prompt)
        task_id = re.search(r'finish_task\(task_id="([^"]+)"', prompt).group(1)
        async with streamablehttp_client(f"{API_URL}/mcp/", headers={"Authorization": f"Bearer {TOKEN}"}) as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize()
                mesh = next(a for a, kind in ids if kind == "mesh")
                for asset_id, kind in ids:
                    if kind in ("image", "mask"):
                        seen = await s.call_tool("view_asset", {"asset_id": asset_id})
                        chat["viewed_image"] = any(b.type == "image" for b in seen.content)
                if scenario == "[fail]":
                    await s.call_tool("finish_task", {"task_id": task_id, "asset_ids": [], "message": "I cannot do this: the mesh has no head"})
                    return
                if scenario == "[input]":
                    await s.call_tool("finish_task", {"task_id": task_id, "asset_ids": [mesh], "message": "no change needed"})
                    return
                created = json.loads(text_of(await s.call_tool("scene_create", {"asset_ids": [mesh], "name": "fake agent"})))
                sid = created["id"]
                ran = json.loads(text_of(await s.call_tool("scene_run", {"scene_id": sid, "code": "meshes()[0].scale = (1.5, 1, 1)"})))
                assert ran["ok"], ran
                shot = await s.call_tool("scene_render", {"scene_id": sid, "views": ["front", "three_quarter"], "size": 256})
                chat["saw_render"] = any(b.type == "image" for b in shot.content)
                exported = json.loads(text_of(await s.call_tool("scene_export", {"scene_id": sid, "format": "glb", "name": "wide"})))
                await s.call_tool("scene_destroy", {"scene_id": sid})
                done = await s.call_tool("finish_task", {"task_id": task_id, "asset_ids": [exported["id"]], "message": "made it 50% wider"})
                chat["finish_reply"] = text_of(done)
    except Exception as exc:  # the test reads this to see why the agent failed
        chat["agent_error"] = repr(exc)
    finally:
        chat["status"] = "idle"


async def create_chat(request):
    body = await request.json()
    chat_id = uuid.uuid4().hex[:12]
    CHATS[chat_id] = {"id": chat_id, "status": "idle", "create": body, "messages": [], "stops": 0}
    return JSONResponse({"id": chat_id, "status": "idle", "kind": body.get("kind")})


async def post_message(request):
    chat = CHATS.get(request.path_params["chat_id"])
    if not chat:
        return JSONResponse({"detail": "Chat not found"}, status_code=404)
    text = (await request.json()).get("text", "")
    chat["messages"].append(text)
    chat["status"] = "running"
    chat["task"] = asyncio.create_task(play_agent(chat["id"], text)) if len(chat["messages"]) == 1 else asyncio.create_task(_turn_ends(chat))
    return JSONResponse({"id": chat["id"], "status": "running"}, status_code=202)


async def _turn_ends(chat):  # a nudge: the agent stays silent again
    await asyncio.sleep(0.2)
    chat["status"] = "idle"


async def read_chat(request):
    chat = CHATS.get(request.path_params["chat_id"])
    return JSONResponse({"id": chat["id"], "status": chat["status"]} if chat else {"detail": "Chat not found"}, status_code=200 if chat else 404)


async def stop_chat(request):
    chat = CHATS.get(request.path_params["chat_id"])
    if chat:
        chat["stops"] += 1
        chat["status"] = "stopped"
        if chat.get("task"):
            chat["task"].cancel()
    return JSONResponse({"status": "stopped"})


async def delete_chat(request):
    CHATS.pop(request.path_params["chat_id"], None)
    return JSONResponse({"ok": True})


async def log(request):
    return JSONResponse({k: {kk: vv for kk, vv in v.items() if kk != "task"} for k, v in CHATS.items()})


app = Starlette(routes=[
    Route("/chats", create_chat, methods=["POST"]),
    Route("/chats/{chat_id}", read_chat, methods=["GET"]),
    Route("/chats/{chat_id}", delete_chat, methods=["DELETE"]),
    Route("/chats/{chat_id}/messages", post_message, methods=["POST"]),
    Route("/chats/{chat_id}/stop", stop_chat, methods=["POST"]),
    Route("/_log", log),
])

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
