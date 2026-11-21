"""Runs INSIDE a background Blender (never imported by the API):

    blender -b --python blender_fbx_to_glb.py -- in.fbx out.glb info.json [--in-place]

FBX -> GLB for viewing, keeping what a game asset is made of: the mesh,
its skeleton and skinning, its materials/textures and EVERY animation clip.
Units and axes are not handled here on purpose: Blender's FBX importer
applies the file's unit scale and axes, and the glTF exporter writes
metres / Y-up, which is the general rule and needs no choice from the person.

A file with a skeleton but no mesh (animation-only, the usual Mixamo/Unreal
export) would export as nothing visible, so a thin skinned "stick" mesh is
built along the bones: it is bound to the same bones, so it plays the clips.

`info.json` carries what the page shows: clips (name, frames, seconds), bone
count, whether any clip moves the root horizontally (root motion), whether the
file had a mesh of its own, and which meshes it holds (a file can carry several
models; the viewer lets the person pick which to show). `--in-place` writes a second rendition with that
horizontal travel taken out, so a walk cycle stays on the spot in the viewer.
"""
import json
import sys

import bpy
from mathutils import Vector

ROOT_MOTION_EPS_M = 0.05  # horizontal travel (metres) below which a clip is "in place"
STICK_RADIUS_M = 0.012


def _args() -> tuple[str, str, str, bool]:
    rest = sys.argv[sys.argv.index("--") + 1:]
    flags = [a for a in rest if a.startswith("--")]
    pos = [a for a in rest if not a.startswith("--")]
    return pos[0], pos[1], pos[2], "--in-place" in flags


def _fps() -> float:
    r = bpy.context.scene.render
    return r.fps / (r.fps_base or 1.0)


def _action_fcurves(action):
    """All fcurves of an action, across the slotted-action layout of Blender 4.4+."""
    try:
        for layer in action.layers:
            for strip in layer.strips:
                for bag in strip.channelbags:
                    yield from bag.fcurves
    except AttributeError:  # pre-slot actions
        yield from action.fcurves


def _root_bone_axes(arm_obj) -> dict[str, list[bool]]:
    """For each ROOT bone: which of its local location axes run horizontally in
    the world. Bone axes are rolled differently per rig, so this is read from the
    bone's own rest orientation rather than assumed to be x,y."""
    out = {}
    world = arm_obj.matrix_world.to_3x3()
    for bone in arm_obj.data.bones:
        if bone.parent is None:
            basis = world @ bone.matrix_local.to_3x3()
            out[bone.name] = [abs((basis @ Vector(axis)).z) < 0.7071 for axis in ((1, 0, 0), (0, 1, 0), (0, 0, 1))]
    return out


def _root_location_curves(arm_obj, action):
    """(fcurve, is_horizontal) for the location curves of the root that carries motion:
    the armature object itself, and each root bone."""
    bone_axes = _root_bone_axes(arm_obj)
    for fc in _action_fcurves(action):
        if not fc.data_path.endswith("location") or fc.array_index > 2:
            continue
        if fc.data_path == "location":  # the object's own translation: world axes
            yield fc, fc.array_index < 2
        elif fc.data_path.startswith('pose.bones["'):
            name = fc.data_path[len('pose.bones["'):fc.data_path.index('"]')]
            if name in bone_axes:
                yield fc, bone_axes[name][fc.array_index]


def _travel(fc) -> float:
    values = [kp.co[1] for kp in fc.keyframe_points]
    return (max(values) - min(values)) if values else 0.0


def _flatten(fc) -> None:
    kps = fc.keyframe_points
    if not len(kps):
        return
    first = kps[0].co[1]
    for kp in kps:
        kp.co[1] = first
        kp.handle_left[1] = first
        kp.handle_right[1] = first
    fc.update()


