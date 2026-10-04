"""Producing the raster that goes into an asset file's prefix block, one
producer per AssetKind.

A preview is *always a picture*, whatever the payload is: a mesh would be a
rendered still, a video a grabbed frame. That is why the kind never reaches
asset_prefix.py as anything but a discriminator -- the block itself only ever
holds WebP bytes, and only the producer here is kind-aware. Adding a kind means
adding one class, not a branch at each call site (same reasoning as
core/asset_types.py, which does this for the asset *node* kinds).

Producers that return None mean "no picture for this one", and the caller then
writes no prefix at all rather than a 64 KiB block of padding around nothing.
That is deliberately also the answer for a picture that is already smaller than
the preview would be: there is nothing to downscale, and the original is
already cheap to load.
"""

import asyncio
import hashlib
import io
import re
from collections import OrderedDict
from pathlib import Path
from typing import Awaitable, Callable

import resvg_py
from PIL import Image, ImageOps, UnidentifiedImageError

from app.config import get_settings
from app.core import glb_stats
from app.db.models import AssetKind

# The grid renders an asset's face at 118x118 CSS px (a 260px .node-cell minus
# padding, halved by .output-grid's two columns). Worst case in device pixels is
# that times the grid's maximum 1.4x zoom times a 2x display = 330, so 384 is
# the next sensible size up. .output-item img is `aspect-ratio: 1;
# object-fit: cover`, i.e. the grid always shows a centre square -- so the
# preview stores that square and nothing else. Pixels outside it are never
# rendered anywhere that uses a preview.
PREVIEW_EDGE = 384

# Tried in order until one fits PREVIEW_CAPACITY. Measured across the whole
# library, q90 peaks at ~30.5 KB against 65504 B of capacity, so the lower rungs
# are a guard rather than a working path.
QUALITY_LADDER = (90, 80, 70, 60)

# Pillow releases the GIL while decoding, so these genuinely run in parallel.
# Three of the box's four cores, leaving one for the event loop and the
# in-process job queue.
DECODE_CONCURRENCY = 3


class PreviewProducer:
    """Returns (webp_bytes, descriptor) for one asset kind, or None when that
    kind has no picture to show."""

    def wants_preview(self, path: Path) -> bool:
        """Cheap gate, decided from the file header alone -- the caller uses it
        to avoid pulling 45 MB off disk only to learn there was nothing to do.
        Pillow's Image.open reads just enough to identify the image; it does not
        decode pixels until .load()."""
        return False

    def build(self, data: bytes, capacity: int) -> tuple[bytes, dict] | None:
        raise NotImplementedError


class RasterPreviewProducer(PreviewProducer):
    """image and mask. A mask is a picture with the same dimensions as any
    other and is rendered through the same <img>, so it gets the same
    treatment -- it is the pixel count that costs, not the semantics, and the
    masks in this library are 7680x4320 despite being a couple of KB on disk."""

    def wants_preview(self, path: Path) -> bool:
        try:
            with Image.open(path) as img:
                width, height = img.size
        except (UnidentifiedImageError, OSError):
            return False
        return min(width, height) > PREVIEW_EDGE

    def build(self, data: bytes, capacity: int) -> tuple[bytes, dict] | None:
        try:
            with Image.open(io.BytesIO(data)) as img:
                # Browsers honour EXIF orientation, so a preview that ignores it
                # would be silently rotated relative to the original it stands in for.
                img = ImageOps.exif_transpose(img) or img
                width, height = img.size
                if min(width, height) <= PREVIEW_EDGE:
                    return None
                edge = min(width, height)
                left, top = (width - edge) // 2, (height - edge) // 2
                # Alpha is load-bearing here: native.mask bakes its painted
                # holes into the source image's alpha channel, so dropping to
                # RGB would show a preview of something the app never displays.
                mode = "RGBA" if (img.mode in ("RGBA", "LA") or "transparency" in img.info) else "RGB"
                thumb = img.convert(mode).resize(
                    (PREVIEW_EDGE, PREVIEW_EDGE), Image.LANCZOS, box=(left, top, left + edge, top + edge)
                )
        except (UnidentifiedImageError, OSError, ValueError):
            return None

        descriptor = {"width": width, "height": height}
        for quality in QUALITY_LADDER:
            buf = io.BytesIO()
            thumb.save(buf, "WEBP", quality=quality, method=4)
            encoded = buf.getvalue()
            if len(encoded) <= capacity:
                return encoded, descriptor
        return None


class NoPreviewProducer(PreviewProducer):
    """The honest answer for a kind we can't turn into a picture yet. A video
    gets its own producer when it grows one; the format already accepts the
    raster for it."""

    def wants_preview(self, path: Path) -> bool:
        return False

    def build(self, data: bytes, capacity: int) -> tuple[bytes, dict] | None:
        return None


