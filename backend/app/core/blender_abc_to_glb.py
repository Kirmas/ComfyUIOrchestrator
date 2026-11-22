"""Runs INSIDE a background Blender (never imported by the API):

    blender -b --python blender_abc_to_glb.py -- in.abc out.glb info.json

Alembic -> GLB for viewing. An Alembic file is a baked cache of whatever a DCC
had: meshes, curves (hair/fur grooms), points (particles), per frame, with no
rig and no materials. This writes the FIRST frame, static:
  * meshes      -> triangles (loose edges -> lines, loose vertices -> points)
  * curves      -> ONE line-segment primitive named after the object
  * points      -> a points primitive
Blender's glTF exporter drops curves, so the file is written by core/glb_writer.py.

Hair: a groom can be hundreds of thousands of strands and the viewer wants a
recognisable hairstyle with a few percent of them, adjustable on the fly. So the
strands are (1) resampled to the SAME number of points each, (2) ordered so that
ANY PREFIX of the list is an even sample of the whole head, and (3) the primitive
carries {strands, strand_points, total_strands} in its glTF `extras`. The viewer
then just sets the draw range to the first k strands -- no re-fetch, no rebuild.
The order is Morton (Z-curve) order of the roots, then bit-reversed positions:
every prefix is then evenly spaced along the curve (van der Corput), which in
space means evenly spread over the scalp rather than every-Nth-in-file-order
(file order is often by region, and would bald one side).

Axes: Alembic has no unit/up-axis; Blender's importer yields Z-up, written Y-up
(x, z, -y) like every other format here. EXCEPT an Unreal groom from the Groom
Exporter add-on (signature "Groom Exporter" in its metadata): it is written the
way Unreal reads it -- Z up, Y negated -- while Blender's importer assumes Y-up,
so read naively the hair came out lying down and mirrored (2026-11-22, measured
with strands of known direction). `_UE_GROOM_TO_BLENDER` undoes both; the bridge
add-on applies the same fix when such a file is imported back into Blender.
"""
import importlib.util
import json
import sys
from pathlib import Path

import bpy
import numpy as np

# One colour per groom, so separate groups (main hair, fringe...) can be told apart.
# Strands are drawn TRANSLUCENT (alpha below): opaque 1-px lines of a dense groom
# overlap into a solid silhouette even at 2% and the hairstyle is lost; translucent,
# overlaps add up, so density reads as brightness and single strands, curls and the
# parting stay visible (checked on real grooms before choosing 0.2).
_HAIR_ALPHA = 0.2
_HAIR_COLORS = [[0.85, 0.68, 0.48], [0.45, 0.70, 1.00], [0.60, 0.90, 0.50],
                [0.95, 0.50, 0.70], [0.80, 0.60, 1.00], [1.00, 0.60, 0.35]]
_HAIR_COLOR = _HAIR_COLORS[0] + [1.0]  # lines that are not a groom (loose edges): opaque
_MESH_COLOR = [0.75, 0.75, 0.78, 1.0]
_POINT_COLOR = [1.0, 0.85, 0.3, 1.0]
_SEND_ALL_UP_TO = 4000      # strands: below this nothing is dropped
_SEND_FRACTION = 0.25       # above it, send at most this share (the slider tops out there)
_SEND_MAX = 60000
_Z_UP_TO_Y_UP = np.array([[1, 0, 0], [0, 0, 1], [0, -1, 0]], dtype=np.float32)
# Blender-imported point of a Groom Exporter file -> the point it was in Blender: the importer
# mapped the stored (x, -y, z) to (x, -z, -y), so this is (bx, -bz, -by). A reflection (det -1).
_UE_GROOM_TO_BLENDER = np.array([[1, 0, 0], [0, 0, -1], [0, -1, 0]], dtype=np.float32)
GROOM_EXPORTER_SIGNATURE = b"Groom Exporter"
_TO_VIEW = _Z_UP_TO_Y_UP


