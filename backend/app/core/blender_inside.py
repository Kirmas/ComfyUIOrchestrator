"""Runs INSIDE Blender (`blender -b -P blender_inside.py -- in.glb out.glb script.py`),
never imported by the app itself -- `bpy` only exists in Blender's own Python.

The contract native.blender_script promises its script (see BLENDER_SCRIPT_INSTRUCTION
in core/blender.py): the scene starts empty except for the input GLB, the
script edits it through `bpy`, and whatever is in the scene afterwards is
exported as the output GLB. Any exception makes Blender exit non-zero
(--python-exit-code 1), which the caller reports as the node's error.
"""
import sys
import traceback

import bpy

in_path, out_path, script_path = sys.argv[sys.argv.index("--") + 1 :][:3]

bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_scene.gltf(filepath=in_path)
imported = list(bpy.context.scene.objects)

with open(script_path, encoding="utf-8") as f:
    source = f.read()

try:
    exec(compile(source, "<node script>", "exec"), {"__name__": "__main__", "bpy": bpy, "imported": imported})
except Exception:
    traceback.print_exc()
    sys.exit(1)

if not any(o.type == "MESH" for o in bpy.context.scene.objects):
    print("BLENDER_SCRIPT_ERROR: the scene has no mesh left to export", file=sys.stderr)
    sys.exit(1)

bpy.ops.export_scene.gltf(filepath=out_path, export_format="GLB", use_selection=False, export_apply=True)