class MeshProducer(PreviewProducer):
    """A mesh has no picture: rendering one server-side would mean a heavyweight
    dependency, and the browser renders it (MeshThumb). What it *does* get is a
    descriptor -- its vertex and triangle counts, which the UI can't learn
    without loading the whole file. The preview is empty, so the prefix block
    carries the counts and no WebP (read_preview returns None for it)."""

    def wants_preview(self, path: Path) -> bool:
        return False

    def build(self, data: bytes, capacity: int) -> tuple[bytes, dict] | None:
        counts = glb_stats.counts_from_bytes(data)
        if counts is None:
            return None
        vertices, triangles = counts
        return b"", {"verts": vertices, "faces": triangles}


PREVIEW_PRODUCERS: dict[AssetKind, PreviewProducer] = {
    AssetKind.image: RasterPreviewProducer(),
    AssetKind.mask: RasterPreviewProducer(),
    AssetKind.mesh: MeshProducer(),
    AssetKind.other: NoPreviewProducer(),
}

_decode_semaphore = asyncio.Semaphore(DECODE_CONCURRENCY)


def wants_preview(kind: AssetKind, path: Path) -> bool:
    producer = PREVIEW_PRODUCERS.get(kind)
    return producer.wants_preview(path) if producer else False


def build_preview(kind: AssetKind, data: bytes, capacity: int) -> tuple[bytes, dict] | None:
    producer = PREVIEW_PRODUCERS.get(kind)
    return producer.build(data, capacity) if producer else None


async def build_preview_async(kind: AssetKind, data: bytes, capacity: int) -> tuple[bytes, dict] | None:
    """Decoding an 8K PNG costs ~1 s and ~130 MB, so it never runs on the event
    loop -- nothing else in this single-worker process offloads sync work, and a
    blocking read here is exactly what made grid actions queue behind image
    loads once before (see the FileResponse work in api/routes/assets.py)."""
    async with _decode_semaphore:
        return await asyncio.to_thread(build_preview, kind, data, capacity)


# --- page-sized renditions ----------------------------------------------------
# The prefix preview above is a centre *square*, right for a grid cell and
# wrong for anything that shows the whole picture: in the design doc a 16:9
# character chart came out as its middle two columns. A page wants the whole
# frame at a width a page can use, so this is a second, uncropped size --
# produced on demand rather than stored in the asset, since only pictures a
# doc or the board actually show ever need it.
#
# Cached twice: in memory (a small LRU) and on disk under settings.cache_dir,
# because for an SVG the render is the expensive part (seconds, see below) and
# a deploy restarts the process. The browser keeps its own copy by ETag.
FIT_EDGE = 1600
FIT_QUALITY = 85
# Bump to invalidate every cached rendition after changing how they're made.
_FIT_VERSION = "2"
_FIT_CACHE_ENTRIES = 48
_fit_cache: OrderedDict[str, bytes] = OrderedDict()
_fit_locks: dict[str, asyncio.Lock] = {}

# --- SVG ---------------------------------------------------------------------
# An SVG is kept as-is -- it's the original, and opening it full size still
# shows the real vector. But a browser re-rasterises one on every repaint at a
# new scale, and a city map with ~45k shapes, 73 masks and ~400 clip paths
# made the board crawl on every zoom step. So everywhere a picture is merely
# *shown* (board sticker, grid cell, design doc) gets a raster of it instead,
# drawn once here with resvg (pure Rust, no system cairo). Larger than FIT_EDGE,
# since a map is exactly the kind of picture people zoom into.
SVG_FIT_EDGE = 2400
_SVG_HEAD = 2048
_SVG_SIZE = re.compile(rb'viewBox\s*=\s*["\']\s*[-\d.]+[\s,]+[-\d.]+[\s,]+([\d.]+)[\s,]+([\d.]+)')


def is_svg(head: bytes) -> bool:
    """Sniffed from the bytes, not trusted from a mime type: an SVG uploaded
    as application/octet-stream is still one."""
    head = head[:_SVG_HEAD].lstrip(b"\xef\xbb\xbf \t\r\n")
    return head.startswith((b"<?xml", b"<svg", b"<!--", b"<!DOCTYPE svg")) and b"<svg" in head


