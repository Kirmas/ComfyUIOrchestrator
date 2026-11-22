"""Running one of the Blender converter scripts (blender_fbx_to_glb.py,
blender_abc_to_glb.py) on some bytes: a temp dir, one Blender at a time (it never
hands memory back and a big file is hundreds of MB), a time limit, and the
GLB + info.json it leaves behind."""

import json
import subprocess
import tempfile
import threading
from pathlib import Path

from app.core.blender import find_blender_binary

_TIMEOUT_S = 300
_ONE_AT_A_TIME = threading.Lock()


def run_converter(script: Path, data: bytes, suffix: str, label: str, extra_args: list[str] | None = None) -> tuple[bytes, dict]:
    """(GLB bytes, info). Raises ValueError (with a reason fit to show) when there
    is no Blender, the file takes too long, or Blender could not read it."""
    blender = find_blender_binary()
    if blender is None:
        raise ValueError(f"Blender is not installed, so an {label} can't be converted")
    with tempfile.TemporaryDirectory(prefix=f"{suffix.strip('.')}_") as tmp:
        work = Path(tmp)
        (work / f"in{suffix}").write_bytes(data)
        cmd = [blender, "-b", "--factory-startup", "--python", str(script), "--",
               str(work / f"in{suffix}"), str(work / "out.glb"), str(work / "info.json"), *(extra_args or [])]
        with _ONE_AT_A_TIME:
            try:
                proc = subprocess.run(cmd, capture_output=True, timeout=_TIMEOUT_S, cwd=tmp)
            except subprocess.TimeoutExpired:
                raise ValueError(f"the {label} took longer than {_TIMEOUT_S} s to convert")
        out, info = work / "out.glb", work / "info.json"
        if not out.is_file() or not info.is_file():
            tail = (proc.stderr or proc.stdout or b"").decode("utf-8", "replace").strip().splitlines()[-1:]
            raise ValueError(f"Blender could not read this {label}" + (f": {tail[0]}" if tail else ""))
        return out.read_bytes(), json.loads(info.read_text())
