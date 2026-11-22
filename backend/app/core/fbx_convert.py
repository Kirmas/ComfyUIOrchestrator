"""FBX -> GLB through a background Blender, for viewing (core/mesh_view.py).

The FBX itself is stored untouched; a game asset is a mesh, a skeleton, its
materials and a set of animation clips, and a GLB can carry all of those. The
work is done by core/blender_fbx_to_glb.py inside Blender (its FBX importer
already applies the file's units and axes; see that file for the rules).
"""

from pathlib import Path

from app.core.blender_convert import run_converter

FBX_MIME = "model/fbx"
_SCRIPT = Path(__file__).with_name("blender_fbx_to_glb.py")
_BINARY_MAGIC = b"Kaydara FBX Binary  \x00"


def is_fbx(data: bytes) -> bool:
    """By signature only: binary FBX's magic, or the `; FBX x.y project file`
    comment an ASCII FBX opens with."""
    return data.startswith(_BINARY_MAGIC) or data[:64].lstrip().startswith(b"; FBX")


def fbx_to_glb(data: bytes, in_place: bool = False) -> tuple[bytes, dict]:
    """(GLB bytes, info) -- info is {clips, fps, bones, has_mesh, root_motion, meshes}.
    `in_place` takes the root's horizontal travel out of every clip."""
    return run_converter(_SCRIPT, data, ".fbx", "FBX", ["--in-place"] if in_place else None)
