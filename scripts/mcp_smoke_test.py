#!/usr/bin/env python
"""End-to-end check of the MCP server against a running orchestrator.

Drives a scratch project through the same tools an agent would use, then
deletes it. Run against the dev instance first:

    backend/.venv/bin/python scripts/mcp_smoke_test.py http://127.0.0.1:8011 dev-local-token

Against the live service, pass its URL and real token instead. It only ever
touches a project it creates itself.
"""
import asyncio
import base64
import json
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

# 1x1 red PNG.
PNG_1PX = base64.b64encode(
    base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )
).decode()

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((PASS if ok else FAIL, name, detail))
    print(f"  [{PASS if ok else FAIL}] {name}" + (f" -- {detail}" if detail else ""))
    return ok


def payload(result):
    """Unwrap a tool result.

    structuredContent is the reliable form; a tool whose return type isn't an
    object is wrapped as {"result": ...}. Text blocks are the fallback for
    tools that return content directly (get_candidates).
    """
    sc = result.structuredContent
    if sc is not None:
        return sc["result"] if isinstance(sc, dict) and set(sc) == {"result"} else sc
    for block in result.content:
        if block.type == "text":
            try:
                return json.loads(block.text)
            except json.JSONDecodeError:
                return block.text
    return None


async def main(base_url: str, token: str) -> int:
    url = base_url.rstrip("/") + "/mcp"
    headers = {"Authorization": f"Bearer {token}"}
    project_id = None

    async with streamablehttp_client(url, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            check("initialize handshake", True, init.serverInfo.name)

            tools = (await session.list_tools()).tools
            names = {t.name for t in tools}
            check("tools listed", len(tools) > 0, f"{len(tools)} tools")
            for required in ("create_node", "run_node", "await_node", "select_candidate", "flag_cell"):
                check(f"tool present: {required}", required in names)

            try:
                project = payload(await session.call_tool("create_project", {"name": "mcp-smoke-test"}))
                project_id = project["id"]
                check("create_project", bool(project_id), project_id)

                track = payload(await session.call_tool("create_track", {"project_id": project_id}))
                track_id = track["id"]
                check("create_track", bool(track_id))

                recipe = payload(await session.call_tool("get_project_recipe", {"project_id": project_id}))
                check(
                    "get_project_recipe shape",
                    all(k in recipe for k in ("tracks", "steps", "occupied", "spans", "blocked_cells")),
                    f"keys={sorted(recipe)}",
                )

                node = payload(
                    await session.call_tool(
                        "create_node",
                        {"track_id": track_id, "step_index": 0, "node_type": "asset.single", "kind": "asset"},
                    )
                )
                node_id = node["id"]
                check("create_node (asset cell)", bool(node_id))

                asset = payload(
                    await session.call_tool(
                        "upload_reference_image",
                        {"node_id": node_id, "image_base64": PNG_1PX, "filename": "smoke.png"},
                    )
                )
                check("upload_reference_image", bool(asset.get("id")))

                cands = await session.call_tool("get_candidates", {"node_id": node_id})
                has_image = any(b.type == "image" for b in cands.content)
                check("get_candidates returns image content", has_image)

                recipe2 = payload(await session.call_tool("get_project_recipe", {"project_id": project_id}))
                check("recipe reports the new node as occupied", [0, 0] in recipe2["occupied"], str(recipe2["occupied"]))

                flag = payload(await session.call_tool("flag_cell", {"node_id": node_id, "note": "smoke-test flag"}))
                check("flag_cell creates annotation", flag.get("source") == "agent" and node_id in flag.get("node_ids", []))

                flags = payload(await session.call_tool("list_flags", {"project_id": project_id}))
                check("list_flags returns it", any(f["id"] == flag["id"] for f in flags))

                # Cost guard: a paid run while unattended must be refused outright.
                blocked = payload(
                    await session.call_tool(
                        "run_node",
                        {"node_id": node_id, "backend": "api", "session_mode": "auto"},
                    )
                )
                check("run_node(api, auto) is blocked", blocked.get("blocked") is True, str(blocked.get("reason"))[:60])

                after = payload(await session.call_tool("get_node", {"node_id": node_id}))
                check("blocked run did not queue the node", after["status"] != "queued", f"status={after['status']}")

                flags_after = payload(await session.call_tool("list_flags", {"project_id": project_id}))
                check("blocked run left a flag for review", len(flags_after) > len(flags))

                check("list_backends", isinstance(payload(await session.call_tool("list_backends", {})), list))
                check("list_node_types", isinstance(payload(await session.call_tool("list_node_types", {})), list))
            finally:
                if project_id:
                    import httpx

                    async with httpx.AsyncClient(headers=headers, timeout=30) as c:
                        r = await c.delete(f"{base_url.rstrip('/')}/api/projects/{project_id}")
                        check("scratch project cleaned up", r.status_code in (204, 404), f"HTTP {r.status_code}")

    failed = [r for r in results if r[0] == FAIL]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    for _, name, detail in failed:
        print(f"  FAILED: {name} {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8011"
    tok = sys.argv[2] if len(sys.argv) > 2 else "dev-local-token"
    sys.exit(asyncio.run(main(base, tok)))
