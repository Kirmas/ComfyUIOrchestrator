"""End-to-end check of the Unreal/Blender bridge against a running server.

    cd backend && .venv/bin/python ../scripts/bridge_smoke_test.py http://127.0.0.1:8011 dev-local-token

(from backend/: the expiry check ages a key directly in the dev DB.)

Plays a plugin: pairs (request -> the page lists it with the same code ->
approve -> the plugin's poll gets a key exactly once), then works with that
device key only. Checks the key opens nothing outside /api/bridge/device/,
the API token opens nothing inside it, the download serves only assets sent to
the device, a send to a disconnected app is a 409 (no queue), denial and
revocation (which also drops the live socket), and an expired key. Deletes its
scratch project and devices afterwards.
"""
import asyncio
import json
from pathlib import Path
import struct
import sys
import zlib

import httpx
import websockets

BASE, TOKEN = sys.argv[1].rstrip("/"), sys.argv[2]
H = {"Authorization": f"Bearer {TOKEN}"}
WS = BASE.replace("http", "ws", 1) + "/api/bridge/device/ws"


def tiny_png() -> bytes:
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
    raw = b"".join(b"\x00" + b"\xff\x00\x00" * 4 for _ in range(4))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 4, 4, 8, 2, 0, 0, 0)) + \
        chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


async def pair(anon: httpx.AsyncClient, page: httpx.AsyncClient, name: str, approve: bool = True) -> dict:
    r = await anon.post("/api/bridge/device/pair", json={"app": "blender", "name": name})
    assert r.status_code == 200, r.text
    ask = r.json()
    listed = [p for p in (await page.get("/api/bridge/pairing")).json() if p["id"] == ask["request_id"]]
    assert listed and listed[0]["code"] == ask["code"], (listed, ask)
    poll = (await anon.post(f"/api/bridge/device/pair/{ask['request_id']}/poll", json={"secret": ask["secret"]})).json()
    assert poll["status"] == "pending", poll
    bad = (await anon.post(f"/api/bridge/device/pair/{ask['request_id']}/poll", json={"secret": "wrong"})).json()
    assert bad["status"] == "expired" and "key" not in bad, bad
    verb = "approve" if approve else "deny"
    r = await page.post(f"/api/bridge/pairing/{ask['request_id']}/{verb}")
    assert r.status_code in (200, 204), r.text
    got = (await anon.post(f"/api/bridge/device/pair/{ask['request_id']}/poll", json={"secret": ask["secret"]})).json()
    again = (await anon.post(f"/api/bridge/device/pair/{ask['request_id']}/poll", json={"secret": ask["secret"]})).json()
    assert again["status"] == "expired", "the key must leave exactly once"
    return {**got, "device": r.json() if approve else None}


async def ws_refused(key: str) -> str:
    async with websockets.connect(WS, additional_headers={"Authorization": f"Bearer {key}"}) as ws:
        try:
            await ws.recv()
        except websockets.exceptions.ConnectionClosed as exc:
            assert exc.rcvd.code == 4401, exc
            return exc.rcvd.reason
    raise AssertionError("connection was not refused")


