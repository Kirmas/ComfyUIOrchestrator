"""STL (the 3D-printing format) -> GLB, so an uploaded STL is an ordinary mesh.

STL is real 3D geometry, just the bare minimum: a soup of triangles with no
colour, texture, material or animation, and no shared vertices. The viewer
only reads GLB, so on upload we weld the soup into an indexed mesh and write
it as GLB (numpy only; the GLB writer is mesh_simplify's). Binary and ASCII
STL are both read. Anything that doesn't parse is left alone (stored as-is).
"""

import re
import struct

import numpy as np

from app.core.mesh_simplify import _vertex_normals, _write_glb

_ASCII_VERTEX = re.compile(rb"vertex\s+(\S+)\s+(\S+)\s+(\S+)")


def _triangles(data: bytes) -> np.ndarray | None:
    """(n, 3, 3) float32 triangles, or None when this isn't an STL."""
    if len(data) >= 84:
        (count,) = struct.unpack_from("<I", data, 80)
        if count and len(data) == 84 + 50 * count:  # binary: exact size is the signature
            rec = np.frombuffer(data, dtype=np.dtype([("n", "<f4", 3), ("v", "<f4", (3, 3)), ("a", "<u2")]),
                                count=count, offset=84)
            return rec["v"].astype(np.float32)
    head = data[:512].lstrip()
    if head[:5].lower() == b"solid" and b"facet" in data[:4096]:
        try:
            nums = [float(x) for m in _ASCII_VERTEX.finditer(data) for x in m.groups()]
        except ValueError:
            return None
        if nums and len(nums) % 9 == 0:
            return np.asarray(nums, dtype=np.float32).reshape(-1, 3, 3)
    return None


def stl_to_glb(data: bytes) -> bytes | None:
    tris = _triangles(data)
    if tris is None or not np.isfinite(tris).all():
        return None
    flat = np.ascontiguousarray(tris.reshape(-1, 3))
    # Weld identical corners (STL repeats every vertex once per triangle).
    keyed = flat.view(np.dtype((np.void, 12))).ravel()
    _, first, inverse = np.unique(keyed, return_index=True, return_inverse=True)
    points = flat[first]
    faces = inverse.reshape(-1, 3).astype(np.uint32)
    return _write_glb(points, _vertex_normals(points, faces), faces, None)


def convert_stl_upload(data: bytes, filename: str | None) -> bytes:
    """The GLB for an uploaded .stl, otherwise the bytes unchanged."""
    if filename and filename.lower().endswith(".stl"):
        return stl_to_glb(data) or data
    return data


# --- GLB -> STL and the format-to-format entry point ------------------------

_FORMATS = ("glb", "stl")


def detect_mesh_format(data: bytes) -> str | None:
    """'glb' / 'stl' by the bytes themselves, None for anything else."""
    if data[:4] == b"glTF":
        return "glb"
    if _triangles(data) is not None:
        return "stl"
    return None


def _node_matrix(node: dict) -> np.ndarray:
    if "matrix" in node:
        return np.array(node["matrix"], dtype=np.float64).reshape(4, 4).T  # glTF is column-major
    x, y, z, w = node.get("rotation", [0, 0, 0, 1])
    rot = np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ])
    m = np.eye(4)
    m[:3, :3] = rot * np.array(node.get("scale", [1, 1, 1]))
    m[:3, 3] = node.get("translation", [0, 0, 0])
    return m


def glb_to_stl(data: bytes) -> bytes:
    """Binary STL of every triangle in the GLB's default scene, node transforms
    applied. Geometry only: colour, textures, skins and animation are dropped
    (STL can't hold them), the rest pose is what is written. Axes and units are
    kept as they are in the GLB (glTF is Y-up, in metres)."""
    from app.core.mesh_simplify import _accessor, _split_glb

    doc, blob = _split_glb(data)
    nodes = doc.get("nodes", [])
    scene = doc.get("scenes", [{}])[doc.get("scene", 0)] if doc.get("scenes") else {}
    roots = scene.get("nodes", list(range(len(nodes))))
    chunks: list[np.ndarray] = []

    def visit(index: int, parent: np.ndarray) -> None:
        node = nodes[index]
        world = parent @ _node_matrix(node)
        if "mesh" in node:
            for prim in doc["meshes"][node["mesh"]]["primitives"]:
                if prim.get("mode", 4) != 4:
                    continue  # only triangle lists; points/lines have no surface
                pts = _accessor(doc, blob, prim["attributes"]["POSITION"]).astype(np.float64)
                pts = pts @ world[:3, :3].T + world[:3, 3]
                if "indices" in prim:
                    tri = pts[_accessor(doc, blob, prim["indices"]).astype(np.int64)]
                else:
                    tri = pts
                chunks.append(tri.reshape(-1, 3, 3).astype(np.float32))
        for child in node.get("children", []):
            visit(child, world)

    for root in roots:
        visit(root, np.eye(4))
    if not chunks:
        raise ValueError("The GLB has no triangle geometry to write as STL")
    tris = np.concatenate(chunks)
    normals = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])
    length = np.linalg.norm(normals, axis=1, keepdims=True)
    length[length == 0] = 1.0
    rec = np.zeros(len(tris), dtype=np.dtype([("n", "<f4", 3), ("v", "<f4", (3, 3)), ("a", "<u2")]))
    rec["n"] = normals / length
    rec["v"] = tris
    return b"orchestrator STL".ljust(80, b"\0") + struct.pack("<I", len(tris)) + rec.tobytes()


def convert_mesh(data: bytes, target: str) -> tuple[bytes, str, str]:
    """(bytes, mime_type, asset kind) of `data` converted to `target`.
    Raises ValueError when the input isn't a known mesh or the pair is unsupported."""
    target = (target or "").lower()
    if target not in _FORMATS:
        raise ValueError(f"Unsupported output format '{target}' (supported: {', '.join(_FORMATS)})")
    source = detect_mesh_format(data)
    if source is None:
        raise ValueError(f"Can't convert to {target.upper()}: the input isn't a GLB or STL file")
    if source == "glb" and target == "stl":
        return glb_to_stl(data), "model/stl", "other"
    if source == "stl" and target == "glb":
        glb = stl_to_glb(data)
        if glb is None:
            raise ValueError("The STL has no usable triangles")
        return glb, "model/gltf-binary", "mesh"
    raise ValueError(f"The input is already {source.upper()}; pick a different output format")
