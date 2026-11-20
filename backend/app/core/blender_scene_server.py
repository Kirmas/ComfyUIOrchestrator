"""Runs INSIDE a background Blender -- never imported by the app (`bpy` only
exists in Blender's own Python). One of these is a live "scene":

    blender -b [scene.blend] --factory-startup -noaudio --python-exit-code 1 \
        -P blender_scene_server.py -- SOCK IDLE_S SCENE_DIR fresh|resume

It listens on a unix socket and executes code the manager (blender_scene.py)
sends it, in ONE persistent namespace, so a hundred small edits cost
milliseconds instead of a Blender start-up each. The manager owns everything
about lifetime (idle close, memory cap, checkpoints, cleanup); this file's own
idle timeout is only a safety net for the case where the API process dies and
nothing is left to close the scene.

Protocol: one JSON object per line in, one per line out.
    {"op": "run", "code": "...", "save": true}   -> {"ok", "out", "ms"}
    {"op": "call", "fn": "info", "kwargs": {}}   -> {"ok", "result", "ms"}
    {"op": "save"} / {"op": "ping"} / {"op": "destroy"}

Everything below the protocol loop is the helper library the agent codes
against (documented for it in blender_scene.SCENE_GUIDE -- keep the two in
step). Objects the helpers create (camera, lights) carry `orch_aux = True` and
are never exported.
"""
import contextlib
import io
import json
import linecache
import math
import os
import socket
import sys
import time
import traceback

import bpy
import numpy as np
from mathutils import Vector

SOCK, IDLE_S, SCENE_DIR, MODE = sys.argv[sys.argv.index("--") + 1 :][:4]
IDLE_S = float(IDLE_S)
CKPT = os.path.join(SCENE_DIR, "scene.blend")
INPUTS_PATH = os.path.join(SCENE_DIR, "inputs.json")
AUX = "orch_aux"

if MODE == "fresh":
    bpy.ops.wm.read_factory_settings(use_empty=True)

try:
    with open(INPUTS_PATH, encoding="utf-8") as f:
        INPUTS = json.load(f)
except (OSError, ValueError):
    INPUTS = []


# ----------------------------------------------------------------- helpers --
def objects():
    """Every scene object that is real content (not the helper camera/lights)."""
    return [o for o in bpy.context.scene.objects if not o.get(AUX)]


def meshes():
    return [o for o in objects() if o.type == "MESH"]


def _aux(obj):
    obj[AUX] = True
    return obj


def import_file(path):
    """Import a GLB/GLTF/FBX/OBJ/STL/PLY by extension; returns the new objects' names."""
    before = {o.name for o in bpy.context.scene.objects}
    ext = os.path.splitext(path)[1].lower()
    if ext in (".glb", ".gltf"):
        bpy.ops.import_scene.gltf(filepath=path)
    elif ext == ".fbx":
        bpy.ops.import_scene.fbx(filepath=path)
    elif ext == ".obj":
        bpy.ops.wm.obj_import(filepath=path)
    elif ext == ".stl":
        bpy.ops.wm.stl_import(filepath=path)
    elif ext == ".ply":
        bpy.ops.wm.ply_import(filepath=path)
    else:
        raise ValueError(f"don't know how to import {ext!r}")
    return [o.name for o in bpy.context.scene.objects if o.name not in before]


def import_inputs():
    """Import every input the manager marked auto_import; records what each created."""
    for item in INPUTS:
        if item.get("auto_import") and not item.get("objects"):
            item["objects"] = import_file(item["path"])
    with open(INPUTS_PATH, "w", encoding="utf-8") as f:
        json.dump(INPUTS, f)
    return INPUTS


def bbox(objs=None):
    """World-space (min, max) corners of `objs` (default: all content), evaluated -- so a posed rig counts."""
    dg = bpy.context.evaluated_depsgraph_get()
    pts = []
    for o in objs or objects():
        if o.type not in ("MESH", "CURVE", "SURFACE", "FONT"):
            continue
        ev = o.evaluated_get(dg)
        pts.extend(ev.matrix_world @ Vector(c) for c in ev.bound_box)
    if not pts:
        return Vector((-1, -1, -1)), Vector((1, 1, 1))
    arr = np.array([p[:] for p in pts])
    return Vector(arr.min(axis=0)), Vector(arr.max(axis=0))


VIEWS = {
    "front": (0, -1, 0), "back": (0, 1, 0), "left": (-1, 0, 0), "right": (1, 0, 0),
    "top": (0, 0, 1), "bottom": (0, 0, -1),
    "three_quarter": (0.75, -1, 0.5), "three_quarter_back": (-0.75, 1, 0.5),
}