def _glb_writer():
    spec = importlib.util.spec_from_file_location("glb_writer", Path(__file__).with_name("glb_writer.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _world(points: np.ndarray, obj) -> np.ndarray:
    """Object-space points -> world, then Z-up -> Y-up."""
    m = np.array(obj.matrix_world, dtype=np.float32)
    world = points @ m[:3, :3].T + m[:3, 3]
    return (world @ _TO_VIEW.T).astype(np.float32)


def _spread_order(roots: np.ndarray) -> np.ndarray:
    """Strand indices ordered so every prefix is an even sample over space."""
    n = len(roots)
    lo, hi = roots.min(axis=0), roots.max(axis=0)
    q = ((roots - lo) / np.maximum(hi - lo, 1e-9) * 1023).astype(np.uint64)

    def spread_bits(x):
        x = x & 0x3FF
        x = (x | (x << 16)) & 0x30000FF
        x = (x | (x << 8)) & 0x300F00F
        x = (x | (x << 4)) & 0x30C30C3
        return (x | (x << 2)) & 0x9249249

    morton = spread_bits(q[:, 0]) | (spread_bits(q[:, 1]) << 1) | (spread_bits(q[:, 2]) << 2)
    along_curve = np.argsort(morton, kind="stable")
    bits = max(1, (n - 1).bit_length())
    position = np.arange(n, dtype=np.uint64)
    reversed_bits = np.zeros(n, dtype=np.uint64)
    for b in range(bits):
        reversed_bits |= ((position >> np.uint64(b)) & np.uint64(1)) << np.uint64(bits - 1 - b)
    return along_curve[np.argsort(reversed_bits, kind="stable")]


def _hair_primitive(obj, depsgraph, color) -> tuple[dict, dict] | None:
    curves = obj.evaluated_get(depsgraph).data
    n_total = len(curves.curves)
    if n_total == 0:
        return None
    positions = np.empty(len(curves.points) * 3, dtype=np.float32)
    curves.attributes["position"].data.foreach_get("vector", positions)
    positions = positions.reshape(-1, 3)
    data = curves.curve_offset_data  # a collection of ints, n_total + 1 of them
    offsets = np.empty(len(data), dtype=np.int32)
    data.foreach_get("value", offsets)
    offsets = offsets.astype(np.int64)
    lengths = np.diff(offsets)
    usable = np.nonzero(lengths >= 2)[0]
    if len(usable) == 0:
        return None
    roots = _world(positions[offsets[usable]], obj)
    kept = len(usable) if len(usable) <= _SEND_ALL_UP_TO else min(max(int(len(usable) * _SEND_FRACTION), _SEND_ALL_UP_TO), _SEND_MAX)
    chosen = usable[_spread_order(roots)[:kept]]

    per = int(np.clip(np.median(lengths[chosen]), 4, 24))
    n = lengths[chosen]
    u = np.linspace(0.0, 1.0, per)[None, :] * (n - 1)[:, None]
    i0 = np.minimum(np.floor(u).astype(np.int64), (n - 2)[:, None])
    frac = (u - i0).astype(np.float32)
    base = offsets[chosen][:, None] + i0
    pts = positions[base] + (positions[base + 1] - positions[base]) * frac[..., None]
    world = _world(pts.reshape(-1, 3), obj)

    segment = np.arange(per - 1, dtype=np.uint32)
    strand_base = (np.arange(kept, dtype=np.uint32) * per)[:, None]
    indices = np.stack([strand_base + segment, strand_base + segment + 1], axis=2).reshape(-1)
    extras = {"strands": int(kept), "strand_points": per, "total_strands": int(len(usable))}
    prim = {"name": obj.name, "mode": 1, "positions": world, "indices": indices, "color": color + [_HAIR_ALPHA], "blend": True, "extras": extras}
    return prim, {"name": obj.name, "kind": "curves", "strands": int(len(usable)), "sent": int(kept), "vertices": int(len(world))}


def _vertex_normals(points: np.ndarray, tris: np.ndarray) -> np.ndarray:
    a, b, c = points[tris[:, 0]], points[tris[:, 1]], points[tris[:, 2]]
    face = np.cross(b - a, c - a)
    normals = np.zeros_like(points)
    for k in range(3):
        np.add.at(normals, tris[:, k], face)
    length = np.linalg.norm(normals, axis=1, keepdims=True)
    return (normals / np.maximum(length, 1e-12)).astype(np.float32)


def _mesh_primitives(obj, depsgraph) -> tuple[list[dict], list[dict]]:
    evaluated = obj.evaluated_get(depsgraph)
    mesh = evaluated.to_mesh()
    try:
        nv = len(mesh.vertices)
        if nv == 0:
            return [], []
        points = np.empty(nv * 3, dtype=np.float32)
        mesh.vertices.foreach_get("co", points)
        world = _world(points.reshape(-1, 3), obj)
        mesh.calc_loop_triangles()
        tris = np.empty(len(mesh.loop_triangles) * 3, dtype=np.uint32)
        mesh.loop_triangles.foreach_get("vertices", tris)
        tris = tris.reshape(-1, 3)
        edges = np.empty(len(mesh.edges) * 2, dtype=np.uint32)
        mesh.edges.foreach_get("vertices", edges)
        edges = edges.reshape(-1, 2)
        prims, infos = [], []
        if len(tris):
            prims.append({"name": obj.name, "mode": 4, "positions": world, "normals": _vertex_normals(world, tris),
                          "indices": tris.reshape(-1), "color": _MESH_COLOR})
            infos.append({"name": obj.name, "kind": "mesh", "vertices": nv, "triangles": int(len(tris))})
        elif len(edges):
            prims.append({"name": obj.name, "mode": 1, "positions": world, "indices": edges.reshape(-1), "color": _HAIR_COLOR})
            infos.append({"name": obj.name, "kind": "lines", "vertices": nv})
        else:
            prims.append({"name": obj.name, "mode": 0, "positions": world, "color": _POINT_COLOR})
            infos.append({"name": obj.name, "kind": "points", "vertices": nv})
        return prims, infos
    finally:
        evaluated.to_mesh_clear()


def main() -> None:
    rest = sys.argv[sys.argv.index("--") + 1:]
    src, dst, info_path = rest[:3]
    global _TO_VIEW
    ue_groom = GROOM_EXPORTER_SIGNATURE in Path(src).read_bytes()
    if ue_groom:
        _TO_VIEW = _Z_UP_TO_Y_UP @ _UE_GROOM_TO_BLENDER
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.wm.alembic_import(filepath=src, as_background_job=False, set_frame_range=True)
    scene = bpy.context.scene
    scene.frame_set(scene.frame_start)
    depsgraph = bpy.context.evaluated_depsgraph_get()

    prims, parts = [], []
    for obj in bpy.data.objects:
        if obj.type == "CURVES":
            result = _hair_primitive(obj, depsgraph, _HAIR_COLORS[sum(p["kind"] == "curves" for p in parts) % len(_HAIR_COLORS)])
            if result:
                prims.append(result[0])
                parts.append(result[1])
        elif obj.type == "MESH":
            p, i = _mesh_primitives(obj, depsgraph)
            prims += p
            parts += i
    if not prims:
        raise SystemExit("nothing drawable in this Alembic file")

    Path(dst).write_bytes(_glb_writer().write_glb(prims))
    fps = scene.render.fps / (scene.render.fps_base or 1.0)
    frames = scene.frame_end - scene.frame_start + 1
    info = {
        "clips": [], "fps": round(fps, 3), "bones": 0, "has_mesh": any(p["kind"] == "mesh" for p in parts),
        "root_motion": False,
        # Named like the FBX info so the viewer's part dropdown works unchanged:
        # a head and its hair can be shown or hidden separately.
        "meshes": [{"name": p["name"], "vertices": p["vertices"]} for p in parts],
        "parts": parts,
        "source": {"format": "alembic", "frames": int(frames), "frame_start": int(scene.frame_start),
                   "frame_end": int(scene.frame_end), "static_first_frame": frames > 1,
                   "unreal_groom": ue_groom},
    }
    Path(info_path).write_text(json.dumps(info))


main()
