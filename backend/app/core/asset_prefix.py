"""The 64 KiB header block that every stored asset file carries in front of its
real bytes: a small self-describing service header plus a ready-made raster
preview.

Why the preview lives *inside* the asset file rather than in a sidecar: an
8K PNG in this library is 40-49 MB and decodes to ~132 MB of bitmap in the
browser, for a grid cell that renders it at 118x118 CSS px. The grid needed a
thumbnail; a sidecar file would have needed its own lifecycle (storage GC would
have reported every one of them as an orphan, and delete_object would have had
to remember to unlink a second path). One file, one lifecycle.

Layout -- every offset fixed, so finding anything is a single pread:

    0   4   magic
    4   1   version
    5   1   asset kind code   <- the discriminator for everything below
    6   2   preview_len       uint16 LE, 0 means "no preview in this file"
    8  24   descriptor, interpreted according to the kind code:
              image / mask : w uint32, h uint32, 16 B spare
              mesh         : verts uint32, faces uint32, 16 B spare
              other        : zeroes
    32 ..   preview bytes (WebP), then zero padding to PREFIX_SIZE

The kind code is stored rather than looked up from Asset.kind on purpose: a
file that can only be interpreted with the database open is not self-describing,
and losing that property was the main thing that made the sidecar design
unattractive. storage_gc's orphan report reads the descriptor straight off disk
for exactly this reason.

The block is padded rather than tightly packed so the preview can be re-encoded
later (a different size, a different codec) with one 64 KiB pwrite at offset 0,
instead of rewriting a 45 MB payload -- `preview_len` is what makes the actual
size a runtime value instead of something baked into the format. Measured
worst case across the real library is 30.5 KB at WebP q90, so capacity is
roughly 2x the largest preview anyone has needed.

PREFIX_SIZE is a power of two so the payload behind it stays block-aligned on
disk; a 2 KiB-ish prefix would have knocked every subsequent read of a 45 MB
file off its 4 KiB boundary.
"""

import struct
from dataclasses import dataclass, field
from pathlib import Path

from app.db.models import AssetKind

MAGIC = b"COAP"
VERSION = 1

HEADER_SIZE = 32
PREFIX_SIZE = 64 * 1024
PREVIEW_CAPACITY = PREFIX_SIZE - HEADER_SIZE  # 65504
DESCRIPTOR_SIZE = HEADER_SIZE - 8  # 24

# Stable on-disk codes. Deliberately not AssetKind's string values or its
# declaration order -- renaming or reordering the enum must never change how an
# already-written file parses.
_KIND_TO_CODE = {
    AssetKind.image: 1,
    AssetKind.mask: 2,
    AssetKind.mesh: 3,
    AssetKind.other: 4,
}
_CODE_TO_KIND = {code: kind for kind, code in _KIND_TO_CODE.items()}

# Which descriptor layout a kind uses. image and mask share one: a mask is a
# picture with the same pixel dimensions as any other, and the discriminator
# still records which of the two it actually is.
_RASTER_KINDS = (AssetKind.image, AssetKind.mask)


@dataclass(frozen=True)
class AssetHeader:
    kind: AssetKind
    version: int
    preview_len: int
    descriptor: dict = field(default_factory=dict)

    @property
    def has_preview(self) -> bool:
        return self.preview_len > 0


def _pack_descriptor(kind: AssetKind, descriptor: dict) -> bytes:
    if kind in _RASTER_KINDS:
        body = struct.pack("<II", int(descriptor.get("width", 0)), int(descriptor.get("height", 0)))
    elif kind is AssetKind.mesh:
        body = struct.pack("<II", int(descriptor.get("verts", 0)), int(descriptor.get("faces", 0)))
    else:
        body = b""
    return body.ljust(DESCRIPTOR_SIZE, b"\0")


def _unpack_descriptor(kind: AssetKind, body: bytes) -> dict:
    if kind in _RASTER_KINDS:
        width, height = struct.unpack_from("<II", body, 0)
        return {"width": width, "height": height}
    if kind is AssetKind.mesh:
        verts, faces = struct.unpack_from("<II", body, 0)
        return {"verts": verts, "faces": faces}
    return {}


def build_prefix(kind: AssetKind, preview: bytes, descriptor: dict) -> bytes:
    """The complete PREFIX_SIZE-byte block to write in front of the payload."""
    if len(preview) > PREVIEW_CAPACITY:
        raise ValueError(f"preview of {len(preview)} B exceeds {PREVIEW_CAPACITY} B capacity")
    header = MAGIC + struct.pack("<BBH", VERSION, _KIND_TO_CODE[kind], len(preview))
    block = header + _pack_descriptor(kind, descriptor) + preview
    return block.ljust(PREFIX_SIZE, b"\0")


def parse_header(head: bytes) -> AssetHeader | None:
    """None for anything that isn't one of our files -- which is the normal
    answer for every asset written before this shipped, and the signal that
    the whole file is payload."""
    if len(head) < HEADER_SIZE or head[:4] != MAGIC:
        return None
    version, kind_code, preview_len = struct.unpack_from("<BBH", head, 4)
    kind = _CODE_TO_KIND.get(kind_code)
    if kind is None or version > VERSION:
        # A newer writer than this reader, or a code we don't know: treat the
        # file as un-prefixed rather than guessing at a layout we can't parse.
        return None
    if preview_len > PREVIEW_CAPACITY:
        return None
    return AssetHeader(
        kind=kind,
        version=version,
        preview_len=preview_len,
        descriptor=_unpack_descriptor(kind, head[8:HEADER_SIZE]),
    )


def read_header(path: Path) -> AssetHeader | None:
    try:
        with path.open("rb") as f:
            return parse_header(f.read(HEADER_SIZE))
    except OSError:
        return None


def read_preview(path: Path) -> bytes | None:
    """The stored WebP, or None if this file has no prefix or carries no
    preview (a kind we can't render, or a picture already smaller than one)."""
    try:
        with path.open("rb") as f:
            header = parse_header(f.read(HEADER_SIZE))
            if header is None or not header.has_preview:
                return None
            return f.read(header.preview_len)
    except OSError:
        return None


def payload_offset(path: Path) -> int:
    """Where the real file starts: 0 for anything not yet migrated."""
    return PREFIX_SIZE if read_header(path) is not None else 0