def _ensure_stage():
    """Neutral studio: a world colour, a key and a fill sun, one camera."""
    s = bpy.context.scene
    if s.world is None:
        w = bpy.data.worlds.new("World")
        w.use_nodes = True
        w.node_tree.nodes["Background"].inputs[0].default_value = (0.05, 0.05, 0.055, 1.0)
        s.world = w
    if "_sun_key" not in bpy.data.objects:
        for name, rot, energy in (("_sun_key", (0.8, 0.25, 0.5), 3.0), ("_sun_fill", (1.0, -0.2, -2.4), 1.2)):
            data = bpy.data.lights.new(name, "SUN")
            data.energy = energy
            obj = _aux(bpy.data.objects.new(name, data))
            obj.rotation_euler = rot
            s.collection.objects.link(obj)
    cam = bpy.data.objects.get("_cam")
    if cam is None:
        cam = _aux(bpy.data.objects.new("_cam", bpy.data.cameras.new("_cam")))
        s.collection.objects.link(cam)
    if s.camera is None:
        s.camera = cam
    return s.camera


def _tans(scene):
    """tan(half fov) along x and y of the render. `angle` is along the sensor-fit
    axis, which for AUTO is the longer side."""
    longer = math.tan(scene.camera.data.angle / 2)
    w, h = scene.render.resolution_x, scene.render.resolution_y
    return (longer, longer * h / w) if w >= h else (longer * w / h, longer)


def set_view(view="front", margin=1.12, ortho=False, objs=None):
    """Aim the helper camera at all content from a named direction (VIEWS) or any
    (x, y, z) vector, close enough to fill the frame. view="current" leaves it."""
    cam = _ensure_stage()
    if view == "current":
        return cam
    s = bpy.context.scene
    direction = Vector(VIEWS[view] if isinstance(view, str) else view).normalized()
    lo, hi = bbox(objs)
    ctr = (lo + hi) / 2
    corners = [Vector((x, y, z)) for x in (lo.x, hi.x) for y in (lo.y, hi.y) for z in (lo.z, hi.z)]
    cam.data.lens, cam.data.sensor_width, cam.data.sensor_fit = 50, 36, "AUTO"
    cam.data.type = "ORTHO" if ortho else "PERSP"
    quat = (-direction).to_track_quat("-Z", "Y")
    right, up, fwd = quat @ Vector((1, 0, 0)), quat @ Vector((0, 1, 0)), quat @ Vector((0, 0, -1))
    tx, ty = _tans(s)
    need, half_w, half_h = 0.0, 0.0, 0.0
    for c in corners:
        d = c - ctr
        x, y, z = abs(d.dot(right)) * margin, abs(d.dot(up)) * margin, d.dot(fwd)
        half_w, half_h = max(half_w, x), max(half_h, y)
        need = max(need, x / tx - z, y / ty - z)
    need = max(need, 0.01)
    cam.location = ctr + direction * need
    cam.rotation_euler = quat.to_euler()
    cam.data.clip_start, cam.data.clip_end = need * 0.01, need * 10 + (hi - lo).length * 2
    if ortho:
        cam.data.ortho_scale = 2 * max(half_w, half_h)
    s.camera = cam
    # Setting location/rotation does not refresh matrix_world until the depsgraph
    # re-evaluates; verts_under_mask() reads it, and would project through the
    # PREVIOUS camera pose without this.
    bpy.context.view_layer.update()
    return cam


def render_to(path, view="front", engine="workbench", size=512, frame=None, ortho=False, margin=1.12, samples=32):
    """Render the content to a PNG. engine: workbench (fast, always works, gray
    studio look) | eevee (nicer) | cycles (slow, CPU). size: px or (w, h)."""
    s = bpy.context.scene
    _ensure_stage()
    if frame is not None:
        s.frame_set(int(frame))
    s.render.resolution_x, s.render.resolution_y = (size, size) if isinstance(size, int) else size
    s.render.resolution_percentage = 100
    set_view(view, margin, ortho)
    s.render.engine = {"workbench": "BLENDER_WORKBENCH", "eevee": "BLENDER_EEVEE", "cycles": "CYCLES"}[engine]
    s.view_settings.view_transform = "Standard"
    if engine == "workbench":
        s.display.shading.light, s.display.shading.color_type = "STUDIO", "MATERIAL"
        s.display.render_aa = "8"
    elif engine == "cycles":
        s.cycles.device, s.cycles.samples = "CPU", int(samples)
    s.render.image_settings.file_format = "PNG"
    s.render.filepath = path
    bpy.ops.render.render(write_still=True)
    return path


