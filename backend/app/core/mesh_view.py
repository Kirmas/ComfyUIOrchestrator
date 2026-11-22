"""The viewer-friendly rendition of a mesh stored in some other format.

Every mesh format is `kind=mesh`; the 3D viewer (<model-viewer>) only loads
GLB, so a mesh in any other format is shown through a GLB converted from the
original on first request and cached under <cache_dir>/mesh_views_v3/. Assets are
immutable, so the cache key (the storage key) never goes stale. The original
is never replaced: download and nodes keep reading it.
"""

import asyncio
import hashlib
import json
import threading
from collections.abc import Callable
from pathlib import Path

from app.config import get_settings
from app.core import glb_stats
from app.core.abc_convert import ABC_MIME, abc_to_glb
from app.core.fbx_convert import FBX_MIME, fbx_to_glb
from app.core.stl_convert import STL_MIME, stl_to_glb

# mime -> the short format name shown on a card. Anything not listed is "glb".
MESH_FORMAT_BY_MIME = {"model/gltf-binary": "glb", STL_MIME: "stl", FBX_MIME: "fbx", ABC_MIME: "abc"}

# Formats a background Blender converts (slow, one at a time): they also leave an
# info file next to the GLB, and are built ahead of time by warm().
_BLENDER_CONVERTERS = {FBX_MIME: fbx_to_glb, ABC_MIME: abc_to_glb}


def converts_via_blender(mime: str) -> bool:
    return mime in _BLENDER_CONVERTERS

# Bump whenever a converter's output changes: it names the cache directory AND is the
# `?v=` of the view URL (core/storage.build_view_url). The URL has to change because
# the file is served `immutable` -- a browser never asks again, so a better GLB under
# the same URL is never seen (the colourless Alembic grooms, 2026-11-22).
VIEW_VERSION = 6

_locks: dict[str, asyncio.Lock] = {}


def _cache_path(key: str, suffix: str = ".glb") -> Path:
    name = hashlib.sha1(key.encode()).hexdigest()
    # v5: grooms are translucent; v4: a colour per groom (older files are simply left unused).
    # v3: v2 held one shared file for every asset (a comment ate the rest of this
    # path); v1 had STL unrotated.
    return Path(get_settings().cache_dir) / f"mesh_views_v{VIEW_VERSION}" / name[:2] / f"{name}{suffix}"


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


def _convert(key: str, data: bytes, mime: str, in_place: bool) -> None:
    """Writes the cached rendition(s) of `key`. FBX also leaves its info
    (animation clips, bones, root motion) next to the GLB."""
    if mime == STL_MIME:
        glb = stl_to_glb(data)
        if glb is not None:
            _write(_cache_path(key), glb)
            return
    elif mime in _BLENDER_CONVERTERS:
        glb, info = _BLENDER_CONVERTERS[mime](data, in_place=in_place)
        _write(_cache_path(key, ".inplace.glb" if in_place else ".glb"), glb)
        if not in_place:
            _write(_cache_path(key, ".json"), json.dumps(info).encode())
        return
    raise ValueError("unsupported or unreadable mesh format")


async def mesh_view_path(key: str, read_original: Callable[[], bytes], mime: str = STL_MIME,
                         in_place: bool = False) -> Path:
    """The GLB for `key`; `in_place` is the same animation with the root's
    horizontal travel removed (only meaningful for FBX, ignored otherwise)."""
    in_place = in_place and mime == FBX_MIME  # root motion only means something for an FBX
    path = _cache_path(key, ".inplace.glb" if in_place else ".glb")
    if path.is_file():
        return path
    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        if not path.is_file():
            await asyncio.to_thread(lambda: _convert(key, read_original(), mime, in_place))
        _locks.pop(key, None)
    return path


async def mesh_view_info(key: str, read_original: Callable[[], bytes], mime: str,
                         original: tuple[Path, int] | None = None) -> dict | None:
    """What the conversion learned about the file (FBX: clips, bones, root
    motion); a GLB is read for the same report straight off its JSON chunk
    (`original` = its path and payload offset); None for a format that has
    nothing to report (STL)."""
    if mime == "model/gltf-binary" and original is not None:
        return glb_stats.view_info_from_file(*original)
    if not converts_via_blender(mime):
        return None
    await mesh_view_path(key, read_original, mime)
    info = json.loads(_cache_path(key, ".json").read_text())
    if "meshes" not in info:
        # Converted before the info listed its meshes: build it again.
        for suffix in (".glb", ".inplace.glb", ".json"):
            _cache_path(key, suffix).unlink(missing_ok=True)
        await mesh_view_path(key, read_original, mime)
        info = json.loads(_cache_path(key, ".json").read_text())
    return info


# --- background warm-up + counts for formats that have none of their own ----

_warming: set[str] = set()
_failed: set[str] = set()  # not retried until the next restart: a broken file would respawn Blender per listing


def view_counts(key: str) -> tuple[int, int] | None:
    """(vertices, triangles) of the cached GLB rendition, None while it isn't built."""
    path = _cache_path(key)
    return glb_stats.counts_from_file(path) if path.is_file() else None


def warm(key: str, read_original: Callable[[], bytes], mime: str) -> None:
    """Starts building the rendition in a background thread (once per file), so
    a format with no readable counts of its own (FBX) gets them, and the viewer
    opens at once. Returns immediately; the counts show on a later read."""
    if not converts_via_blender(mime) or key in _warming or key in _failed or _cache_path(key).is_file():
        return
    _warming.add(key)

    def run() -> None:
        try:
            _convert(key, read_original(), mime, False)
        except Exception:  # noqa: BLE001 -- surfaced to the person when they open it (422)
            _failed.add(key)
        finally:
            _warming.discard(key)

    threading.Thread(target=run, daemon=True, name="mesh-warm").start()
