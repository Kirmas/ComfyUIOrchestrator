"""Vertex and triangle counts for a binary glTF (.glb) file.

The counts live in the file's JSON chunk, not in the geometry, so parsing that
chunk is enough: a 40 MB mesh costs one small read, never a load of its
buffers. That is what lets an asset written before this existed be counted
straight off disk.

Only GLB is understood. Anything else a Save3D node can emit (.obj, .stl, .ply)
comes back as None, which the UI reads as "no counts to show" -- honest, rather
than a guess from a format we don't parse.
"""

import json
import struct
from collections.abc import Callable
from pathlib import Path

_HEADER = struct.Struct("<4sII")  # magic, version, total length
_CHUNK_HEADER = struct.Struct("<II")  # chunk length, chunk type
_MAGIC = b"glTF"
_CHUNK_JSON = 0x4E4F534A  # b"JSON", little-endian
# A real glTF JSON chunk is kilobytes. The cap only stops a corrupt length
# field from asking for gigabytes of "JSON".
_MAX_JSON_BYTES = 16 * 1024 * 1024

# glTF primitive modes: 4 = triangles, 5 = triangle strip, 6 = triangle fan.
# Points and lines (0-3) aren't surfaces, so they don't count as faces.
_TRIANGLE_MODES = {4, 5, 6}


def _json_chunk(read: Callable[[int, int], bytes]) -> dict | None:
    head = read(0, _HEADER.size + _CHUNK_HEADER.size)
    if len(head) < _HEADER.size + _CHUNK_HEADER.size:
        return None
    magic, _version, _length = _HEADER.unpack_from(head, 0)
    if magic != _MAGIC:
        return None
    chunk_length, chunk_type = _CHUNK_HEADER.unpack_from(head, _HEADER.size)
    if chunk_type != _CHUNK_JSON or chunk_length > _MAX_JSON_BYTES:
        return None
    try:
        doc = json.loads(read(_HEADER.size + _CHUNK_HEADER.size, chunk_length))
    except (ValueError, UnicodeDecodeError):
        return None
    return doc if isinstance(doc, dict) else None


def _counts(doc: dict) -> tuple[int, int] | None:
    accessors = doc.get("accessors", [])

    def count(index: int) -> int:
        return int(accessors[index]["count"])

    try:
        # Several primitives can share one POSITION accessor; a shared vertex
        # buffer is one set of vertices, so count each accessor once.
        position_accessors: set[int] = set()
        triangles = 0
        for mesh in doc.get("meshes", []):
            for prim in mesh.get("primitives", []):
                mode = prim.get("mode", 4)
                if mode not in _TRIANGLE_MODES:
                    continue
                position = prim.get("attributes", {}).get("POSITION")
                if position is None:
                    continue
                position_accessors.add(position)
                indexed = count(prim["indices"]) if "indices" in prim else count(position)
                triangles += indexed // 3 if mode == 4 else max(indexed - 2, 0)
        vertices = sum(count(index) for index in position_accessors)
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    return vertices, triangles


def counts_from_bytes(data: bytes) -> tuple[int, int] | None:
    """(vertices, triangles) for a GLB already in memory, or None if it isn't one."""
    doc = _json_chunk(lambda offset, size: data[offset : offset + size])
    return _counts(doc) if doc is not None else None


def _doc_from_file(path: Path, offset: int) -> dict | None:
    def read(start: int, size: int) -> bytes:
        with path.open("rb") as f:
            f.seek(offset + start)
            return f.read(size)

    try:
        return _json_chunk(read)
    except OSError:
        return None


def counts_from_file(path: Path, offset: int = 0) -> tuple[int, int] | None:
    """(vertices, triangles) for a GLB on disk, reading only its header and JSON
    chunk. `offset` is where the GLB starts -- past the prefix block, for a
    stored asset."""
    doc = _doc_from_file(path, offset)
    return _counts(doc) if doc is not None else None


def _view_info(doc: dict) -> dict:
    """The same report a converted FBX gets (core/blender_fbx_to_glb.py's info.json),
    read off a GLB's own JSON: its animation clips, bones, and meshes -- so the viewer
    offers clips and parts for a GLB too. glTF stores no frame rate: `frames` is the
    densest keyframe track and `fps` follows from it (exact for baked exports, which
    key every frame)."""
    accessors = doc.get("accessors", [])
    clips = []
    for i, anim in enumerate(doc.get("animations", [])):
        lo, hi, keys = None, 0.0, 0
        for sampler in anim.get("samplers", []):
            acc = accessors[sampler["input"]]
            hi = max(hi, float((acc.get("max") or [0])[0]))
            first = float((acc.get("min") or [0])[0])
            lo = first if lo is None else min(lo, first)
            keys = max(keys, int(acc.get("count", 0)))
        seconds = hi - (lo or 0.0)  # a clip exported from frame 1 starts at 1/fps, not 0
        clips.append({"name": anim.get("name") or f"Animation {i + 1}", "frames": keys,
                      "seconds": round(seconds, 3), "root_motion": False})
    timed = [c for c in clips if c["seconds"] > 0 and c["frames"] > 1]
    fps = round((timed[0]["frames"] - 1) / timed[0]["seconds"], 3) if timed else 0
    joints = {j for skin in doc.get("skins", []) for j in skin.get("joints", [])}
    meshes_doc = doc.get("meshes", [])
    meshes = []
    for node in doc.get("nodes", []):
        if "mesh" not in node:
            continue
        prims = meshes_doc[node["mesh"]].get("primitives", [])
        vertices = sum(int(accessors[p["attributes"]["POSITION"]]["count"]) for p in prims
                       if "POSITION" in p.get("attributes", {}))
        meshes.append({"name": node.get("name") or meshes_doc[node["mesh"]].get("name") or "mesh", "vertices": vertices})
    return {"clips": clips, "fps": fps, "bones": len(joints), "has_mesh": bool(meshes),
            "root_motion": False, "meshes": meshes}


def view_info_from_file(path: Path, offset: int = 0) -> dict | None:
    doc = _doc_from_file(path, offset)
    if doc is None:
        return None
    try:
        return _view_info(doc)
    except (KeyError, IndexError, TypeError, ValueError):
        return None