def info():
    """Structure of the scene as a plain dict -- what an agent reads first."""
    s = bpy.context.scene
    out = {"frame_range": [s.frame_start, s.frame_end], "fps": s.render.fps, "objects": [], "actions": []}
    for o in objects():
        entry = {"name": o.name, "type": o.type, "parent": o.parent.name if o.parent else None,
                 "location": [round(v, 4) for v in o.location], "dimensions": [round(v, 4) for v in o.dimensions]}
        if o.type == "MESH":
            entry.update(vertices=len(o.data.vertices), faces=len(o.data.polygons),
                         materials=[m.name for m in o.data.materials if m], vertex_groups=len(o.vertex_groups),
                         modifiers=[f"{m.name}:{m.type}" for m in o.modifiers])
        elif o.type == "ARMATURE":
            bones = [b.name for b in o.data.bones]
            entry.update(bone_count=len(bones), bones=bones[:80])
        out["objects"].append(entry)
    for a in bpy.data.actions:
        out["actions"].append({"name": a.name, "frame_range": [round(v, 2) for v in a.frame_range]})
    lo, hi = bbox()
    out["bounds"] = {"min": [round(v, 4) for v in lo], "max": [round(v, 4) for v in hi]}
    return out


def verts_under_mask(mask_path, obj=None, channel="r", threshold=0.5, grid=256, depth_tol=0.04):
    """Indices of the vertices a person painted over, as seen from the CURRENT
    camera (call set_view(...) / render_to(...) with the view the mask was
    drawn on first). White (channel "r"|"g"|"b"|"a") = selected. Only the
    camera-facing surface counts, so this is a "where" hint, not a whole part:
    widen it with grow() before deforming, or the back of the part is left behind."""
    obj = obj or meshes()[0]
    s = bpy.context.scene
    bpy.context.view_layer.update()
    cam = s.camera
    img = bpy.data.images.load(mask_path)
    mw, mh = img.size
    chan = {"r": 0, "g": 1, "b": 2, "a": 3}[channel]
    mask = np.array(img.pixels[:], dtype=np.float32).reshape(mh, mw, 4)[:, :, chan]
    bpy.data.images.remove(img)
    n = len(obj.data.vertices)
    co = np.empty(n * 3, dtype=np.float32)
    obj.data.vertices.foreach_get("co", co)
    co = co.reshape(n, 3)
    to_cam = np.array(cam.matrix_world.inverted() @ obj.matrix_world)
    pc = co @ to_cam[:3, :3].T + to_cam[:3, 3]
    depth = -pc[:, 2]
    if cam.data.type == "ORTHO":
        half = cam.data.ortho_scale / 2
        w, h = s.render.resolution_x, s.render.resolution_y
        tx, ty = (half, half * h / w) if w >= h else (half * w / h, half)
        u, v = pc[:, 0] / tx, pc[:, 1] / ty
    else:
        tx, ty = _tans(s)
        safe = np.maximum(depth, 1e-9)
        u, v = pc[:, 0] / safe / tx, pc[:, 1] / safe / ty
    px, py = (u * 0.5 + 0.5), (v * 0.5 + 0.5)
    inside = (depth > 0) & (px >= 0) & (px < 1) & (py >= 0) & (py < 1)
    cell = np.zeros(n, dtype=np.int64)
    cell[inside] = (py[inside] * grid).astype(np.int64) * grid + (px[inside] * grid).astype(np.int64)
    front = np.full(grid * grid, np.inf, dtype=np.float32)
    np.minimum.at(front, cell[inside], depth[inside])
    lo, hi = bbox([obj])
    visible = inside & (depth <= front[cell] + (hi - lo).length * depth_tol)
    hit = np.zeros(n, dtype=bool)
    sel = np.where(visible)[0]
    mx = np.minimum((px[sel] * mw).astype(np.int64), mw - 1)
    my = np.minimum((py[sel] * mh).astype(np.int64), mh - 1)
    hit[sel[mask[my, mx] >= threshold]] = True
    return np.where(hit)[0]


