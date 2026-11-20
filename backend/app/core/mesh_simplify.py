"""Shrinking a very dense GLB *before* Blender ever sees it.

Measured on the box (2026-11-20), a raw AI generation of 6M triangles costs
Blender 2.1 GB just to import and ~5.7 GB to decimate in-process, and Blender
never gives that memory back. Doing the same reduction here takes ~5 s and a
few hundred MB, in a short-lived subprocess whose memory the OS reclaims the
moment it exits (run via `python -m app.core.mesh_simplify IN OUT TARGET`).

Deliberately narrow: exactly one mesh with one triangle primitive and no
skin/morph targets. Anything richer (a rig, several parts, vertex animation)
would be damaged by collapsing vertices, so it is left alone and the caller is
told why (`SimplifyResult.skipped`) instead of silently getting a broken
model. Attributes other than position are dropped; smooth normals are
recomputed from the reduced geometry, because Blender shades a mesh with no
normals flat and faceted.
"""
import json
import struct
import sys
from dataclasses import dataclass

import numpy as np

_COMPONENT = {5121: np.uint8, 5123: np.uint16, 5125: np.uint32, 5126: np.float32}
_NCOMP = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4}
_JSON, _BIN = 0x4E4F534A, 0x004E4942


@dataclass
class SimplifyResult:
    triangles_before: int
    triangles_after: int
    skipped: str | None = None  # why nothing was done (None = simplified)


def _split_glb(data: bytes | memoryview) -> tuple[dict, memoryview]:
    view = memoryview(data)
    magic, _version, _length = struct.unpack_from("<4sII", view, 0)
    if magic != b"glTF":
        raise ValueError("not a binary glTF")
    offset, doc, blob = 12, None, memoryview(b"")
    while offset + 8 <= len(view):
        size, kind = struct.unpack_from("<II", view, offset)
        chunk = view[offset + 8 : offset + 8 + size]
        if kind == _JSON:
            doc = json.loads(bytes(chunk))
        elif kind == _BIN:
            blob = chunk
        offset += 8 + size
    if doc is None:
        raise ValueError("GLB has no JSON chunk")
    return doc, blob


def _accessor(doc: dict, blob: memoryview, index: int) -> np.ndarray:
    acc = doc["accessors"][index]
    if acc.get("sparse"):
        raise ValueError("sparse accessor")
    view = doc["bufferViews"][acc["bufferView"]]
    dtype = np.dtype(_COMPONENT[acc["componentType"]])
    width = _NCOMP[acc["type"]]
    count = acc["count"]
    start = view.get("byteOffset", 0) + acc.get("byteOffset", 0)
    stride = view.get("byteStride")
    if stride and stride != dtype.itemsize * width:
        raise ValueError("interleaved accessor")
    flat = np.frombuffer(blob, dtype=dtype, count=count * width, offset=start)
    return flat.reshape(count, width) if width > 1 else flat


def _vertex_normals(points: np.ndarray, faces: np.ndarray) -> np.ndarray:
    tri = points[faces]
    face_n = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])  # area-weighted
    normals = np.zeros_like(points)
    for corner in range(3):
        np.add.at(normals, faces[:, corner], face_n)
    length = np.linalg.norm(normals, axis=1, keepdims=True)
    length[length == 0] = 1.0
    return (normals / length).astype(np.float32)


