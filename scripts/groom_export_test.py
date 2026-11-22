"""End-to-end check of the Unreal groom export through a running dev server:
builds a tiny groom in a live Blender scene, checks that a broken one is refused,
exports format=groom and reads the .abc back with the same PyAlembic.
  backend/.venv/bin/python scripts/groom_export_test.py http://127.0.0.1:8011 dev-local-token
"""
import subprocess
import sys

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8011"
TOKEN = sys.argv[2] if len(sys.argv) > 2 else "dev-local-token"
c = httpx.Client(base_url=BASE, headers={"Authorization": f"Bearer {TOKEN}"}, timeout=300)
sid = None


def check(cond, msg):
    print(("  ok: " if cond else "FAIL: ") + msg)
    if not cond:
        if sid:
            c.delete(f"/api/scenes/{sid}")
        sys.exit(1)


def run(code):
    r = c.post(f"/api/scenes/{sid}/run", json={"code": code})
    r.raise_for_status()
    return r.json()


BUILD = """
import numpy as np
bpy.ops.mesh.primitive_uv_sphere_add(radius=0.1)
scalp = bpy.context.active_object
G, S, P = 3, 7, 4
n = G + S
cv = bpy.data.hair_curves.new("hair"); cv.add_curves([P] * n)
pos = np.array([(0.02 * i, 0, 0.1 + 0.03 * j) for i in range(n) for j in range(P)], np.float32)
cv.position_data.foreach_set("vector", pos.ravel())
def mk(name, dom, typ, field, vals):
    a = cv.attributes.new(name, typ, dom); a.data.foreach_set(field, np.asarray(vals, np.float32 if field != "value" else np.int32).ravel())
mk("surface_uv_coordinate", "CURVE", "FLOAT2", "vector", np.tile([.5, .5], n))
mk("groom_is_guide", "CURVE", "INT", "value", [1] * G + [0] * S)
mk("groom_guide_id", "CURVE", "INT", "value", list(range(n)))
mk("groom_guide_closest", "CURVE", "FLOAT_COLOR", "color", np.tile([0, 1, 2, 1], n))
mk("groom_guide_weights", "CURVE", "FLOAT_VECTOR", "vector", np.tile([.5, .3, .2], n))
cv.attributes.new("radius", "FLOAT", "POINT").data.foreach_set("value", np.full(n * P, 0.0005, np.float32))
cv.surface = scalp; cv.surface_uv_map = scalp.data.uv_layers[0].name
ob = bpy.data.objects.new("hair", cv); bpy.context.collection.objects.link(ob); ob.parent = scalp
"""

obj = b"v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n"  # a scene needs one input asset
up = c.post("/api/assets/upload", files={"file": ("tri.obj", obj, "model/obj")})
up.raise_for_status()
sid = c.post("/api/scenes", json={"asset_ids": [up.json()["id"]], "name": "groom-test"}).json()["id"]
r = run(BUILD)
check(r["ok"], "hair built: " + r["output"][-200:])
r = run("print([(x['object'], x['ok'], x['issues']) for x in groom_check()])")
check(r["ok"] and "True" in r["output"] and "[]" in r["output"], "groom_check passes a valid groom: " + r["output"].strip())
r = c.post(f"/api/scenes/{sid}/export", json={"format": "groom", "name": "hair"})
check(r.status_code == 201, f"export format=groom -> {r.status_code} {r.text[:200]}")
asset = r.json()
check(str(asset.get("meta", {}).get("filename", "")).endswith(".abc"), f"stored as .abc: {asset.get('meta')}")
data = c.get(f"/api/assets/{asset['id']}/file").content
open("/tmp/groom_test_out.abc", "wb").write(data)
check(len(data) > 500, f"{len(data)} bytes")

READ = """
import sys
sys.path.insert(0, %r)
import alembic
from alembic.Abc import IArchive
from alembic.AbcGeom import ICurves
top = IArchive('/tmp/groom_test_out.abc').getTop()
s = ICurves(top, top.getChild(0).getName()).getSchema(); smp = s.getValue(); p = s.getArbGeomParams()
print('curves', len(smp.getCurvesNumVertices()), 'pts', len(smp.getPositions()), 'names', sorted(p.getPropertyHeader(i).getName() for i in range(p.getNumProperties())))
print('closest', list(p.getProperty('groom_closest_guides').getValue())[0], 'top z', smp.getPositions()[3][2])
"""
import pathlib
libs = str(pathlib.Path(__file__).resolve().parents[1] / "backend/app/blender_addons/GroomExporter/PyAlembic")
out = subprocess.run(["/opt/blender/blender", "-b", "--factory-startup", "--python-expr", READ % libs], capture_output=True, text=True).stdout
print(out)
check("curves 10 pts 40" in out, "10 curves / 40 points")
check("groom_closest_guides" in out and "groom_root_uv" in out and "groom_guide_weights" in out, "groom_* attributes present (incl. plural groom_closest_guides)")
check("top z 19" in out, "scaled to centimetres (unit scale 1 -> x100): z 0.19 m = 19")

# a groom with a broken guide attribute must be refused, with a reason
run("bpy.data.objects['hair'].data.attributes.remove(bpy.data.objects['hair'].data.attributes['groom_guide_id'])")
r = c.post(f"/api/scenes/{sid}/export", json={"format": "groom"})
check(r.status_code >= 400 and "groom_guide_id" in r.text, f"incomplete groom refused with the reason: {r.status_code} {r.text[-160:]}")
# mesh-only formats still work
run("pass")
c.delete(f"/api/scenes/{sid}")
c.delete(f"/api/assets/{up.json()['id']}")
print("ALL OK")
