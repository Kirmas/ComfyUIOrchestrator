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
from mathutils import Matrix, Vector

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
    """Import a GLB/GLTF/FBX/OBJ/STL/PLY/ABC by extension; returns the new objects' names."""
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
    elif ext == ".abc":
        # A cache: meshes (with a sequence-cache modifier), hair/fur as CURVES objects.
        bpy.ops.wm.alembic_import(filepath=path, as_background_job=False, set_frame_range=True)
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
        if o.type not in ("MESH", "CURVE", "CURVES", "SURFACE", "FONT"):
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


def set_view(view="front", margin=1.12, ortho=False, objs=None, focus=None):
    """Aim the helper camera at all content from a named direction (VIEWS) or any
    (x, y, z) vector, close enough to fill the frame. view="current" leaves it.
    focus=<bone name> frames that bone and everything under it (a hand with its
    fingers) instead of the whole scene, re-aimed for the current pose; with a
    hand as focus, view may also be one of HAND_VIEWS (palm, back_of_hand, thumb_side,
    pinky_side, fingertips, wrist), which follow the hand's own orientation."""
    cam = _ensure_stage()
    if view == "current":
        return cam
    s = bpy.context.scene
    if focus is not None:
        bpy.context.view_layer.update()
    if isinstance(view, str) and view in HAND_VIEWS:
        if focus is None:
            raise ValueError(f"view {view!r} is relative to a hand: pass focus=<hand bone>")
        direction = _focus_direction(view, focus)
    else:
        direction = Vector(VIEWS[view] if isinstance(view, str) else view).normalized()
    if focus is not None:
        corners = _focus_points(focus)
        arr = np.array([p[:] for p in corners])
        lo, hi = Vector(arr.min(axis=0)), Vector(arr.max(axis=0))
    else:
        lo, hi = bbox(objs)
        corners = [Vector((x, y, z)) for x in (lo.x, hi.x) for y in (lo.y, hi.y) for z in (lo.z, hi.z)]
    ctr = (lo + hi) / 2
    cam.data.lens, cam.data.sensor_width, cam.data.sensor_fit = 50, 36, "AUTO"
    cam.data.type = "ORTHO" if ortho else "PERSP"
    quat = (-direction).to_track_quat("-Z", "Y")
    if isinstance(view, str) and view in HAND_VIEWS:
        # Fingers up in the picture (palm up when looking along the fingers),
        # instead of whatever roll the world axes happen to give.
        hf = hand_frame(focus)
        hint = hf["palm"] if view in ("fingertips", "wrist") else hf["along"]
        cam_up = (hint - direction * hint.dot(direction)).normalized()
        if cam_up.length > 0.5:
            quat = Matrix((cam_up.cross(direction), cam_up, direction)).transposed().to_quaternion()
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
    if focus is not None:
        # A close-up camera may sit inside the body (a palm resting on a thigh):
        # start the view just in front of the focus so nothing hides it.
        nearest = min(need + (c - ctr).dot(fwd) for c in corners)
        cam.data.clip_start = max(need * 0.01, nearest * 0.9)
    if ortho:
        cam.data.ortho_scale = 2 * max(half_w, half_h)
    s.camera = cam
    # Setting location/rotation does not refresh matrix_world until the depsgraph
    # re-evaluates; verts_under_mask() reads it, and would project through the
    # PREVIOUS camera pose without this.
    bpy.context.view_layer.update()
    return cam


def render_to(path, view="front", engine="workbench", size=512, frame=None, ortho=False, margin=1.12, samples=32,
              focus=None):
    """Render the content to a PNG. engine: workbench (fast, always works, gray
    studio look) | eevee (nicer) | cycles (slow, CPU). size: px or (w, h).
    focus: a bone to close up on (see set_view)."""
    s = bpy.context.scene
    _ensure_stage()
    if frame is not None:
        s.frame_set(int(frame))
    s.render.resolution_x, s.render.resolution_y = (size, size) if isinstance(size, int) else size
    s.render.resolution_percentage = 100
    set_view(view, margin, ortho, focus=focus)
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
            entry.update(bone_count=len(bones), bones=bones)
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


