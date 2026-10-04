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


def counts_from_file(path: Path, offset: int = 0) -> tuple[int, int] | None:
    """(vertices, triangles) for a GLB on disk, reading only its header and JSON
    chunk. `offset` is where the GLB starts -- past the prefix block, for a
    stored asset."""

    def read(start: int, size: int) -> bytes:
        with path.open("rb") as f:
            f.seek(offset + start)
            return f.read(size)

    try:
        doc = _json_chunk(read)
    except OSError:
        return None
    return _counts(doc) if doc is not None else None