def _stick_mesh(arm_obj):
    """A thin skinned prism per bone, weighted 100% to that bone."""
    import bmesh

    # An imported skeleton with no mesh has its bones marked non-deforming, and
    # the glTF exporter drops the skin of a mesh that weights none of them.
    for bone in arm_obj.data.bones:
        bone.use_deform = True
    mesh = bpy.data.meshes.new("skeleton_sticks")
    bm = bmesh.new()
    groups: list[tuple[str, list[int]]] = []
    world = arm_obj.matrix_world
    for bone in arm_obj.data.bones:
        head, tail = world @ bone.head_local, world @ bone.tail_local
        axis = tail - head
        length = axis.length or 0.01
        axis.normalize()
        side = axis.cross(Vector((0, 0, 1)))
        if side.length < 1e-4:
            side = axis.cross(Vector((1, 0, 0)))
        side.normalize()
        up = axis.cross(side)
        r = max(STICK_RADIUS_M, length * 0.04)
        ring = [side * r + up * r, -side * r + up * r, -side * r - up * r, side * r - up * r]
        lo = [bm.verts.new(head + p) for p in ring]
        hi = [bm.verts.new(tail + p * 0.4) for p in ring]
        for i in range(4):
            j = (i + 1) % 4
            bm.faces.new((lo[i], lo[j], hi[j], hi[i]))
        bm.faces.new(lo[::-1])
        bm.faces.new(hi)
        groups.append((bone.name, list(range(len(bm.verts) - 8, len(bm.verts)))))  # a new BMVert's .index is -1 until index_update()
    bm.to_mesh(mesh)
    bm.free()
    obj = bpy.data.objects.new("skeleton_sticks", mesh)
    bpy.context.scene.collection.objects.link(obj)
    for name, idx in groups:
        vg = obj.vertex_groups.new(name=name)
        vg.add(idx, 1.0, "REPLACE")
    mod = obj.modifiers.new("Armature", "ARMATURE")
    mod.object = arm_obj
    obj.parent = arm_obj
    obj.matrix_parent_inverse = arm_obj.matrix_world.inverted()
    return obj


def _all_actions_as_clips(arm_obj) -> None:
    """The exporter writes the assigned action only; stacking every action as its
    own NLA track makes each one a clip, named after the action."""
    ad = arm_obj.animation_data or arm_obj.animation_data_create()
    ad.action = None
    for action in list(bpy.data.actions):
        if not any(True for _ in _action_fcurves(action)):
            continue
        track = ad.nla_tracks.new()
        track.name = action.name
        track.strips.new(action.name, int(action.frame_range[0]), action)


def main() -> None:
    src, dst, info_path, in_place = _args()
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.wm.fbx_import(filepath=src)

    armatures = [o for o in bpy.data.objects if o.type == "ARMATURE"]
    meshes = [{"name": o.name, "vertices": len(o.data.vertices)} for o in bpy.data.objects if o.type == "MESH"]
    had_mesh = bool(meshes)
    fps = _fps()
    clips, root_motion = [], False
    if armatures:
        arm = armatures[0]
        for action in bpy.data.actions:
            lo, hi = action.frame_range
            if not any(True for _ in _action_fcurves(action)):
                continue
            moves = any(h and _travel(fc) > ROOT_MOTION_EPS_M for fc, h in _root_location_curves(arm, action))
            root_motion = root_motion or moves
            clips.append({"name": action.name, "frames": int(round(hi - lo)) + 1,
                          "seconds": round((hi - lo) / fps, 3), "root_motion": moves})
            if in_place:
                for fc, horizontal in _root_location_curves(arm, action):
                    if horizontal:
                        _flatten(fc)
        if not had_mesh:
            _stick_mesh(arm)
        _all_actions_as_clips(arm)

    bpy.ops.export_scene.gltf(
        filepath=dst, export_format="GLB", export_animations=bool(clips),
        export_animation_mode="NLA_TRACKS" if clips else "ACTIONS",
        export_skins=True, export_materials="EXPORT", export_yup=True,
        export_apply=False)

    with open(info_path, "w") as fh:
        json.dump({"clips": clips, "fps": round(fps, 3), "bones": sum(len(a.data.bones) for a in armatures),
                   "has_mesh": had_mesh, "meshes": meshes, "root_motion": root_motion}, fh)


main()
