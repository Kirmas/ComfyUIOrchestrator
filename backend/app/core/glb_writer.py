"""A small multi-primitive GLB writer (numpy + stdlib only, so Blender's own
Python can load it by path): triangles, line segments and points in one file.

mesh_simplify._write_glb writes the one-triangle-mesh case; this is for
files that mix kinds -- an Alembic with a head (triangles) and a groom (lines)
-- which Blender's own glTF exporter can't write (it drops curves).
"""

import json
import struct

import numpy as np

TRIANGLES, LINES, POINTS = 4, 1, 0
_JSON, _BIN = 0x4E4F534A, 0x004E4942


def write_glb(primitives: list[dict]) -> bytes:
    """Each primitive: {name, mode, positions (n,3), [normals (n,3)], [indices
    flat uint32], [color rgba], [blend: bool], [extras dict]} -> one node/mesh each, named `name`."""
    blob = bytearray()
    views: list[dict] = []
    accessors: list[dict] = []
    nodes: list[dict] = []
    meshes: list[dict] = []
    materials: list[dict] = []

    def add(data: bytes, target: int) -> int:
        blob.extend(b"\x00" * (-len(blob) % 4))
        views.append({"buffer": 0, "byteOffset": len(blob), "byteLength": len(data), "target": target})
        blob.extend(data)
        return len(views) - 1

    for i, prim in enumerate(primitives):
        pos = np.ascontiguousarray(prim["positions"], dtype="<f4")
        attributes = {}
        accessors.append({"bufferView": add(pos.tobytes(), 34962), "componentType": 5126, "count": len(pos),
                          "type": "VEC3", "min": pos.min(axis=0).tolist(), "max": pos.max(axis=0).tolist()})
        attributes["POSITION"] = len(accessors) - 1
        if prim.get("normals") is not None:
            accessors.append({"bufferView": add(np.ascontiguousarray(prim["normals"], dtype="<f4").tobytes(), 34962),
                              "componentType": 5126, "count": len(pos), "type": "VEC3"})
            attributes["NORMAL"] = len(accessors) - 1
        entry: dict = {"attributes": attributes, "mode": prim["mode"]}
        if prim.get("indices") is not None:
            idx = np.ascontiguousarray(prim["indices"], dtype="<u4").reshape(-1)
            accessors.append({"bufferView": add(idx.tobytes(), 34963), "componentType": 5125,
                              "count": int(idx.size), "type": "SCALAR"})
            entry["indices"] = len(accessors) - 1
        color = prim.get("color") or [0.8, 0.8, 0.8, 1.0]
        material = {"name": prim["name"], "doubleSided": True,
                    "pbrMetallicRoughness": {"baseColorFactor": color, "metallicFactor": 0.0, "roughnessFactor": 0.8}}
        if prim.get("blend"):  # the colour's alpha is real: overlapping lines add up
            material["alphaMode"] = "BLEND"
        materials.append(material)
        entry["material"] = len(materials) - 1
        meshes.append({"name": prim["name"], "primitives": [entry]})
        node: dict = {"name": prim["name"], "mesh": i}
        if prim.get("extras"):
            node["extras"] = prim["extras"]
        nodes.append(node)

    blob.extend(b"\x00" * (-len(blob) % 4))
    doc = {
        "asset": {"version": "2.0", "generator": "orchestrator glb_writer"},
        "scene": 0, "scenes": [{"nodes": list(range(len(nodes)))}],
        "nodes": nodes, "meshes": meshes, "materials": materials,
        "buffers": [{"byteLength": len(blob)}], "bufferViews": views, "accessors": accessors,
    }
    text = json.dumps(doc, separators=(",", ":")).encode()
    text += b" " * (-len(text) % 4)
    total = 12 + 8 + len(text) + 8 + len(blob)
    return (struct.pack("<4sII", b"glTF", 2, total) + struct.pack("<II", len(text), _JSON) + text
            + struct.pack("<II", len(blob), _BIN) + bytes(blob))
