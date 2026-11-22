"""Orientation of Unreal grooms (Groom Exporter .abc) -- runs INSIDE headless Blender:

    BLENDER_USER_SCRIPTS=/tmp/orch_bt/scripts BLENDER_USER_CONFIG=/tmp/orch_bt/config \
        /opt/blender/blender -b --factory-startup --python-exit-code 1 --python scripts/groom_axes_test.py

(bridge add-on symlinked as in bridge_blender_test.py). The add-on stores points the way Unreal
reads them (Z up, Y negated); Blender's Alembic importer assumes Y-up, so naively the hair came
out lying down and mirrored. Strands of known direction are exported, then checked through
(1) the orchestrator's viewer converter (Y-up GLB = Blender (x, z, -y)) and (2) the bridge
add-on's import back into Blender (original coordinates, cm -> scene units).
"""
import bpy, addon_utils, sys, os, json, struct, shutil, subprocess, tempfile
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADD = os.path.join(ROOT, "backend/app/blender_addons")
sys.path.insert(0, ADD)
import GroomExporter, groom_precheck
GroomExporter.register()
addon_utils.enable("orchestrator_bridge", default_set=True)
import orchestrator_bridge as ob_

for o in list(bpy.data.objects): bpy.data.objects.remove(o)
bpy.ops.mesh.primitive_uv_sphere_add(radius=0.1); scalp = bpy.context.active_object
n, P = 4, 3
cv = bpy.data.hair_curves.new("hair"); cv.add_curves([P] * n)
pos = [(x, y, 0.1 * j) for (x, y) in [(0.1, 0), (0.2, 0), (0, 0.1), (0, 0.2)] for j in range(P)]
cv.position_data.foreach_set("vector", np.array(pos, np.float32).ravel())
def mk(name, dom, typ, field, vals):
    a = cv.attributes.new(name, typ, dom); a.data.foreach_set(field, np.asarray(vals, np.float32 if field != "value" else np.int32).ravel())
mk("surface_uv_coordinate", "CURVE", "FLOAT2", "vector", np.tile([.5, .5], n))
mk("groom_is_guide", "CURVE", "INT", "value", [1] * n)
mk("groom_guide_id", "CURVE", "INT", "value", list(range(n)))
cv.attributes.new("radius", "FLOAT", "POINT").data.foreach_set("value", np.full(n * P, 0.0005, np.float32))
cv.surface = scalp; cv.surface_uv_map = scalp.data.uv_layers[0].name
hair = bpy.data.objects.new("hair", cv); bpy.context.collection.objects.link(hair); hair.parent = scalp
groom_precheck.map_attributes([hair])
for o in bpy.data.objects: o.select_set(o is hair)
bpy.context.view_layer.objects.active = hair
abc = os.path.join(tempfile.mkdtemp(), "axis.abc")
bpy.ops.groom.buttonexport(filepath=abc, groom_scale=100.0, groom_animation=False)
tips = np.array(pos)[P - 1::P]

# (1) viewer converter, run with the backend's python like the server does
code = ("import sys,json,struct,numpy as np; sys.path.insert(0, %r)\n"
        "from app.core.abc_convert import abc_to_glb\n"
        "glb, info = abc_to_glb(open(%r,'rb').read()); n=struct.unpack('<I',glb[12:16])[0]; j=json.loads(glb[20:20+n])\n"
        "a=j['accessors'][j['meshes'][0]['primitives'][0]['attributes']['POSITION']]; bv=j['bufferViews'][a['bufferView']]\n"
        "o=20+n+8+bv.get('byteOffset',0)+a.get('byteOffset',0)\n"
        "print('PTS', json.dumps(np.frombuffer(glb[o:o+a['count']*12],dtype=np.float32).reshape(-1,3).tolist()), info['source']['unreal_groom'])\n"
        ) % (os.path.join(ROOT, "backend"), abc)
out = subprocess.run([os.path.join(ROOT, "backend/.venv/bin/python"), "-c", code], capture_output=True, text=True,
                     cwd=os.path.join(ROOT, "backend")).stdout
line = next(l for l in out.splitlines() if l.startswith("PTS"))
pts = np.array(json.loads(line.split(" ", 1)[1].rsplit(" ", 1)[0])) / 100.0
assert line.endswith("True"), "not recognised as an Unreal groom"
for t in tips:  # every tip, as glTF Y-up (x, z, -y), is among the viewer's points
    want = np.array([t[0], t[2], -t[1]])
    assert np.min(np.linalg.norm(pts - want, axis=1)) < 1e-3, (t, want)
print("viewer orientation OK")

# (2) the bridge add-on importing it back into Blender
ob_.S.key_file = os.path.join(tempfile.mkdtemp(), "k.json")
tmp = os.path.join(tempfile.mkdtemp(), "x.abc"); shutil.copy(abc, tmp)
ob_._import({"request_id": "r", "asset": {"id": "a1", "format": "abc", "kind": "mesh", "filename": "x.abc", "project_name": "t"}}, tmp)
imp = [o for o in bpy.data.objects if o.get("orch_asset_id") == "a1" and o.type == "CURVES"][0]
bpy.context.view_layer.update()
m = np.array(imp.matrix_world)
p = np.empty(len(imp.data.points) * 3, np.float32); imp.data.points.foreach_get("position", p)
w = p.reshape(-1, 3) @ m[:3, :3].T + m[:3, 3]
assert np.allclose(w[P - 1::P], tips, atol=1e-4), w[P - 1::P]
print("add-on import orientation OK")
print("GROOM AXES OK")
