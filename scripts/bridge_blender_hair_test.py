"""Hair send path of the Blender add-on (runs INSIDE headless Blender, add-on symlinked as in
bridge_blender_test.py): the export bakes must skip Curves objects (they crashed on uv_layers), and
a selection with hair goes out as a single-frame Alembic that Blender reads back as curves.
"""
import bpy, addon_utils, os, tempfile
addon_utils.enable("orchestrator_bridge", default_set=True)
import orchestrator_bridge as ob_
for o in list(bpy.data.objects): bpy.data.objects.remove(o)
bpy.ops.mesh.primitive_uv_sphere_add(); scalp = bpy.context.object
c = bpy.data.hair_curves.new("Hair"); c.add_curves([4] * 50)
import random
for i, p in enumerate(c.points): p.position = (random.random(), random.random(), (i % 4) * 0.1)
hair = bpy.data.objects.new("Eshlyn_Hair", c); bpy.context.collection.objects.link(hair)
objs = [scalp, hair]
w = []; u1 = ob_._bake_glow_for_export(objs, w); u2 = ob_._bake_surface_for_export(objs, w); u2(); u1()
print("bakes ok, warnings", w)
for o in bpy.data.objects: o.select_set(o in objs)
path = os.path.join(tempfile.mkdtemp(), "h.abc")
f = bpy.context.scene.frame_current
bpy.ops.wm.alembic_export(filepath=path, selected=True, start=f, end=f, export_hair=True)
data = open(path, "rb").read()
print("abc bytes", len(data), "ogawa", data[:5] == b"Ogawa")
bpy.ops.wm.alembic_import(filepath=path)
print("reimported curves:", [o.name for o in bpy.data.objects if o.type == "CURVES" and o is not hair])
assert data[:5] == b"Ogawa" and any(o.type == "CURVES" and o is not hair for o in bpy.data.objects)
print("HAIR OK")