def _write_glb(points: np.ndarray, normals: np.ndarray, faces: np.ndarray, material: dict | None) -> bytes:
    pos = np.ascontiguousarray(points, dtype="<f4").tobytes()
    nor = np.ascontiguousarray(normals, dtype="<f4").tobytes()
    idx = np.ascontiguousarray(faces.reshape(-1), dtype="<u4").tobytes()
    blob = pos + nor + idx
    blob += b"\x00" * (-len(blob) % 4)
    lo, hi = points.min(axis=0).tolist(), points.max(axis=0).tolist()
    doc: dict = {
        "asset": {"version": "2.0", "generator": "orchestrator mesh_simplify"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0}],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 0, "NORMAL": 1}, "indices": 2, "mode": 4}]}],
        "buffers": [{"byteLength": len(blob)}],
        "bufferViews": [
            {"buffer": 0, "byteOffset": 0, "byteLength": len(pos), "target": 34962},
            {"buffer": 0, "byteOffset": len(pos), "byteLength": len(nor), "target": 34962},
            {"buffer": 0, "byteOffset": len(pos) + len(nor), "byteLength": len(idx), "target": 34963},
        ],
        "accessors": [
            {"bufferView": 0, "componentType": 5126, "count": len(points), "type": "VEC3", "min": lo, "max": hi},
            {"bufferView": 1, "componentType": 5126, "count": len(points), "type": "VEC3"},
            {"bufferView": 2, "componentType": 5125, "count": int(faces.size), "type": "SCALAR"},
        ],
    }
    if material is not None:
        doc["materials"] = [material]
        doc["meshes"][0]["primitives"][0]["material"] = 0
    text = json.dumps(doc, separators=(",", ":")).encode()
    text += b" " * (-len(text) % 4)
    total = 12 + 8 + len(text) + 8 + len(blob)
    return (
        struct.pack("<4sII", b"glTF", 2, total)
        + struct.pack("<II", len(text), _JSON) + text
        + struct.pack("<II", len(blob), _BIN) + blob
    )


def simplify_glb(data: bytes, target_triangles: int) -> tuple[bytes | None, SimplifyResult]:
    """(new GLB bytes, report). Bytes are None when the model was left alone."""
    doc, blob = _split_glb(data)
    meshes = doc.get("meshes", [])
    if len(meshes) != 1 or len(meshes[0].get("primitives", [])) != 1:
        return None, SimplifyResult(0, 0, "more than one mesh/primitive")
    if doc.get("skins") or doc.get("animations"):
        return None, SimplifyResult(0, 0, "has a skeleton or animation")
    prim = meshes[0]["primitives"][0]
    if prim.get("mode", 4) != 4 or "indices" not in prim or prim.get("targets"):
        return None, SimplifyResult(0, 0, "not a plain indexed triangle mesh")
    points = np.asarray(_accessor(doc, blob, prim["attributes"]["POSITION"]), dtype=np.float32)
    faces = _accessor(doc, blob, prim["indices"]).astype(np.int32).reshape(-1, 3)
    before = len(faces)
    if before <= target_triangles:
        return None, SimplifyResult(before, before, "already small enough")

    import fast_simplification  # imported late: only the subprocess needs it

    new_points, new_faces = fast_simplification.simplify(points, faces, target_reduction=1 - target_triangles / before)
    material = None
    if "material" in prim:
        material = doc["materials"][prim["material"]]
        # Textures were bound to UVs that no longer exist; keep only the colour.
        material = {k: v for k, v in material.items() if k != "pbrMetallicRoughness"} | {
            "pbrMetallicRoughness": {
                "baseColorFactor": material.get("pbrMetallicRoughness", {}).get("baseColorFactor", [0.8, 0.8, 0.8, 1.0]),
                "metallicFactor": 0.0,
                "roughnessFactor": 0.8,
            }
        }
        material.pop("normalTexture", None)
        material.pop("occlusionTexture", None)
        material.pop("emissiveTexture", None)
    out = _write_glb(new_points, _vertex_normals(new_points, new_faces), new_faces, material)
    return out, SimplifyResult(before, len(new_faces))


def glb_triangle_count(data: bytes) -> int | None:
    """Cheap triangle total from the accessors alone (no buffers decoded)."""
    doc, _ = _split_glb(data)
    total = 0
    for mesh in doc.get("meshes", []):
        for prim in mesh.get("primitives", []):
            if prim.get("mode", 4) != 4:
                continue
            if "indices" in prim:
                total += doc["accessors"][prim["indices"]]["count"] // 3
            else:
                total += doc["accessors"][prim["attributes"]["POSITION"]]["count"] // 3
    return total


def main(argv: list[str]) -> int:
    src, dst, target = argv[1], argv[2], int(argv[3])
    with open(src, "rb") as f:
        data = f.read()
    out, report = simplify_glb(data, target)
    if out is not None:
        with open(dst, "wb") as f:
            f.write(out)
    print(json.dumps({"before": report.triangles_before, "after": report.triangles_after, "skipped": report.skipped,
                      "written": out is not None}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
