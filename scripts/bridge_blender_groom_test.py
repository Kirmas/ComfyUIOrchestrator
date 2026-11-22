"""Groom path of the Blender bridge add-on (runs INSIDE headless Blender, add-on symlinked as in
bridge_blender_test.py). Loads the server copy of the Groom Exporter (gitignored, see CLAUDE.md).
Fallback (no node pipeline): mapping filled from defaults, selection restored, a broken groom refused.
The person's own node pipeline wins: a guide attribute under a name only the pipeline knows exports,
and the written .abc carries exactly the pipeline's mapping (checked with PyAlembic).
"""
import bpy, addon_utils, sys, os, tempfile
import numpy as np
ADD = "/home/keresh/comfy-orchestrator/backend/app/blender_addons"
sys.path.insert(0, ADD)
import GroomExporter, groom_precheck
GroomExporter.register()
addon_utils.enable("orchestrator_bridge", default_set=True)
import orchestrator_bridge as ob_
ob_.groom_precheck = groom_precheck  # the download zip ships it inside the add-on
for o in list(bpy.data.objects): bpy.data.objects.remove(o)
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
hair = bpy.data.objects.new("hair", cv); bpy.context.collection.objects.link(hair); hair.parent = scalp
for o in bpy.data.objects: o.select_set(True)  # hair + scalp selected, like a person would
print("ready:", ob_._groom_exporter_ready())
path = os.path.join(tempfile.mkdtemp(), "groom.abc")
sent = ob_._export_groom(bpy.context, [hair], path)
print("sent", [o.name for o in sent], "bytes", os.path.getsize(path), "selection restored:", sorted(o.name for o in bpy.context.selected_objects))
assert scalp.select_get() and hair.select_get()
print("mapped:", hair.GroomProperty.att_groom_guide, hair.GroomProperty.att_groom_root_uv)
open("/tmp/orch_bt/groom_out.abc", "wb").write(open(path, "rb").read())
# --- the person's own node pipeline wins: rename the guide attribute to something only it knows
assert ob_._pick_pipeline([hair]) is None
gi = cv.attributes["groom_is_guide"]; vals = np.empty(n, np.int32); gi.data.foreach_get("value", vals)
cv.attributes.remove(gi); a = cv.attributes.new("my_guides", "INT", "CURVE"); a.data.foreach_set("value", vals)
hair.GroomProperty.att_groom_guide = ""
tree = bpy.data.node_groups.new("Eshlyn - LIVE Export", "GroomExporterNode")
cn = tree.nodes.new("GroomCurveNodeType"); cn.curve_obj = hair
an = tree.nodes.new("GroomAttributeNodeType"); tree.links.new(cn.outputs["Groom"], an.inputs["Groom"]); an.update_attributes()
for i, v in {0: "surface_uv_coordinate", 3: "my_guides", 4: "groom_guide_id", 10: "radius"}.items(): an.att_list_atts[i].value = v
en = tree.nodes.new("GroomExportNodeType"); tree.links.new(an.outputs["Atts"], en.inputs["Atts"])
pipe = ob_._pick_pipeline([hair]); print("pipeline:", pipe[0].name if pipe else None)
scalp.select_set(True); hair.select_set(False)
p2 = os.path.join(tempfile.mkdtemp(), "pipe.abc")
sent = ob_._export_groom_pipeline(bpy.context, pipe[0], pipe[1], p2)
print("pipeline sent", [o.name for o in sent], "guide mapped to:", hair.GroomProperty.att_groom_guide, "selection:", sorted(o.name for o in bpy.context.selected_objects))
assert hair.GroomProperty.att_groom_guide == "my_guides" and scalp.select_get() and not hair.select_get()
open("/tmp/orch_bt/groom_pipe.abc", "wb").write(open(p2, "rb").read())
# read it back with the add-on's own PyAlembic (separate process, like groom_export_test.py)
import subprocess
READ = ("import sys; sys.path.insert(0, %r)\nfrom alembic.Abc import IArchive\nfrom alembic.AbcGeom import ICurves\n"
        "top = IArchive(%r).getTop(); s = ICurves(top, top.getChild(0).getName()).getSchema(); p = s.getArbGeomParams()\n"
        "print('ATTRS', sorted(p.getPropertyHeader(i).getName() for i in range(p.getNumProperties())))\n") % (ADD + "/GroomExporter/PyAlembic", p2)
out = subprocess.run([bpy.app.binary_path, "-b", "--factory-startup", "--python-expr", READ], capture_output=True, text=True).stdout
attrs = next(l for l in out.splitlines() if l.startswith("ATTRS"))
print("pipeline file:", attrs)
# exactly the pipeline's mapping: its custom guide attribute made it (the evaluated-copy trap), and
# what it left empty (closest guides / weights) did not leak in from an earlier export's mapping
assert "'groom_guide'" in attrs and "groom_closest_guides" not in attrs, attrs
# broken groom is refused with the reason (fallback path, no pipeline)
bpy.data.node_groups.remove(tree)
cv.attributes.remove(cv.attributes["groom_guide_id"]); hair.GroomProperty.att_groom_id = ""
assert ob_._pick_pipeline([hair]) is None
try:
    ob_._export_groom(bpy.context, [hair], path + "2"); raise SystemExit("FAIL: broken groom exported")
except ob_._GroomNotReady as exc:
    print("refused:", str(exc)[:120])
print("GROOM OK")
