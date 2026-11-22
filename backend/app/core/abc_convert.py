"""Alembic (.abc) -> GLB through a background Blender, for viewing.

An Alembic file is a baked cache (meshes, hair/fur curves, points, per frame),
not a model: no rig, no materials. The viewer gets its FIRST frame as a static
GLB (core/blender_abc_to_glb.py explains the hair handling). The original is
stored untouched.
"""

from pathlib import Path

from app.core.blender_convert import run_converter

ABC_MIME = "model/alembic"
_SCRIPT = Path(__file__).with_name("blender_abc_to_glb.py")
# Only the modern "Ogawa" container; the old HDF5 one shares its signature with
# every other HDF5 file, so it isn't claimed.
_OGAWA_MAGIC = b"Ogawa\xff"


def is_abc(data: bytes) -> bool:
    return data.startswith(_OGAWA_MAGIC)


def abc_to_glb(data: bytes, in_place: bool = False) -> tuple[bytes, dict]:
    """(GLB bytes, info). `in_place` has no meaning here and is ignored."""
    return run_converter(_SCRIPT, data, ".abc", "Alembic file")