def grow(obj, idx, hops=2):
    """Expand a vertex selection by `hops` edges (indices in, indices out)."""
    n = len(obj.data.vertices)
    edges = np.empty(len(obj.data.edges) * 2, dtype=np.int32)
    obj.data.edges.foreach_get("vertices", edges)
    a, b = edges[0::2], edges[1::2]
    mask = np.zeros(n, dtype=bool)
    mask[np.asarray(idx, dtype=np.int64)] = True
    for _ in range(int(hops)):
        nxt = mask.copy()
        nxt[a[mask[b]]] = True
        nxt[b[mask[a]]] = True
        mask = nxt
    return np.where(mask)[0]


def falloff(obj, idx, radius=None, power=2.0):
    """Per-vertex weight 1 at the selection's centre fading to 0 at `radius`
    (default: 1.3x the selection's own extent), all vertices, incl. hidden ones."""
    n = len(obj.data.vertices)
    co = np.empty(n * 3, dtype=np.float32)
    obj.data.vertices.foreach_get("co", co)
    co = co.reshape(n, 3)
    seed = co[np.asarray(idx, dtype=np.int64)]
    centre = seed.mean(axis=0)
    radius = radius or float(np.linalg.norm(seed - centre, axis=1).max()) * 1.3 or 1e-6
    return np.clip(1.0 - np.linalg.norm(co - centre, axis=1) / radius, 0.0, 1.0) ** power


def export_to(path, fmt="glb"):
    """Export all content (not the helper camera/lights) as glb | fbx | obj."""
    if not meshes():
        raise RuntimeError("nothing to export: the scene has no mesh")
    for o in bpy.context.scene.objects:
        o.select_set(not o.get(AUX))
    fmt = fmt.lower()
    if fmt == "glb":
        bpy.ops.export_scene.gltf(filepath=path, export_format="GLB", use_selection=True, export_apply=True, export_animations=True)
    elif fmt == "fbx":
        bpy.ops.export_scene.fbx(filepath=path, use_selection=True, add_leaf_bones=False, bake_anim=True)
    elif fmt == "obj":
        bpy.ops.wm.obj_export(filepath=path, export_selected_objects=True, apply_modifiers=True)
    else:
        raise ValueError("format must be glb, fbx or obj")
    return path


# ----------------------------------------------------------- protocol loop --
NAMESPACE = {k: v for k, v in globals().items() if not k.startswith("__")}
NAMESPACE.update(__name__="__scene__", np=np, Vector=Vector, math=math)


def _save():
    bpy.ops.wm.save_as_mainfile(filepath=CKPT, copy=True)


def _handle(req):
    op = req.get("op")
    started = time.perf_counter()
    buf = io.StringIO()
    ok, result = True, None
    with contextlib.redirect_stdout(buf):
        try:
            if op == "run":
                code = req["code"]
                # Registered so the traceback can quote the agent's own lines.
                linecache.cache["<agent>"] = (len(code), None, code.splitlines(True), "<agent>")
                exec(compile(code, "<agent>", "exec"), NAMESPACE)
            elif op == "call":
                result = NAMESPACE[req["fn"]](**req.get("kwargs", {}))
        except BaseException as exc:
            ok = False
            # Without this file's own frames: the agent can't act on them.
            tb = traceback.TracebackException.from_exception(exc)
            tb.stack = traceback.StackSummary.from_list([f for f in tb.stack if f.filename != __file__])
            buf.write("".join(tb.format()))
    out = buf.getvalue()
    # Checkpoint AFTER the call and outside the capture, so Blender's own
    # "Saved copy as ..." chatter doesn't end up in what the agent reads.
    if (ok and req.get("save")) or op == "save":
        try:
            _save()
        except BaseException:
            ok = False
            out += "\ncheckpoint failed:\n" + traceback.format_exc()
    return {"ok": ok, "out": out[-6000:], "result": result, "ms": round((time.perf_counter() - started) * 1000, 1)}


def _serve():
    if os.path.exists(SOCK):
        os.unlink(SOCK)
    srv = socket.socket(socket.AF_UNIX)
    srv.bind(SOCK)
    srv.listen(1)
    srv.settimeout(IDLE_S)
    while True:
        try:
            conn, _ = srv.accept()
        except socket.timeout:
            break
        with conn:
            data = b""
            while not data.endswith(b"\n"):
                chunk = conn.recv(1 << 20)
                if not chunk:
                    break
                data += chunk
            if not data:
                continue
            req = json.loads(data)
            if req.get("op") == "destroy":
                conn.sendall(b'{"ok": true}\n')
                break
            reply = _handle(req)
            conn.sendall((json.dumps(reply, default=str) + "\n").encode())
    if os.path.exists(SOCK):
        os.unlink(SOCK)


_serve()