# ------------------------------------------------------------- pose / hands --
# An agent posing a rig through raw bone quaternions keeps getting fingers wrong:
# every rig has its own bone roll, so "curl" is +X on one and -Z on another, and
# a hand is 20 px on a full-body render. These helpers read a pose back in the
# hand's OWN frame (along the fingers / across the knuckles / out of the palm),
# and aim the camera at a bone instead of at the whole scene.
FINGERS = (("thumb", ("thumb",)), ("index", ("index", "pointer")), ("middle", ("middle",)),
           ("ring", ("ring",)), ("pinky", ("pinky", "little", "small")))
CM = 100.0  # Blender units are metres; reports are in centimetres.


def armatures():
    return [o for o in objects() if o.type == "ARMATURE"]


def find_bone(name):
    """(armature, pose bone) for a bone name; exact match first, then case-insensitive."""
    for exact in (True, False):
        for arm in armatures():
            for pb in arm.pose.bones:
                if pb.name == name if exact else pb.name.lower() == name.lower():
                    return arm, pb
    known = ", ".join(pb.name for arm in armatures() for pb in arm.pose.bones)
    raise ValueError(f"no bone {name!r}. Bones: {known[:1500]}")


def _subtree(pb):
    out, stack = [], [pb]
    while stack:
        b = stack.pop()
        out.append(b)
        stack.extend(b.children)
    return out


def _real(b):
    """Not a zero-length / '_end' leaf some importers add."""
    return b.bone.length > 1e-5 and not b.name.lower().endswith(("_end", ".end", "end"))


def _chains(hand):
    """One chain per leaf under `hand`: the bones from hand's child down to that leaf."""
    chains = []
    for leaf in _subtree(hand):
        if leaf.name == hand.name or leaf.children:
            continue
        chain, b = [], leaf
        while b is not None and b.name != hand.name:  # a new wrapper per access: `is` never matches
            chain.append(b)
            b = b.parent
        chain = [c for c in reversed(chain) if _real(c)]
        if chain:
            chains.append(chain)
    return chains


def _classify(chains, head, along):
    """{finger name: chain}. By bone names first. Unnamed chains: the one rooted
    nearest the wrist is the thumb, the rest are ordered across the knuckles
    away from it (index, middle, ring, pinky)."""
    named, rest = {}, []
    for ch in chains:
        text = " ".join(b.name.lower() for b in ch)
        hit = next((f for f, keys in FINGERS if f not in named and any(k in text for k in keys)), None)
        if hit:
            named[hit] = ch
        else:
            rest.append(ch)
    if rest and "thumb" not in named and len(rest) + len(named) >= 4:
        thumb = min(rest, key=lambda ch: head(ch[0]).dot(along))
        named["thumb"] = thumb
        rest.remove(thumb)
    if rest:
        side = head(named["thumb"][0]) if "thumb" in named else None
        if side is not None:
            rest.sort(key=lambda ch: (head(_knuckle(ch)) - side).length)
    for f, ch in zip([f for f, _ in FINGERS if f not in named], rest):
        named[f] = ch
    return named


def _knuckle(chain, thumb=False):
    """Head of the proximal phalanx: past a metacarpal when the chain has one."""
    if thumb or len(chain) <= 3:
        return chain[0]
    return chain[-3]


def _hand_like(pb):
    """3+ short chains (fingers: up to a metacarpal + 3 phalanges + an end bone)."""
    chains = _chains(pb)
    return len(chains) >= 3 and all(2 <= len(c) <= 5 for c in chains)


def is_hand(pb):
    """Hand-like and the deepest such bone -- a forearm also has short chains
    under it, but its hand child does too."""
    return _hand_like(pb) and not any(_hand_like(c) for c in pb.children)


def hands():
    """Every bone that looks like a hand."""
    return [pb.name for arm in armatures() for pb in arm.pose.bones if is_hand(pb)]


