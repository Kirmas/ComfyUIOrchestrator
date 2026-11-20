"""End to end: a native.agent_task node, the worker, a (fake) agent over real MCP,
live Blender scenes, and the result landing in the grid.

    backend/.venv/bin/python scripts/agent_task_smoke_test.py

Starts its own throwaway dev server (port 8013) pointed at scripts/fake_agent_runner.py
(port 8766), so it needs nothing running beforehand -- but stop any other dev server
first: two API processes over one scenes directory fight over it (CLAUDE.md).
Builds its own meshes; removes the project and assets it made.
"""
import io
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx
import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.core.mesh_simplify import _vertex_normals, _write_glb  # noqa: E402

API, RUNNER, TOKEN = "http://127.0.0.1:8013", "http://127.0.0.1:8766", "dev-local-token"
procs: list[subprocess.Popen] = []
failures = 0
made_projects: list[str] = []
client: httpx.Client


def check(cond, message):
    global failures
    print(("  ok: " if cond else "FAIL: ") + message)
    failures += 0 if cond else 1


def start():
    global client
    env = {**os.environ, "AGENT_RUNNER_URL": RUNNER}
    procs.append(subprocess.Popen([str(ROOT / "backend/.venv/bin/python"), str(ROOT / "scripts/fake_agent_runner.py"), "8766", API, TOKEN]))
    procs.append(subprocess.Popen([str(ROOT / "backend/.venv/bin/uvicorn"), "app.main:app", "--host", "127.0.0.1", "--port", "8013"],
                                  cwd=ROOT / "backend", env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    client = httpx.Client(base_url=API, headers={"Authorization": f"Bearer {TOKEN}"}, timeout=120)
    for _ in range(120):
        try:
            if client.get("/api/health").status_code == 200 and httpx.get(f"{RUNNER}/_log").status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise SystemExit("servers did not start")


def stop():
    for p in procs:
        p.send_signal(signal.SIGTERM)
    for p in procs:
        try:
            p.wait(10)
        except subprocess.TimeoutExpired:
            p.kill()


def ok(resp, codes=(200, 201, 202, 204)):
    if resp.status_code not in codes:
        raise SystemExit(f"FAIL: {resp.request.method} {resp.request.url.path} -> {resp.status_code} {resp.text[:400]}")
    return resp.json() if resp.content and "json" in resp.headers.get("content-type", "") else resp.content


def sphere_glb() -> bytes:
    theta, phi = np.linspace(0, np.pi, 31), np.linspace(0, 2 * np.pi, 60, endpoint=False)
    pts = np.array([[np.sin(t) * np.cos(p), np.sin(t) * np.sin(p), np.cos(t)] for t in theta for p in phi], dtype=np.float32)
    faces = []
    for r in range(30):
        for s in range(60):
            a, b = r * 60 + s, r * 60 + (s + 1) % 60
            faces += [(a, a + 60, b), (b, a + 60, b + 60)]
    faces = np.array(faces, dtype=np.int32)
    return _write_glb(pts, _vertex_normals(pts, faces), faces, None)


def png() -> bytes:
    buf = io.BytesIO()
    Image.new("L", (64, 64), 255).save(buf, "PNG")
    return buf.getvalue()


def run_task(instruction: str, wait_s: float = 120, extra_params: dict | None = None, cancel_after: float | None = None):
    """Build mesh + mask cells and an agent_task node over them; run it; return (node, last)."""
    project = ok(client.post("/api/projects", json={"name": "agent task smoke"}))
    made_projects.append(project["id"])
    track = ok(client.post("/api/tracks", json={"project_id": project["id"]}))
    mesh_cell = ok(client.post("/api/nodes", json={"track_id": track["id"], "step_index": 0, "kind": "asset", "node_type": "asset.single"}))
    mesh = ok(client.post(f"/api/nodes/{mesh_cell['id']}/upload-asset", files={"file": ("m.glb", sphere_glb(), "model/gltf-binary")}))
    mask = ok(client.post("/api/assets/upload", files={"file": ("mask.png", png(), "image/png")}))
    node = ok(client.post("/api/nodes", json={
        "track_id": track["id"], "step_index": 1, "kind": "workflow", "node_type": "native.agent_task",
        "params": {"instruction": instruction, "provider": "claude", "timeout_min": 5, **(extra_params or {})},
        "inputs": [{"type": "explicit", "output_id": mesh["id"]}, {"type": "explicit", "output_id": mask["id"]}]}))
    ok(client.post(f"/api/nodes/{node['id']}/generate", json={}))
    started = time.time()
    last = node
    while time.time() - started < wait_s:
        last = ok(client.get(f"/api/nodes/{node['id']}"))
        if cancel_after and time.time() - started > cancel_after:
            ok(client.post(f"/api/nodes/{node['id']}/cancel"))
            cancel_after = None
        if last["status"] in ("done", "error", "cancelled", "discarded"):
            break
        time.sleep(1)
    return node, last, mesh, mask, track


def runner_log() -> dict:
    return httpx.get(f"{RUNNER}/_log").json()


def library_temporaries() -> int:
    import sqlite3

    db = sqlite3.connect(ROOT / "backend/dev.db")
    return db.execute("select count(*) from assets where node_id is null and meta like '%blender_scene%'").fetchone()[0]


def main():
    start()
    try:
        print("[ok] the agent makes a result")
        node, last, mesh, mask, track = run_task("Make the sphere 50% wider [ok]")
        log = runner_log()
        chat = next(iter(log.values()))
        check(last["status"] == "done", f"node finished: {last['status']} {last.get('error') or ''}")
        check(len(log) == 1, "exactly one agent chat was opened")
        check(chat["create"]["kind"] == "project" and chat["create"]["project_id"], "it is a project chat bound to the project")
        check(len(chat["messages"]) == 1 and mesh["id"] in chat["messages"][0] and mask["id"] in chat["messages"][0], "the task message lists both inputs by id")
        check(chat["messages"][0].startswith("Task: Make the sphere 50% wider"), "the first line becomes the chat title")
        check(chat.get("viewed_image") is True, "the agent could LOOK at the image input (view_asset over MCP)")
        check(chat.get("saw_render") is True, "the agent was sent a picture by scene_render")
        check("agent_error" not in chat, f"the agent's tool calls all worked ({chat.get('agent_error')})")
        jobs = ok(client.get(f"/api/nodes/{node['id']}/jobs"))
        check(jobs and jobs[0]["external_job_id"] == chat["id"] and jobs[0]["retries"] == 0, "the job's external id IS the chat id, no retries")
        out_nodes = [n for n in ok(client.get(f"/api/projects/{track['project_id']}/recipe"))["steps"][2]["nodes"]]
        outputs = ok(client.get(f"/api/nodes/{out_nodes[0]['node_id']}/outputs"))
        check(len(outputs) == 1 and outputs[0]["kind"] == "mesh", f"one mesh landed in the grid: {[o['kind'] for o in outputs]}")
        data = client.get(f"/api/assets/{outputs[0]['id']}/file").content
        check(data[:4] == b"glTF", "and it is a real GLB")
        check((outputs[0].get("meta") or {}).get("agent_note") == "made it 50% wider", "the agent's note is kept with the asset")
        check(library_temporaries() == 0, "the agent's temporary export was cleaned out of the library")
        check(ok(client.get("/api/scenes"))["live"] == 0, "the agent's scene was destroyed")

        print("[input] handing back an input must not delete it")
        node, last, mesh, mask, track = run_task("Nothing to change [input]")
        check(last["status"] == "done", f"node finished: {last['status']} {last.get('error') or ''}")
        check(client.get(f"/api/assets/{mesh['id']}/file").status_code == 200, "the INPUT asset still exists")

        print("[fail] the agent says it can't")
        before = len(runner_log())
        node, last, *_ = run_task("Add a tail [fail]")
        jobs = ok(client.get(f"/api/nodes/{node['id']}/jobs"))
        check(last["status"] == "error" and "mesh has no head" in (jobs[0].get("error") or ""), f"node is an error, the job carries the agent's reason: {jobs[0].get('error')}")
        check(len(runner_log()) - before == 1, "failure did NOT trigger an automatic retry (still one chat)")
        check(jobs[0]["status"] == "error" and len(jobs) == 1, "a single job, ended as an error")

        print("[silent] the agent stops without finishing")
        before = len(runner_log())
        node, last, *_ = run_task("Do something [silent]", wait_s=60)
        new_chat = [c for c in runner_log().values() if "[silent]" in c["messages"][0]][-1]
        jobs = ok(client.get(f"/api/nodes/{node['id']}/jobs"))
        check(last["status"] == "error" and "finish_task" in (jobs[0].get("error") or ""), f"node is an error, the job says why: {jobs[0].get('error')}")
        check(len(new_chat["messages"]) == 2 and "finish_task" in new_chat["messages"][1], "it was nudged exactly once")
        check(len(runner_log()) - before == 1, "no automatic retry (still one chat)")

        print("[slow] cancelling a running task stops the agent")
        node, last, *_ = run_task("Take forever [slow]", wait_s=60, cancel_after=8)
        slow_chat = [c for c in runner_log().values() if "[slow]" in c["messages"][0]][-1]
        check(last["status"] in ("cancelled", "error", "discarded") and last["status"] != "done", f"node is no longer running: {last['status']}")
        check(slow_chat["stops"] >= 1, f"the chat was stopped ({slow_chat['stops']}x)")
    finally:
        for pid in made_projects:
            httpx.delete(f"{API}/api/projects/{pid}", headers={"Authorization": f"Bearer {TOKEN}"})
        stop()
    print("ALL CHECKS PASSED" if not failures else f"{failures} FAILED")
    sys.exit(1 if failures else 0)


main()
