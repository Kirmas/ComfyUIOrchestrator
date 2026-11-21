"""The viewer-friendly rendition of a mesh stored in some other format.

Every mesh format is `kind=mesh`; the 3D viewer (<model-viewer>) only loads
GLB, so a mesh in any other format is shown through a GLB converted from the
original on first request and cached under <cache_dir>/mesh_views_v3/. Assets are
immutable, so the cache key (the storage key) never goes stale. The original
is never replaced: download and nodes keep reading it.
"""

import asyncio
import hashlib
from collections.abc import Callable
from pathlib import Path

from app.config import get_settings
from app.core.stl_convert import STL_MIME, stl_to_glb

# mime -> the short format name shown on a card. Anything not listed is "glb".
MESH_FORMAT_BY_MIME = {"model/gltf-binary": "glb", STL_MIME: "stl"}

_locks: dict[str, asyncio.Lock] = {}


def _cache_path(key: str) -> Path:
    name = hashlib.sha1(key.encode()).hexdigest()
    # v3: v2 held one shared file for every asset (a comment ate the rest of this
    # path); v1 had STL unrotated.
    return Path(get_settings().cache_dir) / "mesh_views_v3" / name[:2] / f"{name}.glb"


def _convert(data: bytes, mime: str) -> bytes:
    if mime == STL_MIME:
        glb = stl_to_glb(data)
        if glb is not None:
            return glb
    raise ValueError("unsupported or unreadable mesh format")


async def mesh_view_path(key: str, read_original: Callable[[], bytes], mime: str = STL_MIME) -> Path:
    path = _cache_path(key)
    if path.is_file():
        return path
    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        if not path.is_file():
            def build() -> None:
                glb = _convert(read_original(), mime)
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(".tmp")
                tmp.write_bytes(glb)
                tmp.replace(path)

            await asyncio.to_thread(build)
        _locks.pop(key, None)
    return path
