"""End-to-end check of the live Blender scenes (core/blender_scene.py) through the REST API.

    backend/.venv/bin/python scripts/blender_scene_smoke_test.py http://127.0.0.1:8011 dev-local-token

Needs a running server with Blender installed. Builds its own test meshes (a
1.2M-triangle sphere, to exercise automatic simplification) and uploads a
painted mask, so nothing depends on what is in the library; deletes every scene
and asset it made. Exits non-zero on the first failed check.

Extra checks that need a server started with special settings:
    --idle      server started with BLENDER_SCENE_IDLE_S=6      (idle close)
    --hard      server started with BLENDER_SCENE_HARD_MB=700   (memory kill)
"""
import io
import struct
import sys
import textwrap
import time

import httpx
import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1] / "backend"))
from app.core.mesh_simplify import _write_glb, _vertex_normals, glb_triangle_count  # noqa: E402

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8011"
TOKEN = sys.argv[2] if len(sys.argv) > 2 else "dev-local-token"
FLAGS = set(sys.argv[3:])
client = httpx.Client(base_url=BASE, headers={"Authorization": f"Bearer {TOKEN}"}, timeout=300)
made_assets: list[str] = []
made_scenes: list[str] = []


def check(cond, message):  # noqa
    if not cond:
        print("FAIL:", message)
        cleanup()
        sys.exit(1)
    print("  ok:", message)


def ok(response, expected=(200, 201, 204)):
    if response.status_code not in expected:
        print("FAIL: unexpected", response.status_code, response.text[:600])
        cleanup()
        sys.exit(1)
    if not response.content:
        return None
    return response.json() if "json" in response.headers.get("content-type", "") else response.content


def sphere_glb(rings: int, segments: int) -> bytes:
    """A UV sphere of radius 1 centred on the origin, as GLB bytes."""
    theta = np.linspace(0, np.pi, rings + 1)
    phi = np.linspace(0, 2 * np.pi, segments, endpoint=False)
    pts = np.array([[np.sin(t) * np.cos(p), np.sin(t) * np.sin(p), np.cos(t)] for t in theta for p in phi], dtype=np.float32)
    # glTF is Y-up; Blender's importer maps it to Z-up, so a sphere is orientation-free anyway.
    faces = []
    for r in range(rings):
        for s in range(segments):
            a, b = r * segments + s, r * segments + (s + 1) % segments
            c, d = a + segments, b + segments
            faces.append((a, c, b))
            faces.append((b, c, d))
    faces = np.array(faces, dtype=np.int32)
    return _write_glb(pts, _vertex_normals(pts, faces), faces, None)


def upload(name: str, data: bytes, mime: str) -> str:
    asset = ok(client.post("/api/assets/upload", files={"file": (name, data, mime)}))
    made_assets.append(asset["id"])
    return asset["id"]


def mask_png(size: int, box) -> bytes:
    img = Image.new("L", (size, size), 0)
    ImageDraw.Draw(img).ellipse(box, fill=255)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def cleanup():
    for scene_id in made_scenes:
        client.delete(f"/api/scenes/{scene_id}")
    for asset_id in made_assets:
        client.delete(f"/api/assets/{asset_id}")


def timed(label, fn):
    t = time.perf_counter()
    out = fn()
    print(f"  {label}: {time.perf_counter() - t:.1f}s")
    return out


# ------------------------------------------------------------------ setup --
import atexit
atexit.register(lambda: cleanup())
print("building test inputs")
big = sphere_glb(rings=600, segments=1000)  # 1.2M triangles
small = sphere_glb(rings=40, segments=80)
check(glb_triangle_count(big) > 1_000_000, f"big sphere has {glb_triangle_count(big):,} triangles")
big_id = upload("big.glb", big, "model/gltf-binary")
small_id = upload("small.glb", small, "model/gltf-binary")
mask_id = upload("mask.png", mask_png(384, (144, 144, 240, 240)), "image/png")  # centre disc

