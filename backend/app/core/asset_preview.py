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
import io
from collections import OrderedDict
from pathlib import Path
from typing import Callable

from PIL import Image, ImageOps, UnidentifiedImageError

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
    """The honest answer for a kind we can't turn into a picture yet.

    mesh belongs here for now: rendering one server-side would mean a new
    heavyweight dependency, and Model3DThumb.tsx already renders it in the
    browser. The slot is what matters -- when a mesh (or a video) does grow a
    producer, the format already accepts the raster and the descriptor already
    has a layout waiting for its vertex/face counts."""

    def wants_preview(self, path: Path) -> bool:
        return False

    def build(self, data: bytes, capacity: int) -> tuple[bytes, dict] | None:
        return None


PREVIEW_PRODUCERS: dict[AssetKind, PreviewProducer] = {
    AssetKind.image: RasterPreviewProducer(),
    AssetKind.mask: RasterPreviewProducer(),
    AssetKind.mesh: NoPreviewProducer(),
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
# produced on demand rather than stored, since only pictures a doc actually
# embeds ever need it, and kept in a small in-memory LRU (a restart just
# re-renders; the browser keeps its own copy by ETag anyway).
FIT_EDGE = 1600
FIT_QUALITY = 85
_FIT_CACHE_ENTRIES = 48
_fit_cache: OrderedDict[str, bytes] = OrderedDict()


def build_fit(data: bytes, edge: int = FIT_EDGE) -> bytes | None:
    """The whole picture scaled to fit `edge` on its long side, as WebP. None
    when it isn't a raster or is already that small -- the original is then the
    right thing to serve."""
    try:
        with Image.open(io.BytesIO(data)) as img:
            img = ImageOps.exif_transpose(img) or img
            if max(img.size) <= edge:
                return None
            mode = "RGBA" if (img.mode in ("RGBA", "LA") or "transparency" in img.info) else "RGB"
            img = img.convert(mode)
            # draft-free: thumbnail() keeps the aspect ratio and never upsizes.
            img.thumbnail((edge, edge), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, "WEBP", quality=FIT_QUALITY, method=4)
            return buf.getvalue()
    except (UnidentifiedImageError, OSError, ValueError):
        return None


async def fit_rendition(key: str, load: Callable[[], bytes]) -> bytes | None:
    """Cached build_fit for the asset stored under `key`. `load` is only called
    on a miss -- reading an 8K original off disk is the expensive half."""
    if key in _fit_cache:
        _fit_cache.move_to_end(key)
        return _fit_cache[key]
    async with _decode_semaphore:
        data = await asyncio.to_thread(load)
        fitted = await asyncio.to_thread(build_fit, data)
    if fitted is not None:
        _fit_cache[key] = fitted
        while len(_fit_cache) > _FIT_CACHE_ENTRIES:
            _fit_cache.popitem(last=False)
    return fitted
