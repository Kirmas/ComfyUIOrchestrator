"""Put textures into a GLB's materials after the fact.

Unreal's glTF exporter can't read a Substrate material graph: a mesh on the
Interchange glTF material (M_GLTF, Substrate on) came out with Base Color 0 --
black -- though the material instance holds plain glTF-named texture parameters
(BaseColorTexture, MetallicRoughnessTexture, NormalTexture, ...). The bridge
plugin sends those textures along with the GLB, and this writes them into the
named materials, appending the images to the binary chunk (2026-11-24).
"""
import json
import struct

_MAGIC = b"glTF"
_JSON, _BIN = 0x4E4F534A, 0x004E4942

# fixup key -> where it goes in a glTF material
_SLOTS = {
    "baseColor": ("pbrMetallicRoughness", "baseColorTexture"),
    "metallicRoughness": ("pbrMetallicRoughness", "metallicRoughnessTexture"),
    "normal": (None, "normalTexture"),
    "emissive": (None, "emissiveTexture"),
    "occlusion": (None, "occlusionTexture"),
}


def _pad(b: bytes | bytearray, fill: bytes) -> bytes:
    return bytes(b) + fill * ((4 - len(b) % 4) % 4)


def _split(data: bytes) -> tuple[dict, bytearray]:
    magic, _version, _length = struct.unpack_from("<4sII", data, 0)
    if magic != _MAGIC:
        raise ValueError("not a GLB")
    jlen, jtype = struct.unpack_from("<II", data, 12)
    if jtype != _JSON:
        raise ValueError("GLB without a JSON chunk first")
    doc = json.loads(data[20:20 + jlen])
    pos = 20 + jlen
    binary = bytearray()
    if pos + 8 <= len(data):
        blen, btype = struct.unpack_from("<II", data, pos)
        if btype == _BIN:
            binary = bytearray(data[pos + 8:pos + 8 + blen])
    return doc, binary


def _join(doc: dict, binary: bytearray) -> bytes:
    j = _pad(json.dumps(doc, separators=(",", ":")).encode(), b" ")
    b = _pad(binary, b"\0")
    total = 12 + 8 + len(j) + (8 + len(b) if b else 0)
    out = [struct.pack("<4sII", _MAGIC, 2, total), struct.pack("<II", len(j), _JSON), j]
    if b:
        out += [struct.pack("<II", len(b), _BIN), b]
    return b"".join(out)


def add_material_textures(data: bytes, fixups: dict, images: dict[str, bytes], only_missing: bool = True) -> tuple[bytes, list[str]]:
    """`fixups` = {material name: {"baseColor": image key, ..., "baseColorFactor": [r,g,b,a]}},
    `images` = {image key: PNG bytes}. A slot the exporter already filled is left alone
    unless `only_missing` is off. Returns the new GLB and what was changed."""
    doc, binary = _split(data)
    materials = doc.get("materials", [])
    by_name = {m.get("name"): m for m in materials}
    changed: list[str] = []
    texture_of: dict[str, int] = {}  # one image used by several materials is stored once

    def texture(key: str) -> int:
        if key not in texture_of:
            png = images[key]
            while len(binary) % 4:
                binary.append(0)
            view = {"buffer": 0, "byteOffset": len(binary), "byteLength": len(png)}
            binary.extend(png)
            doc.setdefault("bufferViews", []).append(view)
            doc.setdefault("images", []).append({"bufferView": len(doc["bufferViews"]) - 1, "mimeType": "image/png", "name": key})
            doc.setdefault("textures", []).append({"source": len(doc["images"]) - 1})
            texture_of[key] = len(doc["textures"]) - 1
        return texture_of[key]

    def image_size(tex_index: int) -> tuple[int, int] | None:
        """Width/height of a texture's PNG from its IHDR, without decoding it."""
        try:
            img = doc["images"][doc["textures"][tex_index]["source"]]
            view = doc["bufferViews"][img["bufferView"]]
            head = bytes(binary[view.get("byteOffset", 0):view.get("byteOffset", 0) + 24])
        except (KeyError, IndexError, TypeError):
            return None
        if head[:8] != b"\x89PNG\r\n\x1a\n":
            return None
        return struct.unpack(">II", head[16:24])

    for name, fix in fixups.items():
        mat = by_name.get(name)
        if mat is None:
            continue
        # The same failed bake that left Base Color black wrote Occlusion as ONE black pixel:
        # occlusion scales the ambient light, which is nearly all a viewer lights with, so the
        # mesh stayed black even with its colour fixed (2026-11-24). A texture that small is no
        # occlusion map at all -- drop it.
        occ = mat.get("occlusionTexture")
        size = image_size(occ["index"]) if occ else None
        if size and size[0] * size[1] <= 16:
            del mat["occlusionTexture"]
            changed.append(f"{name}.occlusion(dropped {size[0]}x{size[1]} bake)")
        for slot, (parent, field) in _SLOTS.items():
            key = fix.get(slot)
            if not key or key not in images:
                continue
            holder = mat.setdefault(parent, {}) if parent else mat
            if only_missing and field in holder:
                continue
            holder[field] = {"index": texture(key)}
            changed.append(f"{name}.{slot}")
            if slot == "baseColor":
                factor = fix.get("baseColorFactor") or [1, 1, 1, 1]
                holder["baseColorFactor"] = [float(v) for v in factor][:4]
            if slot == "metallicRoughness":
                holder["metallicFactor"] = 1.0
                holder["roughnessFactor"] = 1.0
    if not changed:
        return data, changed
    if not doc.get("buffers"):
        doc["buffers"] = [{"byteLength": 0}]
    doc["buffers"][0]["byteLength"] = len(binary)
    return _join(doc, binary), changed