def _hand_axes(hand, rest):
    """(along, across, palm) unit vectors in ARMATURE space. along = wrist ->
    knuckles, across = towards the thumb side, palm = out of the palm. rest=True
    uses the rest pose (for deciding which side is the palm), else the current pose."""
    def head(b):
        return Vector(b.bone.head_local) if rest else Vector(b.head)

    def tail(b):
        return Vector(b.bone.tail_local) if rest else Vector(b.tail)

    chains = _chains(hand)
    rough_along = (tail(hand) - head(hand)).normalized()
    fingers = _classify(chains, head, rough_along)
    knuckles = {f: head(_knuckle(ch, f == "thumb")) for f, ch in fingers.items()}
    non_thumb = [k for f, k in knuckles.items() if f != "thumb"] or list(knuckles.values())
    along = (sum(non_thumb, Vector()) / len(non_thumb) - head(hand)).normalized()
    if "index" in knuckles and "pinky" in knuckles:
        across = knuckles["index"] - knuckles["pinky"]
    else:
        pts = sorted(non_thumb, key=lambda p: p.dot(along.orthogonal()))
        across = pts[-1] - pts[0]
    across = (across - along * across.dot(along)).normalized()
    if "thumb" in knuckles and (knuckles["thumb"] - head(hand)).dot(across) < 0:
        across = -across  # the index side is the thumb side
    palm = along.cross(across).normalized()
    return along, across, palm, fingers


def _palm_sign(arm, hand):
    """+1/-1 so that along x across points OUT of the palm. Decided once from the
    rest pose: the thumb and a resting curl both sit on the palm side; with no
    such cue a T/A-pose palm faces down. Overridable with palm_flip()."""
    key = f"orch_palm_flip:{hand.name}"
    along, across, palm, fingers = _hand_axes(hand, rest=True)
    score = 0.0
    origin = Vector(hand.bone.head_local)
    for f, ch in fingers.items():
        tip = Vector(ch[-1].bone.tail_local)
        score += (tip - origin).dot(palm) * (2.0 if f == "thumb" else 1.0)
    span = max((Vector(ch[-1].bone.tail_local) - origin).length for ch in fingers.values()) or 1.0
    if abs(score) < 0.02 * span:
        down = arm.matrix_world.to_3x3().inverted() @ Vector((0, 0, -1))
        score = palm.dot(down)
    sign = 1.0 if score >= 0 else -1.0
    return -sign if arm.get(key) else sign


def palm_flip(hand_bone, flip=True):
    """Tell the helpers the palm is on the other side of `hand_bone` than they
    guessed (pose_report's finger bends all read as 'backwards' when it's wrong).
    Saved with the scene."""
    arm, hand = find_bone(hand_bone)
    arm[f"orch_palm_flip:{hand.name}"] = bool(flip)
    return _palm_sign(arm, hand)


def hand_frame(hand_bone):
    """World-space frame of a hand in the CURRENT pose: origin (wrist), along,
    across (thumb side), palm (out of the palm), and its fingers."""
    arm, hand = find_bone(hand_bone)
    along, across, palm, fingers = _hand_axes(hand, rest=False)
    palm = palm * _palm_sign(arm, hand)
    rot = arm.matrix_world.to_3x3()
    return {"arm": arm, "hand": hand, "origin": arm.matrix_world @ Vector(hand.head),
            "along": (rot @ along).normalized(), "across": (rot @ across).normalized(),
            "palm": (rot @ palm).normalized(), "fingers": fingers}


def _focus_points(focus):
    """World points to frame for a bone (and everything under it) or an object."""
    if focus in bpy.data.objects and not bpy.data.objects[focus].get(AUX):
        lo, hi = bbox([bpy.data.objects[focus]])
        return [lo, hi]
    arm, pb = find_bone(focus)
    mw = arm.matrix_world
    bones = _subtree(pb)
    pts = [mw @ Vector(b.head) for b in bones] + [mw @ Vector(b.tail) for b in bones]
    # Bones are the skeleton, not the flesh: pad by a share of the longest bone
    # so a finger's skin isn't cut off at the frame edge.
    pad = max(b.bone.length for b in bones) * max(mw.to_scale()) * 0.6
    ctr = sum(pts, Vector()) / len(pts)
    extra = [ctr + Vector(d) * pad for d in ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1))]
    return pts + extra


