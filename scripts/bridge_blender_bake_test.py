"""Computed-material bake for the Blender add-on's GLB export (runs INSIDE headless Blender).

    BLENDER_USER_SCRIPTS=/tmp/orch_bt/scripts BLENDER_USER_CONFIG=/tmp/orch_bt/config \
        /opt/blender/blender -b --factory-startup --python-exit-code 1 --python scripts/bridge_blender_bake_test.py

(with the add-on symlinked into /tmp/orch_bt/scripts/addons, see bridge_blender_test.py). A
two-material plane whose first material computes Base Color (mix of a colour and an image by UV),
Roughness (UV.y), and its normal (noise -> Bump): the GLB must carry baseColor/metallicRoughness/
normal textures with the right values, and the shader graph must be exactly as before afterwards.
"""
import bpy, addon_utils, json, struct, tempfile, os, time
addon_utils.enable("orchestrator_bridge", default_set=True)
import orchestrator_bridge as ob_
for o in list(bpy.data.objects): bpy.data.objects.remove(o)
bpy.ops.mesh.primitive_plane_add(size=2); ob = bpy.context.object
bpy.ops.object.mode_set(mode="EDIT"); bpy.ops.mesh.subdivide(number_cuts=8); bpy.ops.object.mode_set(mode="OBJECT")
def mat_proc():
    m = bpy.data.materials.new("Proc"); m.use_nodes = True; nt = m.node_tree
    b = nt.nodes["Principled BSDF"]
    uv = nt.nodes.new("ShaderNodeTexCoord"); sep = nt.nodes.new("ShaderNodeSeparateXYZ")
    nt.links.new(uv.outputs["UV"], sep.inputs[0])
    mix = nt.nodes.new("ShaderNodeMix"); mix.data_type = "RGBA"
    mix.inputs[6].default_value = (1, 0, 0, 1); mix.inputs[7].default_value = (0, 1, 0, 1)
    gt = nt.nodes.new("ShaderNodeMath"); gt.operation = "GREATER_THAN"; gt.inputs[1].default_value = 0.5
    nt.links.new(sep.outputs["X"], gt.inputs[0]); nt.links.new(gt.outputs[0], mix.inputs["Factor"])
    nt.links.new(mix.outputs[2], b.inputs["Base Color"])
    nt.links.new(sep.outputs["Y"], b.inputs["Roughness"])
    b.inputs["Metallic"].default_value = 1.0
    noise = nt.nodes.new("ShaderNodeTexNoise"); bump = nt.nodes.new("ShaderNodeBump")
    nt.links.new(noise.outputs["Fac"], bump.inputs["Height"]); nt.links.new(bump.outputs["Normal"], b.inputs["Normal"])
    # a decoy image upstream, like the masks texture
    img = bpy.data.images.new("Masks", 64, 64); img.pixels = [0.2, 0.4, 1.0, 1.0] * 64 * 64
    t = nt.nodes.new("ShaderNodeTexImage"); t.image = img; nt.links.new(t.outputs["Color"], mix.inputs["B"] if "B" in mix.inputs else mix.inputs[7])
    return m
m1 = mat_proc(); m2 = bpy.data.materials.new("Plain"); m2.use_nodes = True
ob.data.materials.append(m1); ob.data.materials.append(m2)
for i, p in enumerate(ob.data.polygons): p.material_index = 1 if p.center.y > 0.8 else 0
links_before = len(m1.node_tree.links); nodes_before = len(m1.node_tree.nodes); imgs_before = len(bpy.data.images)
warn = []
t0 = time.time(); undo = ob_._bake_surface_for_export([ob], warn); print("bake s", round(time.time()-t0, 1), "warnings", warn)
path = os.path.join(tempfile.mkdtemp(), "t.glb")
for o in bpy.data.objects: o.select_set(o is ob)
bpy.ops.export_scene.gltf(filepath=path, export_format="GLB", use_selection=True, use_active_scene=True)
undo()
assert (len(m1.node_tree.links), len(m1.node_tree.nodes), len(bpy.data.images)) == (links_before, nodes_before, imgs_before), "graph not restored"
data = open(path, "rb").read(); n = struct.unpack("<I", data[12:16])[0]; j = json.loads(data[20:20+n])
mat = next(x for x in j["materials"] if x["name"] == "Proc")
pbr = mat["pbrMetallicRoughness"]
print("Proc keys:", sorted(pbr), "normal:", "normalTexture" in mat)
assert "baseColorTexture" in pbr and "metallicRoughnessTexture" in pbr and "normalTexture" in mat
# read back the base colour image
binstart = 20 + n + 8
tex = j["textures"][pbr["baseColorTexture"]["index"]]; im = j["images"][tex["source"]]; bv = j["bufferViews"][im["bufferView"]]
png = data[binstart + bv.get("byteOffset", 0): binstart + bv.get("byteOffset", 0) + bv["byteLength"]]
f = os.path.join(tempfile.mkdtemp(), "b.png"); open(f, "wb").write(png)
img = bpy.data.images.load(f); w, h = img.size; px = list(img.pixels)
def at(u, v):
    i = (int(v*h)*w + int(u*w))*4; return [round(c, 2) for c in px[i:i+3]]
print("left", at(0.25, 0.3), "right", at(0.75, 0.3))
l, r = at(0.25, 0.3), at(0.75, 0.3)
assert l[0] > 0.8 and l[1] < 0.2 and r[2] > 0.8 and r[0] < 0.2, (l, r)  # right half = the image fed into the mix
tex = j["textures"][pbr["metallicRoughnessTexture"]["index"]]; im = j["images"][tex["source"]]; bv = j["bufferViews"][im["bufferView"]]
png = data[binstart + bv.get("byteOffset", 0): binstart + bv.get("byteOffset", 0) + bv["byteLength"]]
f = os.path.join(tempfile.mkdtemp(), "mr.png"); open(f, "wb").write(png)
img = bpy.data.images.load(f); img.colorspace_settings.name = "Non-Color"; w, h = img.size; px = list(img.pixels)
lo, hi = at(0.25, 0.2), at(0.25, 0.6)
print("metal/rough at v=.2", lo, "v=.6", hi)
assert abs(lo[1] - 0.2) < 0.06 and abs(hi[1] - 0.6) < 0.06 and lo[2] > 0.95, (lo, hi)
print("SURFACE OK")