async def main():
    async with httpx.AsyncClient(base_url=BASE, timeout=60) as anon, \
            httpx.AsyncClient(base_url=BASE, headers=H, timeout=60) as page:
        pid = (await page.post("/api/projects", json={"name": "bridge smoke test"})).json()["id"]
        devices = []
        try:
            denied = await pair(anon, page, "smoke-denied", approve=False)
            assert denied["status"] == "denied" and "key" not in denied, denied

            got = await pair(anon, page, "smoke")
            assert got["status"] == "approved" and got["key"], got
            key, dev = got["key"], got["device"]
            devices.append(dev["id"])
            D = {"Authorization": f"Bearer {key}"}

            # the device key opens only device routes; the API token opens none of them
            assert (await anon.get("/api/projects", headers=D)).status_code == 401
            assert (await anon.get("/api/bridge/clients", headers=D)).status_code == 401
            assert (await page.get("/api/bridge/device/targets")).status_code == 401
            assert (await anon.get("/api/bridge/device/targets")).status_code == 401
            assert "Invalid" not in await ws_refused(TOKEN)

            me = (await anon.get("/api/bridge/device/me", headers=D)).json()
            assert me["name"] == "smoke" and me["expires_at"], me

            async with websockets.connect(WS, additional_headers=D) as ws:
                await ws.send(json.dumps({"type": "hello", "version": "t"}))
                welcome = json.loads(await ws.recv())
                assert welcome["type"] == "welcome", welcome
                cid = welcome["client_id"]
                clients = (await page.get("/api/bridge/clients")).json()
                assert any(c["id"] == cid and c["name"] == "smoke" for c in clients), clients
                assert any(d["id"] == dev["id"] and d["online"] for d in (await page.get("/api/bridge/devices")).json())

                targets = (await anon.get("/api/bridge/device/targets", headers=D)).json()
                mine = next(t for t in targets if t["id"] == pid)
                assert mine["graphs"][0] == {"id": None, "name": "Main"}, mine

                r = await anon.post("/api/bridge/device/upload", headers=D,
                                    files={"file": ("Cube.png", tiny_png(), "image/png")},
                                    data={"project_id": pid, "place": "true"})
                assert r.status_code == 201, r.text
                up = r.json()
                aid = up["asset"]["id"]
                node = (await page.get(f"/api/nodes/{up['placed']['node_id']}")).json()
                assert node["node_type"] == "asset.single" and node["status"] == "done", node  # the cell OWNS the file
                outs = (await page.get(f"/api/nodes/{node['id']}/outputs")).json()
                assert [o["id"] for o in outs] == [aid], outs
                assert node.get("is_agent_created") in (False, None), node
                assert up["asset"].get("project_id") in (None, ""), up["asset"]  # node_id XOR project_id

                # not sent yet -> the device can't download it
                assert (await anon.get(f"/api/bridge/device/assets/{aid}/file", headers=D)).status_code == 403

                async def plugin():
                    msg = json.loads(await ws.recv())
                    assert msg["type"] == "import", msg
                    a = msg["asset"]
                    assert a["id"] == aid and a["format"] == "png" and a["project_name"] == "bridge smoke test", a
                    data = (await anon.get(a["url"], headers=D)).content
                    assert data.startswith(b"\x89PNG") and len(data) == a["size"], (len(data), a["size"])
                    await ws.send(json.dumps({"type": "result", "request_id": msg["request_id"], "ok": True,
                                              "message": "imported as Cube"}))

                task = asyncio.create_task(plugin())
                r = await page.post("/api/bridge/send", json={"asset_id": aid, "client_id": cid})
                await task
                assert r.status_code == 200 and r.json()["message"] == "imported as Cube", r.text

                # revoking drops the live socket at once
                assert (await page.delete(f"/api/bridge/devices/{dev['id']}")).status_code == 204
                devices.remove(dev["id"])
                try:
                    await asyncio.wait_for(ws.recv(), 5)
                    raise AssertionError("socket survived revocation")
                except websockets.exceptions.ConnectionClosed as exc:
                    assert exc.rcvd.code == 4401, exc

            assert (await anon.get("/api/bridge/device/me", headers=D)).status_code == 401
            assert "revoked" in await ws_refused(key) or "unknown" in await ws_refused(key)
            r = await page.post("/api/bridge/send", json={"asset_id": aid, "client_id": cid})
            assert r.status_code == 409, r.text

            # expiry: age a fresh device's key in the DB
            got = await pair(anon, page, "smoke-expiring")
            devices.append(got["device"]["id"])
            sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
            from datetime import datetime, timedelta, timezone
            import uuid
            from app.db.base import async_session_maker
            from app.db.models import BridgeDevice
            async with async_session_maker() as db:
                d = await db.get(BridgeDevice, uuid.UUID(got["device"]["id"]))
                d.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
                await db.commit()
            assert "expired" in await ws_refused(got["key"])
            r = await anon.get("/api/bridge/device/me", headers={"Authorization": f"Bearer {got['key']}"})
            assert r.status_code == 401 and "expired" in r.text, r.text
            print("bridge smoke test: OK")
        finally:
            for d in devices:
                await page.delete(f"/api/bridge/devices/{d}")
            await page.delete(f"/api/projects/{pid}")


asyncio.run(main())