HAND_VIEWS = ("palm", "back_of_hand", "thumb_side", "pinky_side", "fingertips", "wrist")


def _focus_direction(view, focus):
    """A view name relative to the focused hand, as a world direction (target -> camera)."""
    f = hand_frame(focus)
    return {"palm": f["palm"], "back_of_hand": -f["palm"], "thumb_side": f["across"], "pinky_side": -f["across"],
            "fingertips": f["along"], "wrist": -f["along"]}[view]


def _r(v, k=1):
    return round(float(v), k)


def _char_axes(arm):
    """The character's own right / forward / up (models face -Y, Z up)."""
    rot = arm.matrix_world.to_3x3()
    fwd, up = (rot @ Vector((0, -1, 0))).normalized(), (rot @ Vector((0, 0, 1))).normalized()
    return fwd.cross(up).normalized(), fwd, up


def _describe_dir(v, arm):
    """'forward-down' style words for a world direction, in the character's frame."""
    right, fwd, up = _char_axes(arm)
    comps = [(v.dot(right), "character's right", "character's left"), (v.dot(fwd), "forward", "backward"),
             (v.dot(up), "up", "down")]
    comps.sort(key=lambda c: -abs(c[0]))
    words = [pos if val > 0 else neg for val, pos, neg in comps if abs(val) > 0.35]
    return " + ".join(words) or (comps[0][1] if comps[0][0] > 0 else comps[0][2])


def _signed_angle(a, b, axis):
    """Angle a -> b in degrees, signed by rotation about `axis`."""
    return math.degrees(math.atan2(a.cross(b).dot(axis), a.dot(b)))


def _props_near(arm, centre, radius):
    """Meshes that aren't skinned to `arm` (a held sword, a table) near `centre`."""
    out = []
    for o in meshes():
        if any(m.type == "ARMATURE" and m.object == arm for m in o.modifiers):
            continue
        lo, hi = bbox([o])
        nearest = Vector([min(max(c, l), h) for c, l, h in zip(centre, lo, hi)])
        if (nearest - centre).length <= radius:
            out.append(o)
    return out


def _contacts(arm, points, radius):
    """{label: {prop, distance_cm, inside}} for named world points vs nearby props."""
    from mathutils.bvhtree import BVHTree

    if not points:
        return {}
    centre = sum(points.values(), Vector()) / len(points)
    props = _props_near(arm, centre, radius)
    if not props:
        return {}
    dg = bpy.context.evaluated_depsgraph_get()
    trees = [(o, BVHTree.FromObject(o, dg), o.matrix_world.inverted(), o.matrix_world) for o in props]
    out = {}
    for label, p in points.items():
        best = None
        for o, tree, inv, mw in trees:
            loc, normal, _i, _d = tree.find_nearest(inv @ p)
            if loc is None:
                continue
            wloc = mw @ loc
            wnormal = (mw.to_3x3().inverted().transposed() @ normal).normalized()
            dist = (p - wloc).length
            if best is None or dist < best[1]:
                best = (o.name, dist, (p - wloc).dot(wnormal) < 0)
        if best:
            out[label] = {"prop": best[0], "distance_cm": _r(best[1] * CM),
                          "inside": best[2] and best[1] > 0.002}
    return out


def _curl_axis(along, across, palm, thumb=False):
    """Rotation axis that curls a finger in (its tip toward the palm). A thumb
    folds across the palm instead: from the thumb side toward the palm."""
    return (across.cross(palm) if thumb else along.cross(palm)).normalized()