def main_flow():
    # ---------------------------------------------------------------- create ----
    print("scene_create with automatic simplification")
    scene = timed("create", lambda: ok(client.post("/api/scenes", json={"asset_ids": [big_id, mask_id], "name": "smoke"})))
    sid = scene["id"]
    made_scenes.append(sid)
    check(scene["state"] == "running", "scene is running after create")
    check(scene["inputs"][0].get("simplified_from", 0) > 1_000_000, f"dense input simplified: {scene['inputs'][0].get('note')}")
    check(scene["inputs"][0]["triangles"] <= 300_000 * 1.05, f"down to {scene['inputs'][0]['triangles']:,} triangles")
    check(scene["has_preview"], "preview thumbnail exists")
    ok(client.get(f"/api/scenes/{sid}/preview"))

    print("scene_info")
    info = ok(client.get(f"/api/scenes/{sid}/info"))
    objs = info["blender"]["objects"]
    check(any(o["type"] == "MESH" for o in objs), "the mesh was imported")
    mesh = next(o for o in objs if o["type"] == "MESH")
    check(abs(mesh["dimensions"][0] - 2.0) < 0.05, f"sphere is ~2 units wide: {mesh['dimensions']}")

    # ---------------------------------------------------------------- run -------
    print("scene_run: state persists, errors are reported")
    r = ok(client.post(f"/api/scenes/{sid}/run", json={"code": "marker = 41\nprint('hello', marker + 1)"}))
    check(r["ok"] and "hello 42" in r["output"], "stdout comes back")
    r = ok(client.post(f"/api/scenes/{sid}/run", json={"code": "print(marker + 1)"}))
    check(r["ok"] and "42" in r["output"], "variables persist between calls")
    r = ok(client.post(f"/api/scenes/{sid}/run", json={"code": "raise ValueError('boom')"}))
    check(not r["ok"] and "ValueError: boom" in r["output"], "an exception returns its traceback")
    r = ok(client.post(f"/api/scenes/{sid}/run", json={"code": "print(marker)"}))
    check(r["ok"], "the scene is still alive after an error")

    print("scene_render: contact sheet")
    r = timed("render 3 views", lambda: ok(client.post(f"/api/scenes/{sid}/render", json={"views": ["front", "back", "top"], "size": 256})))
    check(r["width"] == 768 and r["height"] == 256, f"sheet is {r['width']}x{r['height']}")
    png = client.get(f"/api/scenes/{sid}/render").content
    check(png[:8] == b"\x89PNG\r\n\x1a\n", "latest render is a PNG")
    sheet = Image.open(io.BytesIO(png)).convert("L")
    check(sheet.getextrema()[1] > 100, "the render is not blank")

    # --------------------------------------------------------------- the mask ---
    print("mask -> vertices (front view, 384 px)")
    ok(client.post(f"/api/scenes/{sid}/render", json={"views": ["front"], "size": 384}))
    r = ok(client.post(f"/api/scenes/{sid}/run", json={"code": textwrap.dedent("""
    o = meshes()[0]
    idx = verts_under_mask(INPUTS[1]["path"], o)
    co = np.empty(len(o.data.vertices) * 3, np.float32); o.data.vertices.foreach_get("co", co); co = co.reshape(-1, 3)
    sel = co[idx]
    print("count", len(idx), "maxabs_x", float(np.abs(sel[:, 0]).max()), "maxabs_z", float(np.abs(sel[:, 2]).max()), "mean_y", float(sel[:, 1].mean()))
    w = falloff(o, grow(o, idx, 3))
    print("weights", float(w.max()), int((w > 0).sum()))
    """)}))
    check(r["ok"], "mask helpers ran: " + r["output"].strip()[-200:])
    line = next(l for l in r["output"].splitlines() if l.startswith("count"))
    parts = line.split()
    count, max_x, max_z, mean_y = int(parts[1]), float(parts[3]), float(parts[5]), float(parts[7])
    check(count > 50, f"{count} vertices selected under the mask")
    check(max_x < 0.6 and max_z < 0.6, f"selection is the centre disc (max |x|={max_x:.2f}, |z|={max_z:.2f}), not the whole sphere")
    check(mean_y < -0.5, f"only the camera-facing hemisphere (mean y={mean_y:.2f})")

    # ---------------------------------------------------------------- export ----
    print("scene_export becomes a library asset")
    r = ok(client.post(f"/api/scenes/{sid}/run", json={"code": "meshes()[0].scale = (1, 1, 2)"}))
    asset = timed("export glb", lambda: ok(client.post(f"/api/scenes/{sid}/export", json={"format": "glb", "name": "result"})))
    made_assets.append(asset["id"])
    check(asset["kind"] == "mesh", f"GLB export is a mesh asset ({asset['mime_type']})")
    data = client.get(f"/api/assets/{asset['id']}/file").content
    check(data[:4] == b"glTF", "stored bytes are a GLB")
    check(glb_triangle_count(data) > 100_000, f"exported mesh has {glb_triangle_count(data):,} triangles")
    fbx = ok(client.post(f"/api/scenes/{sid}/export", json={"format": "fbx"}))
    made_assets.append(fbx["id"])
    check(client.get(f"/api/assets/{fbx['id']}/file").content[:18] == b"Kaydara FBX Binary", "FBX export is a real FBX")

    # ----------------------------------------------- timeout and the checkpoint --
    print("timeout kills the call; the scene returns to its checkpoint")
    ok(client.post(f"/api/scenes/{sid}/run", json={"code": "meshes()[0].location.x = 5.0"}))
    resp = client.post(f"/api/scenes/{sid}/run", json={"code": "import time; time.sleep(30)", "timeout_s": 3})
    check(resp.status_code == 408, f"overrun answers 408: {resp.text[:140]}")
    check(ok(client.get(f"/api/scenes/{sid}"))["state"] == "closed", "the scene is closed after the kill")
    r = ok(client.post(f"/api/scenes/{sid}/run", json={"code": "print(meshes()[0].location.x)"}))
    check(r["ok"] and "5.0" in r["output"], "the next call reopens it with the last successful change intact")
    r = ok(client.post(f"/api/scenes/{sid}/run", json={"code": "print('marker' in globals())"}))
    check("False" in r["output"], "python variables did not survive the restart (as documented)")

    # -------------------------------------------------------------- capacity ----
    print("capacity limit")
    second = ok(client.post("/api/scenes", json={"asset_ids": [small_id], "name": "second"}))
    made_scenes.append(second["id"])
    third = client.post("/api/scenes", json={"asset_ids": [small_id], "name": "third"})
    check(third.status_code == 409, f"third scene refused: {third.json().get('detail', '')[:100]}")
    listing = ok(client.get("/api/scenes"))
    check(listing["live"] == 2 and len(listing["scenes"]) == 2, f"list shows {listing['live']} live of max {listing['max_live']}")

    # ------------------------------------------------------------- lifecycle ----
    print("close, reopen, destroy")
    closed = ok(client.post(f"/api/scenes/{second['id']}/close"))
    check(closed["state"] == "closed" and closed["has_checkpoint"], "closed but the checkpoint is kept")
    ok(client.post(f"/api/scenes/{second['id']}/run", json={"code": "print(len(meshes()))"}))
    check(ok(client.get(f"/api/scenes/{second['id']}"))["state"] == "running", "a call reopens a closed scene")
    for s_id in (second["id"], sid):
        ok(client.delete(f"/api/scenes/{s_id}"))
        check(client.get(f"/api/scenes/{s_id}").status_code == 404, f"scene {s_id} is gone")
    made_scenes.clear()
    check(client.get("/api/scenes/..%2F..%2Fetc").status_code in (404, 422), "a path-traversal id is refused")



