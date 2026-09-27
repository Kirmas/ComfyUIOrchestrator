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
import io
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



def big_png_b64(width: int = 3000, height: int = 2000) -> str:
    """Large enough that get_candidates has to scale it down, which is the
    path an 8K upscale takes (sending those as is closed the connection)."""
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (width, height), (40, 90, 160)).save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode()


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
            for required in (
                "create_node",
                "run_node",
                "await_node",
                "cancel_node",
                "select_candidate",
                "flag_cell",
                "reply_to_flag",
                "resolve_flag",
                "edit_flag_message",
            ):
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

                big = payload(
                    await session.call_tool(
                        "upload_reference_image",
                        {"node_id": node_id, "image_base64": big_png_b64(), "filename": "big.png"},
                    )
                )
                listing = await session.call_tool("get_candidates", {"node_id": node_id, "max_images": 0})
                texts = [b.text for b in listing.content if b.type == "text"]
                check(
                    "get_candidates(max_images=0) lists every id, no images",
                    not any(b.type == "image" for b in listing.content) and big["id"] in texts[0] and asset["id"] in texts[0],
                    texts[0][:80],
                )
                scaled = await session.call_tool("get_candidates", {"node_id": node_id, "max_px": 800})
                scaled_texts = " ".join(b.text for b in scaled.content if b.type == "text")
                check("get_candidates scales a big image to max_px", "sent at 800x533" in scaled_texts, scaled_texts[-60:])
                cropped = await session.call_tool(
                    "get_candidates", {"node_id": node_id, "max_px": 800, "region": [0.25, 0.25, 0.5, 0.5]}
                )
                cropped_texts = " ".join(b.text for b in cropped.content if b.type == "text")
                check("get_candidates crops region from the original", "sent at 750x500" in cropped_texts, cropped_texts[-60:])

                # Comment threads: one per cell, the person and the agent in it.
                flag = payload(await session.call_tool("flag_cell", {"node_id": node_id, "note": "smoke-test flag"}))
                check(
                    "flag_cell starts a thread",
                    node_id in flag.get("node_ids", [])
                    and [m["source"] for m in flag.get("messages", [])] == ["agent"]
                    and flag["resolved"] is False,
                )
                again = payload(await session.call_tool("flag_cell", {"node_id": node_id, "note": "second note"}))
                check("flag_cell on the same cell continues that thread", again["id"] == flag["id"] and len(again["messages"]) == 2)

                import httpx

                async with httpx.AsyncClient(headers=headers, timeout=30) as c:
                    r = await c.post(
                        f"{base_url.rstrip('/')}/api/annotations/{flag['id']}/messages", json={"text": "person replying"}
                    )
                    user_message = r.json()["messages"][-1]
                check("a person's reply lands as source=user", user_message["source"] == "user", user_message["source"])

                done = payload(
                    await session.call_tool("reply_to_flag", {"flag_id": flag["id"], "text": "done, have a look", "resolve": True})
                )
                check(
                    "reply_to_flag(resolve=True) answers and resolves",
                    done["resolved"] is True and done["resolved_by"] == "agent" and done["messages"][-1]["text"] == "done, have a look",
                )
                open_flags = payload(await session.call_tool("list_flags", {"project_id": project_id}))
                all_flags = payload(await session.call_tool("list_flags", {"project_id": project_id, "include_resolved": True}))
                check(
                    "list_flags hides resolved unless asked",
                    not any(f["id"] == flag["id"] for f in open_flags) and any(f["id"] == flag["id"] for f in all_flags),
                )

                own = done["messages"][0]
                edited = payload(await session.call_tool("edit_flag_message", {"message_id": own["id"], "text": "edited note"}))
                check("edit_flag_message rewrites the agent's own message", edited["messages"][0]["text"] == "edited note")
                refused = await session.call_tool("edit_flag_message", {"message_id": user_message["id"], "text": "hijack"})
                check("edit_flag_message refuses a person's message", refused.isError is True)

                reopened = payload(await session.call_tool("resolve_flag", {"flag_id": flag["id"], "resolved": False}))
                check("resolve_flag(resolved=False) reopens", reopened["resolved"] is False)

                flags = payload(await session.call_tool("list_flags", {"project_id": project_id}))
                check("list_flags returns it", any(f["id"] == flag["id"] for f in flags))
                messages_before = next(f for f in flags if f["id"] == flag["id"])["messages"]

                cancelled = payload(await session.call_tool("cancel_node", {"node_id": node_id}))
                check("cancel_node on a node with nothing in flight", cancelled.get("cancelled_variants") == [], str(cancelled))

                # Idea board (roadmap.md §1): a sticker the agent wrote, a
                # comment on it, and the project-wide uniqueness of a {tag}.
                board = payload(await session.call_tool("get_board", {"project_id": project_id}))
                check("get_board creates one on first access", bool(board.get("id")))

                note = payload(
                    await session.call_tool(
                        "create_note",
                        {"board_id": board["id"], "text": "**smoke** brow", "tag": "smoke_head", "x": 20, "y": 20},
                    )
                )
                check("create_note", note.get("tag") == "smoke_head" and note.get("source") == "agent")

                dup = await session.call_tool(
                    "create_note", {"board_id": board["id"], "text": "other", "tag": "smoke_head"}
                )
                check("duplicate {tag} is refused", dup.isError is True)

                comment = payload(
                    await session.call_tool(
                        "comment_on_board_item",
                        {"board_id": board["id"], "item_id": note["id"], "text": "too vague"},
                    )
                )
                check("comment_on_board_item", comment.get("target_item_id") == note["id"])

                items = payload(await session.call_tool("list_board_items", {"board_id": board["id"]}))
                check("list_board_items sees both", len(items) == 2, f"{len(items)} items")

                # Deleting the sticker must take its comment with it (self-FK
                # cascade), not leave it anchored to nothing.
                await session.call_tool("delete_board_item", {"item_id": note["id"]})
                left = payload(await session.call_tool("list_board_items", {"board_id": board["id"]}))
                check("deleting a sticker cascades to its comment", left == [], str(left)[:60])

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
                thread_after = next(f for f in flags_after if f["id"] == flag["id"])
                check(
                    "blocked run left a note in the cell's thread",
                    len(thread_after["messages"]) == len(messages_before) + 1
                    and thread_after["messages"][-1]["source"] == "agent",
                )

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