# Joint bends (degrees) of a full curl, knuckle first; a thumb has fewer joints.
FULL_CURL = (80.0, 100.0, 70.0)
FULL_THUMB = (30.0, 50.0, 70.0)
HAND_PRESETS = {
    "open": {f: (0.0, 0.0) for f, _ in FINGERS} | {"index": (0.0, 8.0), "ring": (0.0, -5.0), "pinky": (0.0, -12.0)},
    "flat": {f: (0.0, 0.0) for f, _ in FINGERS},
    "relaxed": {"thumb": (0.2, 0.0), "index": (0.2, 0.0), "middle": (0.28, 0.0), "ring": (0.34, 0.0),
                "pinky": (0.4, 0.0)},
    "fist": {"thumb": (0.7, 0.0), "index": (1.0, 0.0), "middle": (1.0, 0.0), "ring": (1.0, 0.0), "pinky": (1.0, 0.0)},
    "grip": {"thumb": (0.5, 0.0), "index": (0.7, 0.0), "middle": (0.75, 0.0), "ring": (0.8, 0.0), "pinky": (0.85, 0.0)},
    "point": {"thumb": (0.6, 0.0), "index": (0.0, 0.0), "middle": (1.0, 0.0), "ring": (1.0, 0.0), "pinky": (1.0, 0.0)},
    "pinch": {"thumb": (0.55, 0.0), "index": (0.5, 0.0), "middle": (0.3, 0.0), "ring": (0.35, 0.0),
              "pinky": (0.4, 0.0)},
}


def pose_finger(hand_bone, finger, curl=0.0, spread=0.0, key=False):
    """Set one finger WITHOUT touching bone axes: curl 0 = straight .. 1 = fully
    curled into the palm (negative bends it backwards -- don't); spread in degrees
    turns the finger in the palm plane, + toward the thumb side. Assumes the rig's
    REST hand is flat, as almost every rig's is. key=True inserts keyframes at
    the current frame."""
    from mathutils import Quaternion

    arm, hand = find_bone(hand_bone)
    along, across, palm, fingers = _hand_axes(hand, rest=True)
    palm = palm * _palm_sign(arm, hand)
    if finger not in fingers:
        raise ValueError(f"{hand.name} has no {finger!r}; it has {sorted(fingers)}")
    chain = fingers[finger]
    thumb = finger == "thumb"
    axis = _curl_axis(along, across, palm, thumb)
    full = FULL_THUMB if thumb else FULL_CURL
    start = 0 if thumb else chain.index(_knuckle(chain))
    for i, pb in enumerate(chain[start:start + len(full)]):
        # Rotating about the curl axis keeps that axis fixed, so it reads the
        # same in every bone's rest frame no matter how its parents are curled.
        rest_inv = pb.bone.matrix_local.to_3x3().inverted()
        q = Quaternion((rest_inv @ axis).normalized(), math.radians(full[i] * curl))
        if i == 0 and spread:
            q = Quaternion((rest_inv @ palm).normalized(), math.radians(spread)) @ q
        pb.rotation_mode = "QUATERNION"
        pb.rotation_quaternion = q
        if key:
            pb.keyframe_insert("rotation_quaternion")
    bpy.context.view_layer.update()


def pose_hand(hand_bone, pose="relaxed", key=False, **fingers):
    """Whole hand at once: a preset (open flat relaxed fist grip point pinch)
    and/or per-finger overrides, e.g. pose_hand("hand.R", "fist", index=0) or
    pose_hand("hand.L", thumb=(0.6, 10), index=0.5). A value is curl or
    (curl, spread). key=True keyframes it at the current frame."""
    if pose not in HAND_PRESETS:
        raise ValueError(f"pose must be one of {sorted(HAND_PRESETS)}")
    plan = dict(HAND_PRESETS[pose])
    for f, v in fingers.items():
        plan[f] = tuple(v) if isinstance(v, (list, tuple)) else (float(v), plan.get(f, (0, 0))[1])
    _arm, hand = find_bone(hand_bone)
    have = _hand_axes(hand, rest=True)[3]
    for f, (curl, spread) in plan.items():
        if f in have:
            pose_finger(hand_bone, f, curl, spread, key)
    return sorted(have)


