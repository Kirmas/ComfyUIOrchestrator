"""FBX -> GLB through a background Blender, for viewing (core/mesh_view.py).

The FBX itself is stored untouched; a game asset is a mesh, a skeleton, its
materials and a set of animation clips, and a GLB can carry all of those. The
work is done by core/blender_fbx_to_glb.py inside Blender (its FBX importer
already applies the file's units and axes; see that file for the rules). One
Blender at a time: it never hands memory back and a big FBX is hundreds of MB.
"""

import json
import subprocess
import tempfile
import threading
from pathlib import Path

from app.core.blender import find_blender_binary

FBX_MIME = "model/fbx"
_SCRIPT = Path(__file__).with_name("blender_fbx_to_glb.py")
_BINARY_MAGIC = b"Kaydara FBX Binary  \x00"
_TIMEOUT_S = 300
_ONE_AT_A_TIME = threading.Lock()


def is_fbx(data: bytes) -> bool:
    """By signature only: binary FBX's magic, or the `; FBX x.y project file`
    comment an ASCII FBX opens with."""
    return data.startswith(_BINARY_MAGIC) or data[:64].lstrip().startswith(b"; FBX")


def fbx_to_glb(data: bytes, in_place: bool = False) -> tuple[bytes, dict]:
    """(GLB bytes, info) -- info is {clips, fps, bones, has_mesh, root_motion}.
    `in_place` takes the root's horizontal travel out of every clip.
    Raises ValueError when there is no Blender or the file can't be read."""
    blender = find_blender_binary()
    if blender is None:
        raise ValueError("Blender is not installed, so an FBX can't be converted")
    with tempfile.TemporaryDirectory(prefix="fbx_") as tmp:
        work = Path(tmp)
        (work / "in.fbx").write_bytes(data)
        cmd = [blender, "-b", "--factory-startup", "--python", str(_SCRIPT), "--",
               str(work / "in.fbx"), str(work / "out.glb"), str(work / "info.json")]
        if in_place:
            cmd.append("--in-place")
        with _ONE_AT_A_TIME:
            try:
                proc = subprocess.run(cmd, capture_output=True, timeout=_TIMEOUT_S, cwd=tmp)
            except subprocess.TimeoutExpired:
                raise ValueError(f"the FBX took longer than {_TIMEOUT_S} s to convert")
        out, info = work / "out.glb", work / "info.json"
        if not out.is_file() or not info.is_file():
            tail = (proc.stderr or proc.stdout or b"").decode("utf-8", "replace").strip().splitlines()[-1:]
            raise ValueError("Blender could not read this FBX" + (f": {tail[0]}" if tail else ""))
        return out.read_bytes(), json.loads(info.read_text())
