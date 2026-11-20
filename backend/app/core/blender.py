"""native.blender_script: a GLB in, a Python (bpy) script, a GLB out -- so an
agent can change a mesh without a shell of its own.

Blender is an optional system dependency. find_blender_binary() decides at
import time whether the node type exists at all (core/node_types.py registers
it only when this returns a path), so a box without Blender simply has no
such node in the picker rather than one that always fails.

The script is arbitrary code, run as the service's own user. That is the
point of the node, and the person chose it knowingly (2026-11-19); what this
module does limit is the blast radius of accidents: a stripped environment
(no API_TOKEN / DATABASE_URL inherited from the unit's EnvironmentFile), a
throwaway HOME, a timeout, and one Blender at a time (each is ~1 GB of RAM).
"""
import asyncio
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.core.job_backend import AssetRef
from app.core.native_backend import NativeBackend

INSIDE_SCRIPT = Path(__file__).with_name("blender_inside.py")

DEFAULT_TIMEOUT_S = 120
MAX_TIMEOUT_S = 600
_STDERR_TAIL = 3000

# One Blender process at a time: a run peaks around 1 GB, and the box also
# hosts the API, Postgres and the frontend build.
_SEMAPHORE = asyncio.Semaphore(1)


def find_blender_binary() -> str | None:
    """BLENDER_PATH (.env) wins; then PATH; then the conventional /opt/blender
    unpack location of the official tarball."""
    for candidate in (get_settings().blender_path, shutil.which("blender"), "/opt/blender/blender"):
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


BLENDER_SCRIPT_INSTRUCTION = """\
The `script` field is Python run inside Blender (bpy) in background mode, no UI.
Before it runs: the scene is empty except for the input GLB, already imported.
Globals: `bpy`, and `imported` -- the list of objects the import created.
After it runs: everything left in the scene is exported as the output GLB
(meshes, materials, armatures). Delete what you don't want exported.
An exception fails the run with its traceback as the node's error; an empty
scene (no mesh) also fails. Prefer bpy.data / object.modifiers / bmesh over
bpy.ops where you can -- operators depend on context and selection.
Use an Apply step for modifiers (bpy.ops.object.modifier_apply with the
object active) or just leave them on: the export applies every modifier.
No rendering happens here; the output is the mesh only.
Example (halve the polygon count of every mesh):
    for o in bpy.context.scene.objects:
        if o.type == "MESH":
            m = o.modifiers.new("dec", "DECIMATE"); m.ratio = 0.5
"""


def _child_env(home: str) -> dict[str, str]:
    return {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": home,
        "LANG": "C.UTF-8",
        "TMPDIR": home,
    }


class BlenderScriptBackend(NativeBackend):
    async def _run(self, execution_config: dict, inputs: dict[str, Any]) -> list[AssetRef]:
        blender = find_blender_binary()
        if blender is None:
            raise RuntimeError("Blender is no longer installed on this server")
        mesh = inputs.get("mesh")
        if not isinstance(mesh, (bytes, bytearray)) or not mesh:
            raise ValueError("mesh input is required (a GLB file)")
        if bytes(mesh[:4]) != b"glTF":
            raise ValueError("mesh input is not a binary glTF (.glb) file")
        script = inputs.get("script") or ""
        if not script.strip():
            raise ValueError("script is empty")
        timeout = min(max(int(inputs.get("timeout_s") or DEFAULT_TIMEOUT_S), 5), MAX_TIMEOUT_S)

        async with _SEMAPHORE:
            with tempfile.TemporaryDirectory(prefix="blender-node-") as tmp:
                in_path = os.path.join(tmp, "in.glb")
                out_path = os.path.join(tmp, "out.glb")
                script_path = os.path.join(tmp, "script.py")
                with open(in_path, "wb") as f:
                    f.write(mesh)
                with open(script_path, "w", encoding="utf-8") as f:
                    f.write(script)

                proc = await asyncio.create_subprocess_exec(
                    blender, "-b", "--factory-startup", "-noaudio", "--python-exit-code", "1",
                    "-P", str(INSIDE_SCRIPT), "--", in_path, out_path, script_path,
                    cwd=tmp, env=_child_env(tmp),
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                )
                try:
                    _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
                    raise RuntimeError(f"Blender script timed out after {timeout}s")

                if proc.returncode != 0 or not os.path.isfile(out_path):
                    tail = stderr.decode("utf-8", "replace")[-_STDERR_TAIL:].strip()
                    raise RuntimeError(f"Blender script failed (exit {proc.returncode}):\n{tail}")
                with open(out_path, "rb") as f:
                    data = f.read()

        return [AssetRef(data=data, mime_type="model/gltf-binary", kind="mesh")]