def _hand_report(hand_bone, contacts=True):
    f = hand_frame(hand_bone)
    arm, hand = f["arm"], f["hand"]
    mw = arm.matrix_world
    along, across, palm = f["along"], f["across"], f["palm"]
    curl_axis = _curl_axis(along, across, palm)
    origin = f["origin"]
    root = next(b for b in arm.pose.bones if b.parent is None)
    right, fwd, up = _char_axes(arm)
    rel = origin - mw @ Vector(root.head)
    report = {
        "hand": hand.name,
        "wrist_world": [_r(c, 3) for c in origin],
        "wrist_from_root_cm": {"right": _r(rel.dot(right)), "forward": _r(rel.dot(fwd)), "up": _r(rel.dot(up)),
                               "root": root.name},
        "palm_faces": _describe_dir(palm, arm),
        "fingers_point": _describe_dir(along, arm),
        "fingers": {},
    }
    warnings, tips = [], {}
    for name, chain in f["fingers"].items():
        thumb = name == "thumb"
        dirs = [(mw @ Vector(b.tail) - mw @ Vector(b.head)).normalized() for b in chain]
        if thumb:
            # The thumb's base sits at its own angle; only its joints bend.
            k, prev, axis = 1, dirs[:-1], _curl_axis(along, across, palm, thumb=True)
        else:
            # the knuckle bends relative to the back of the hand, later joints relative to the previous bone
            k = chain.index(_knuckle(chain))
            prev, axis = [along] + dirs[k:-1], curl_axis
        bends, sideways = [], []
        for i, (a, b) in enumerate(zip(prev, dirs[k:])):
            bend = _signed_angle(a, b, axis)
            bends.append(_r(bend, 0))
            total = math.degrees(a.angle(b, 0.0))
            if not thumb and i > 0 and total > 12:
                off_plane = math.degrees(math.asin(min(1.0, abs(a.cross(b).normalized().dot(palm)))))
                if off_plane > 35:
                    sideways.append(chain[k + i].name)
        tip = mw @ Vector(chain[-1].tail)
        tips[name] = tip
        local = tip - origin
        entry = {"bones": [b.name for b in chain], "bend_deg": bends}
        if thumb:
            entry["tip_cm"] = {"along": _r(local.dot(along)), "across": _r(local.dot(across)),
                               "palm_side": _r(local.dot(palm))}
        else:
            entry["curl"] = _r(max(0.0, sum(bends)) / 250.0, 2)
            prox = dirs[k] - palm * dirs[k].dot(palm)
            entry["spread_deg"] = _r(_signed_angle(along, prox.normalized(), palm), 0) if prox.length > 1e-6 else 0
        report["fingers"][name] = entry
        back = [b for b, deg in zip(chain[k:], bends) if deg < -15]
        if back:
            warnings.append(f"{name}: bends BACKWARDS at {', '.join(b.name for b in back)} "
                            f"({', '.join(str(d) for d in bends if d < -15)} deg)")
        if sideways:
            warnings.append(f"{name}: bends sideways (out of its curl plane) at {', '.join(sideways)}")
    if "thumb" in tips:
        report["thumb_tip_to_cm"] = {n: _r((tips["thumb"] - p).length * CM) for n, p in tips.items() if n != "thumb"}
    if contacts:
        pts = {f"{n}_tip": p for n, p in tips.items()}
        pts["palm"] = origin + along * ((sum(tips.values(), Vector()) / max(len(tips), 1) - origin).dot(along) * 0.45)
        found = _contacts(arm, pts, radius=0.4)
        if found:
            report["contacts"] = found
            for label, c in found.items():
                if c["inside"]:
                    warnings.append(f"{label} is {c['distance_cm']} cm INSIDE {c['prop']}")
    report["warnings"] = warnings
    return report