if "--extras-only" not in FLAGS:
    main_flow()

# ---------------------------------------------------------- optional extras --
if "--idle" in FLAGS:
    print("idle close (server needs BLENDER_SCENE_IDLE_S=6)")
    s = ok(client.post("/api/scenes", json={"asset_ids": [small_id], "name": "idle"}))
    made_scenes.append(s["id"])
    time.sleep(12)
    got = ok(client.get(f"/api/scenes/{s['id']}"))
    check(got["state"] == "closed" and got["has_checkpoint"], "an idle scene closed itself and kept its checkpoint")

if "--hard" in FLAGS:
    print("memory kill (server needs BLENDER_SCENE_HARD_MB=700)")
    s = ok(client.post("/api/scenes", json={"asset_ids": [small_id], "name": "hog"}))
    made_scenes.append(s["id"])
    # Allocate ~2.4 GB, then stay in the call long enough for the sweeper (ticks every 2 s) to see it.
    resp = client.post(f"/api/scenes/{s['id']}/run", json={
        "code": "hog = np.ones((300, 1024, 1024), dtype=np.float64)\nimport time; time.sleep(40)", "timeout_s": 60})
    check(resp.status_code >= 400 and "limit" in resp.text, f"a runaway call is killed with an explanation: {resp.status_code} {resp.text[:200]}")
    got = ok(client.get(f"/api/scenes/{s['id']}"))
    check(got["state"] == "closed" and got["has_checkpoint"] and "limit" in (got["error"] or ""), f"scene is closed with its checkpoint and the reason recorded: {got['error']}")
    check(ok(client.get("/api/health"), (200,))["status"] == "ok", "the API is still up")
    r = ok(client.post(f"/api/scenes/{s['id']}/run", json={"code": "print(len(meshes()))"}))
    check(r["ok"] and "1" in r["output"], "and it reopens from the checkpoint afterwards")

cleanup()
print("ALL CHECKS PASSED")