def rasterize_svg(data: bytes, long_edge: int = SVG_FIT_EDGE) -> Image.Image | None:
    m = _SVG_SIZE.search(data[:_SVG_HEAD])
    w, h = (float(m.group(1)), float(m.group(2))) if m else (1.0, 1.0)
    size = {"width": long_edge} if w >= h else {"height": long_edge}
    try:
        png = resvg_py.svg_to_bytes(
            # A string, never a path: with no resources_dir an external
            # href can't make the renderer read files off this box.
            svg_string=data.decode("utf-8", errors="replace"),
            # The box has DejaVu only; without these a generic "serif" asks
            # for Times New Roman and the labels silently disappear.
            font_family="DejaVu Sans",
            serif_family="DejaVu Serif",
            sans_serif_family="DejaVu Sans",
            monospace_family="DejaVu Sans Mono",
            **size,
        )
        return Image.open(io.BytesIO(bytes(png)))
    except (ValueError, UnidentifiedImageError, OSError):
        return None


def build_fit(data: bytes, edge: int = FIT_EDGE) -> bytes | None:
    """The whole picture scaled to fit `edge` on its long side, as WebP. None
    when it isn't a raster or is already that small -- the original is then the
    right thing to serve. An SVG is always rasterised (see above)."""
    svg = is_svg(data)
    try:
        if svg:
            img = rasterize_svg(data)
            if img is None:
                return None
            edge = SVG_FIT_EDGE
        else:
            img = Image.open(io.BytesIO(data))
            img = ImageOps.exif_transpose(img) or img
            if max(img.size) <= edge:
                return None
        mode = "RGBA" if (img.mode in ("RGBA", "LA") or "transparency" in img.info) else "RGB"
        img = img.convert(mode)
        # thumbnail() keeps the aspect ratio and never upsizes.
        img.thumbnail((edge, edge), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, "WEBP", quality=FIT_QUALITY, method=4)
        return buf.getvalue()
    except (UnidentifiedImageError, OSError, ValueError):
        return None


def build_square_preview(fitted: bytes) -> bytes | None:
    """The grid's centre square (same shape as the prefix preview) cut from an
    existing rendition -- for an SVG, whose prefix block holds no preview."""
    try:
        with Image.open(io.BytesIO(fitted)) as img:
            width, height = img.size
            edge = min(width, height)
            left, top = (width - edge) // 2, (height - edge) // 2
            thumb = img.resize((PREVIEW_EDGE, PREVIEW_EDGE), Image.LANCZOS, box=(left, top, left + edge, top + edge))
            buf = io.BytesIO()
            thumb.save(buf, "WEBP", quality=QUALITY_LADDER[0], method=4)
            return buf.getvalue()
    except (UnidentifiedImageError, OSError, ValueError):
        return None


def _disk_path(key: str) -> Path:
    name = hashlib.sha1(f"{_FIT_VERSION}:{key}".encode()).hexdigest()
    return Path(get_settings().cache_dir) / "renditions" / name[:2] / f"{name}.webp"


def _read_disk(key: str) -> bytes | None:
    try:
        return _disk_path(key).read_bytes()
    except OSError:
        return None


def _write_disk(key: str, data: bytes) -> None:
    path = _disk_path(key)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)
    except OSError:
        pass  # a cache that can't be written is just a slower cache


def _remember(key: str, data: bytes) -> None:
    _fit_cache[key] = data
    _fit_cache.move_to_end(key)
    while len(_fit_cache) > _FIT_CACHE_ENTRIES:
        _fit_cache.popitem(last=False)


async def _cached(key: str, produce: Callable[[], Awaitable[bytes | None]]) -> bytes | None:
    if key in _fit_cache:
        _fit_cache.move_to_end(key)
        return _fit_cache[key]
    # One render per key at a time: a board full of the same map shouldn't
    # rasterise it once per sticker.
    async with _fit_locks.setdefault(key, asyncio.Lock()):
        if key in _fit_cache:
            return _fit_cache[key]
        data = await asyncio.to_thread(_read_disk, key)
        if data is None:
            data = await produce()
            if data is not None:
                await asyncio.to_thread(_write_disk, key, data)
        if data is not None:
            _remember(key, data)
        return data


async def fit_rendition(key: str, load: Callable[[], bytes]) -> bytes | None:
    """Cached build_fit for the asset stored under `key`. `load` is only called
    on a miss -- reading an 8K original off disk is the expensive half, or for
    an SVG the render is."""

    async def produce() -> bytes | None:
        async with _decode_semaphore:
            data = await asyncio.to_thread(load)
            return await asyncio.to_thread(build_fit, data)

    return await _cached(key + "#fit", produce)


async def svg_preview(key: str, load: Callable[[], bytes]) -> bytes | None:
    """The grid-square preview of an SVG, cut from its (cached) rendition."""

    async def produce() -> bytes | None:
        fitted = await fit_rendition(key, load)
        return await asyncio.to_thread(build_square_preview, fitted) if fitted else None

    return await _cached(key + "#preview", produce)