def _rot_from_rest(pb):
    """The pose bone's own rotation away from its rest orientation, in degrees."""
    q = pb.matrix_basis.to_quaternion()
    return _r(math.degrees(q.angle), 0), [_r(math.degrees(a), 0) for a in q.to_euler("XYZ")]


def pose_report(focus=None, frame=None, contacts=True):
    """Read the current pose back in words and centimetres instead of matrices.

    focus=None: every armature, every bone (parent, rotation away from rest, world
    head/tail) plus the hands found. focus=<hand bone>: that hand in its own frame
    -- each finger's joint bends (positive = towards the palm, NEGATIVE = bent
    backwards), curl 0..1, spread, thumb tip position and distances to the other
    fingertips, where the palm faces, contacts with props, and warnings.
    focus=<other bone>: that bone and everything under it. frame: evaluate at that
    frame (the scene's current frame is restored afterwards)."""
    s = bpy.context.scene
    keep = s.frame_current
    if frame is not None:
        s.frame_set(int(frame))
    try:
        bpy.context.view_layer.update()
        out = {"frame": s.frame_current, "units": "cm (1 Blender unit = 1 m)"}
        if focus is not None:
            arm, pb = find_bone(focus)
            if is_hand(pb):
                out.update(_hand_report(pb.name, contacts))
                return out
            bones = _subtree(pb)
        else:
            bones = [b for arm in armatures() for b in arm.pose.bones]
            out["hands"] = hands()
        rows = []
        for b in bones:
            arm = b.id_data
            angle, euler = _rot_from_rest(b)
            row = {"bone": b.name, "parent": b.parent.name if b.parent else None,
                   "head": [_r(c, 3) for c in arm.matrix_world @ Vector(b.head)],
                   "tail": [_r(c, 3) for c in arm.matrix_world @ Vector(b.tail)]}
            if angle >= 1:
                row["rotated_deg"], row["euler_xyz_deg"] = angle, euler
            rows.append(row)
        out["bones"] = rows
        if not armatures():
            out["note"] = "the scene has no armature"
        return out
    finally:
        if frame is not None:
            s.frame_set(keep)


# ------------------------------------------------- Unreal groom (Alembic) --
# The Groom Exporter add-on (blender_addons/GroomExporter, with a Linux-built
# PyAlembic next to it) writes hair as an Unreal Groom: strands + guides + root UVs
# under the `groom_*` names UE reads. Blender's own abc export drops all of that, so
# hair for Unreal leaves ONLY through export_to(fmt="groom"). Loaded lazily: it costs
# 55 MB of libraries, and most scenes never have hair.
ADDONS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "blender_addons")
_GROOM = {}
def _groom():
    """(GroomExporter package, groom_precheck module), registered once per Blender."""
    if not _GROOM:
        if ADDONS_DIR not in sys.path:
            sys.path.insert(0, ADDONS_DIR)
        try:
            import GroomExporter
            import groom_precheck
        except Exception as exc:  # missing libs / wrong Blender python
            raise RuntimeError("the Groom Exporter add-on can't load (PyAlembic is built for Blender's "
                               f"Python 3.13 on Linux; scripts/build_pyalembic.sh): {exc!r}")
        if not hasattr(GroomExporter.AlembicGroomExporter, "alembic"):
            raise RuntimeError("the Groom Exporter add-on loaded without PyAlembic (libs missing in "
                               "blender_addons/GroomExporter/PyAlembic)")
        GroomExporter.register()
        _GROOM.update(addon=GroomExporter, check=groom_precheck)
    return _GROOM["addon"], _GROOM["check"]


def groom_curves():
    """The hair that gets exported: Curves objects that are shown. A hidden one (render-hidden or
    hidden in the viewport) is a helper -- e.g. the `*_GuideRef` object that shares the hair's curves
    data to feed its guides in -- and would otherwise leave as an extra, broken group."""
    vl = bpy.context.view_layer
    return [o for o in objects() if o.type == "CURVES" and not o.hide_render and not (o.name in vl.objects and o.hide_get())]


def groom_map_attributes():
    """Fill each hair object's add-on mapping with the guide's default attribute names
    (only the empty fields whose attribute exists); returns {object: {field: name}}.
    The logic lives in groom_precheck.map_attributes, shared with the bridge add-on."""
    _, chk = _groom()
    return chk.map_attributes(groom_curves())


def groom_check(names=None):
    """Pre-export check of the hair objects (all, or the named ones): per object `ok`,
    `info` and `issues` (a missing guide attribute, 1-point curves, UV out of 0..1, strands
    pointing at far-away guides ...). Changes nothing. Fix every issue before export_to(fmt="groom")."""
    _, chk = _groom()
    groom_map_attributes()
    obs = [o for o in groom_curves() if not names or o.name in names]
    out = chk.report(obs)
    for r in out:
        if r["object"] not in bpy.context.view_layer.objects:
            # Blender only evaluates objects of the active view layer: with the collection excluded the
            # modifiers never run, so the add-on would see the raw strands and none of the guides.
            r["ok"] = False
            r["issues"].insert(0, "the object's collection is excluded from the active view layer, so its "
                                  "modifiers don't run (Blender evaluates only the view layer's objects) -- "
                                  "enable the collection (layer_collection.exclude = False) before exporting")
    return out


def groom_export(path, scale=None, width_scale=True, radius_to_diameter=True):
    """Write an Unreal Groom .abc of every hair object. `scale` is the multiplier to
    centimetres (default: from the scene unit scale, so metres -> 100, a 0.01-scale scene -> 1)."""
    _groom()
    obs = groom_curves()
    if not obs:
        raise RuntimeError("no hair: the scene has no Curves object (Add > Curves > Empty Hair, or import a groom)")
    groom_map_attributes()
    bad = [f"{r['object']}: " + "; ".join(r["issues"]) for r in groom_check() if r["issues"]]
    if bad:
        raise RuntimeError("fix these before a groom export (groom_check() shows details):\n" + "\n".join(bad))
    if scale is None:
        scale = bpy.context.scene.unit_settings.scale_length * 100.0
    if bpy.context.object is not None and bpy.context.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    for o in bpy.context.scene.objects:
        o.select_set(o in obs)
    bpy.context.view_layer.objects.active = obs[0]
    res = bpy.ops.groom.buttonexport(filepath=path, groom_scale=float(scale), groom_width_scale=bool(width_scale),
                                     groom_radius_to_diameter=bool(radius_to_diameter), groom_animation=False)
    if "FINISHED" not in res:
        raise RuntimeError(f"groom export returned {res}")
    return path


def export_to(path, fmt="glb"):
    """Export all content (not the helper camera/lights) as glb | fbx | obj | stl | abc,
    or `groom` = the hair as an Unreal Groom .abc (see groom_check)."""
    fmt = fmt.lower()
    if fmt == "groom":
        return groom_export(path)
    if not meshes() and not any(o.type == "CURVES" for o in objects()):
        raise RuntimeError("nothing to export: the scene has no mesh or hair")
    for o in bpy.context.scene.objects:
        o.select_set(not o.get(AUX))
    sc = bpy.context.scene
    if fmt == "glb":
        bpy.ops.export_scene.gltf(filepath=path, export_format="GLB", use_selection=True, export_apply=True, export_animations=True)
    elif fmt == "fbx":
        bpy.ops.export_scene.fbx(filepath=path, use_selection=True, add_leaf_bones=False, bake_anim=True)
    elif fmt == "obj":
        bpy.ops.wm.obj_export(filepath=path, export_selected_objects=True, apply_modifiers=True)
    elif fmt == "stl":
        bpy.ops.wm.stl_export(filepath=path, export_selected_objects=True, apply_modifiers=True)
    elif fmt == "abc":
        # The scene's frame range, hair included -- an Alembic is a per-frame cache.
        bpy.ops.wm.alembic_export(filepath=path, selected=True, start=sc.frame_start, end=sc.frame_end, export_hair=True)
    else:
        raise ValueError("format must be glb, fbx, obj, stl, abc or groom")
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
