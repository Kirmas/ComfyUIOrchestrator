"""NativeBackend: JobBackend implementation for node types that need no
external model/GPU backend at all -- pure server-side computation (e.g. image
compositing with Pillow). Mirrors api_backend.py's shape: NativeBackend
handles the JobBackend plumbing (in-memory result cache, since the work is
synchronous from the caller's point of view -- submit() already has the full
result by the time it returns a job id), concrete subclasses implement just
the actual computation via `_run()`.

Unlike ComfyUIBackend, there's no workflow_json/param_mapping indirection --
resolve_node_inputs (worker/tasks.py) already hands submit() a flat dict keyed
by the node type's own param_schema field names, so `_run()` reads `inputs`
directly by those names.
"""
import base64
import math
import uuid
from io import BytesIO
from typing import Any

import numpy as np
from PIL import Image, ImageChops, ImageFilter

from app.core.job_backend import AssetRef, CapacityInfo, JobStatus

# In-memory result cache for the lifetime of the worker process -- same
# reasoning as api_backend.py's _RESULTS/_ERRORS.
_RESULTS: dict[str, list[AssetRef]] = {}
_ERRORS: dict[str, str] = {}


class NativeBackend:
    async def submit(self, execution_config: dict, inputs: dict[str, Any]) -> str:
        job_id = str(uuid.uuid4())
        try:
            assets = await self._run(execution_config, inputs)
            _RESULTS[job_id] = assets
        except Exception as exc:
            _ERRORS[job_id] = str(exc)
        return job_id

    async def status(self, job_id: str) -> JobStatus:
        if job_id in _ERRORS:
            return JobStatus.error
        if job_id in _RESULTS:
            return JobStatus.done
        return JobStatus.pending

    async def error_detail(self, job_id: str) -> str | None:
        return _ERRORS.get(job_id)

    async def result(self, job_id: str) -> list[AssetRef]:
        return _RESULTS.pop(job_id, [])

    async def capacity(self) -> CapacityInfo:
        return CapacityInfo(is_alive=True, queue_length=0, max_queue_length=None)

    async def cancel(self, job_id: str) -> None:
        _RESULTS.pop(job_id, None)
        _ERRORS.pop(job_id, None)

    async def _run(self, execution_config: dict, inputs: dict[str, Any]) -> list[AssetRef]:
        raise NotImplementedError


class ChartComposer:
    """Pure image-composition logic for a 4-head/4-body reference chart --
    no JobBackend/async concerns here, just PIL, so it's independently
    testable/reusable without going through the JobBackend plumbing.

    Layout: one row of 4 head cells on top, one row of 4 body cells below,
    stacked into canvas_width x canvas_height. head_height_factor scales the
    head row's height relative to cell width (1.0 = square head cells); the
    body row absorbs whatever height is left. Mirrors the math a hand-built
    ComfyUI chart-assembly workflow used (PrimitiveInt width/height,
    ComfyMathExpression for cell size) -- see the reference workflow this was
    modeled on.
    """

    def __init__(self, canvas_width: int, canvas_height: int, head_height_factor: float = 1.0):
        self.canvas_width = canvas_width
        self.canvas_height = canvas_height
        self.cell_width = canvas_width // 4
        self.head_height = round(self.cell_width * head_height_factor)
        self.body_height = canvas_height - self.head_height

    @staticmethod
    def _cover_crop(img: Image.Image, width: int, height: int) -> Image.Image:
        """Scale to fully cover (width, height), then center-crop the overflow --
        same fit as ComfyUI's ImageScale node with crop="center"."""
        src_w, src_h = img.size
        scale = max(width / src_w, height / src_h)
        new_w, new_h = max(1, round(src_w * scale)), max(1, round(src_h * scale))
        resized = img.resize((new_w, new_h), Image.LANCZOS)
        left = (new_w - width) // 2
        top = (new_h - height) // 2
        return resized.crop((left, top, left + width, top + height))

    def _band(self, images: list[Image.Image], height: int) -> Image.Image:
        band = Image.new("RGB", (self.canvas_width, height), "white")
        for i, img in enumerate(images):
            cell = self._cover_crop(img, self.cell_width, height)
            band.paste(cell, (i * self.cell_width, 0))
        return band

    def compose(self, heads: list[Image.Image], bodies: list[Image.Image]) -> Image.Image:
        top = self._band(heads, self.head_height)
        bottom = self._band(bodies, self.body_height)
        canvas = Image.new("RGB", (self.canvas_width, self.canvas_height), "white")
        canvas.paste(top, (0, 0))
        canvas.paste(bottom, (0, self.head_height))
        return canvas


class CharacterChartBackend(NativeBackend):
    async def _run(self, execution_config: dict, inputs: dict[str, Any]) -> list[AssetRef]:
        width = int(inputs.get("width", 7680))
        height = int(inputs.get("height", 4320))
        head_height_factor = float(inputs.get("head_height_factor", 1.0))

        heads = [Image.open(BytesIO(inputs[f"head_{i}"])).convert("RGB") for i in range(1, 5)]
        bodies = [Image.open(BytesIO(inputs[f"body_{i}"])).convert("RGB") for i in range(1, 5)]

        composer = ChartComposer(width, height, head_height_factor)
        canvas = composer.compose(heads, bodies)

        buf = BytesIO()
        canvas.save(buf, format="PNG")
        return [AssetRef(data=buf.getvalue(), mime_type="image/png", kind="image")]


class CropBackend(NativeBackend):
    async def _run(self, execution_config: dict, inputs: dict[str, Any]) -> list[AssetRef]:
        image = Image.open(BytesIO(inputs["image"])).convert("RGB")
        x = int(inputs.get("crop_x", 0))
        y = int(inputs.get("crop_y", 0))
        width = int(inputs.get("crop_width", image.width))
        height = int(inputs.get("crop_height", image.height))
        cropped = image.crop((x, y, x + width, y + height))

        buf = BytesIO()
        cropped.save(buf, format="PNG")
        return [AssetRef(data=buf.getvalue(), mime_type="image/png", kind="image")]


# Smallest margin kept around the painted region when windowing the work
# below -- enough for a resampling filter's support to fall outside every
# pixel the mask actually uses.
_WINDOW_PAD = 4

# The seam softening an untouched node runs with. node_types.py's
# "transplant" entry declares it as that field's schema default -- which is
# what the param form shows and what the editor previews -- so the value the
# user sees and the value a never-edited node actually runs with are one
# constant, not two that can drift.
DEFAULT_TRANSPLANT_FEATHER = 4.0


def transplant(target: Image.Image, source: Image.Image, mask: Image.Image, feather: float = 0.0) -> Image.Image:
    """Paste the masked region of `source` into `target` at the same
    coordinates, softening the seam by `feather` pixels.

    Pure image logic, split out of TransplantBackend the same way ChartComposer
    is split out of CharacterChartBackend. `mask` is an "L" image the size of
    `target` (white = take this from source); `target` is mutated in place and
    returned -- callers here always own a freshly-opened copy, and a full-frame
    .copy() of a 4K RGB image is 25 MB this box would rather not spend.

    Everything except the mask upscale is windowed to the painted region's
    bounding box: on a memory-tight box a "transfer the head" edit shouldn't
    cost a full-frame resize of a source image that may be much larger than
    the area actually being used (see memory/memory_tight_box_array_scale.md).
    The window is padded by 3*feather (never less than _WINDOW_PAD) so the
    blur inside it matches what a whole-image blur would have produced --
    beyond 3 sigma a Gaussian contributes nothing, and the mask is empty out
    there anyway. The same padding keeps the resampling filter's edge
    clamping away from any pixel the mask actually uses.
    """
    bbox = mask.getbbox()
    if bbox is None:
        return target

    pad = max(_WINDOW_PAD, int(round(feather * 3)))
    left = max(0, bbox[0] - pad)
    top = max(0, bbox[1] - pad)
    right = min(target.width, bbox[2] + pad)
    bottom = min(target.height, bbox[3] + pad)
    if right <= left or bottom <= top:
        return target
    window = (right - left, bottom - top)

    # Scale only the part of the source this window needs, via resize()'s own
    # `box` (a *float* region in source coordinates) rather than crop-then-
    # resize. The difference matters: an integer crop first would land the
    # resample grid up to a pixel away from where resizing the whole source
    # puts it, and a sub-pixel shift is exactly what "transfer the fine
    # detail I lost" cannot afford. With `box` the window is resampled on the
    # full image's grid, so the result matches a whole-image resize.
    scale_x = source.width / target.width
    scale_y = source.height / target.height
    if (scale_x, scale_y) == (1.0, 1.0):
        patch = source.crop((left, top, right, bottom))
    else:
        patch = source.resize(window, Image.LANCZOS, box=(left * scale_x, top * scale_y, right * scale_x, bottom * scale_y))

    window_mask = mask.crop((left, top, right, bottom))
    if feather > 0:
        window_mask = window_mask.filter(ImageFilter.GaussianBlur(feather))

    target.paste(patch, (left, top), window_mask)
    return target


class TransplantBackend(NativeBackend):
    """Two images in, one out: keeps `target` everywhere except the painted
    region, which is taken from `source` at the same coordinates.

    The use case is a regenerated image that got a pose right but lost detail
    somewhere it wasn't supposed to change -- paint over that area and the
    earlier image's version of it comes back. The frontend editor
    (TransplantPreview.tsx) stacks the two as layers and paints holes in the
    top one, so what's painted is literally what shows through.

    transplant_png is a params-stored bilevel PNG, not a slot -- same storage
    and same small capped resolution as native.mask's mask_png (see
    MaskBackend), nearest-neighbor-upscaled back to the target's real size so
    a painted edge stays an edge; transplant_feather then softens it
    deliberately, by a stated number of pixels, rather than inheriting
    whatever gradient an interpolated upscale happened to leave.
    """

    async def _run(self, execution_config: dict, inputs: dict[str, Any]) -> list[AssetRef]:
        target = Image.open(BytesIO(inputs["target"])).convert("RGB")
        mask_png = inputs.get("transplant_png")

        if mask_png:
            source = Image.open(BytesIO(inputs["source"])).convert("RGB")
            mask = Image.open(BytesIO(base64.b64decode(mask_png))).convert("L")
            if mask.size != target.size:
                mask = mask.resize(target.size, Image.NEAREST)
            # Repeated here rather than read from the registry's `defaults`,
            # which resolve_node_inputs only applies to fixed image fields --
            # same as CharacterChartBackend's width/height. An explicit 0
            # (hard edge) has to survive, so this can't be an `or`.
            feather = inputs.get("transplant_feather")
            result = transplant(target, source, mask, DEFAULT_TRANSPLANT_FEATHER if feather is None else float(feather))
        else:
            # Nothing painted yet: pass the target through unchanged rather
            # than failing, so the node is runnable before it's been edited
            # (same posture as MaskBackend's empty-mask branch).
            result = target

        buf = BytesIO()
        result.save(buf, format="PNG")
        return [AssetRef(data=buf.getvalue(), mime_type="image/png", kind="image")]


class MaskBackend(NativeBackend):
    """Outputs the painted mask itself -- a single-channel grayscale PNG,
    kind="mask" -- rather than the source image with the mask baked into its
    alpha channel. Same AssetKind.mask convention comfyui_backend.py's
    MaskToImage detection produces for a SAM3-style generated mask (see the
    "get mask as its own asset" design thread), so a hand-painted mask and a
    model-generated one are indistinguishable to every downstream consumer:
    both round-trip through LoadImage -> ImageToMask(channel=red) the same
    way (ComfyUI's LoadImage always expands a grayscale file back to R=G=B).

    mask_png (params, not a slot -- see node_types.py's "mask" entry) is a
    frontend-painted bilevel PNG capped at a small resolution regardless of
    the source image's real size (see MaskPreview.tsx); nearest-neighbor
    resize back up to the source size keeps its edges hard rather than
    introducing gradient values a painted mask never had. Painted (white)
    pixels are the masked region -- same polarity MaskToImage itself uses
    (mask value 1 -> white), so no inversion is needed on the way out.

    Used to bake into the source's alpha channel instead, matching ComfyUI's
    own clipspace-painted-masked convention -- dropped once every downstream
    consumer in this app moved to consuming a real mask asset instead of a
    pre-composited RGBA file."""

    async def _run(self, execution_config: dict, inputs: dict[str, Any]) -> list[AssetRef]:
        image = Image.open(BytesIO(inputs["image"]))
        mask_png = inputs.get("mask_png")
        if mask_png:
            mask = Image.open(BytesIO(base64.b64decode(mask_png))).convert("L")
            if mask.size != image.size:
                mask = mask.resize(image.size, Image.NEAREST)
        else:
            mask = Image.new("L", image.size, 0)

        buf = BytesIO()
        mask.save(buf, format="PNG")
        return [AssetRef(data=buf.getvalue(), mime_type="image/png", kind="mask")]


class MergeMaskBackend(NativeBackend):
    """Union (OR) of two mask images into one -- e.g. combining a SAM3-detected
    mask with a hand-painted native.mask, or two separate detections, into a
    single mask a downstream inpaint/mask-consuming step can use. Same
    AssetKind.mask output convention as MaskBackend (single-channel grayscale
    PNG, lit/white = masked region), so the result is interchangeable with any
    other mask asset everywhere downstream.

    Both slots are plain "image" fields (like crop's/transplant's own image
    slots), not mask_png/layer_mask params -- unlike MaskBackend/
    TransplantBackend, there's nothing painted in this node's own editor:
    mask_a/mask_b are existing mask assets already sitting in other grid
    cells, so they arrive already at full resolution and just need combining,
    not upscaling from a small painted canvas.
    """

    async def _run(self, execution_config: dict, inputs: dict[str, Any]) -> list[AssetRef]:
        mask_a = Image.open(BytesIO(inputs["mask_a"])).convert("L")
        mask_b = Image.open(BytesIO(inputs["mask_b"])).convert("L")
        if mask_b.size != mask_a.size:
            # Nearest-neighbor, same reasoning as MaskBackend's own resize:
            # a mask is bilevel, an interpolated resize would introduce
            # gradient values along the edge that were never actually there.
            mask_b = mask_b.resize(mask_a.size, Image.NEAREST)
        merged = ImageChops.lighter(mask_a, mask_b)

        buf = BytesIO()
        merged.save(buf, format="PNG")
        return [AssetRef(data=buf.getvalue(), mime_type="image/png", kind="mask")]


def _label_components(fg: np.ndarray) -> list[np.ndarray]:
    """Splits a boolean foreground array into its 8-connected components, one
    boolean array per component (same shape as `fg`, True only where that
    component's own pixels are).

    No scipy/opencv in this project (see requirements.txt) so there's no
    `ndimage.label` to reach for -- this is a plain iterative flood fill
    (explicit stack, not recursion, to avoid Python's recursion limit on a
    large blob) seeded from each not-yet-visited foreground pixel. Its cost
    is O(number of foreground pixels) total, each visited once, regardless of
    the full image's size or the component's shape -- unlike a distance-
    transform/dilation-based labeling scheme, whose cost tracks a component's
    *diameter* against the *whole* array on every pass. A painted mask's
    foreground is normally a small fraction of the canvas, which is the case
    this bound is actually good for.
    """
    visited = np.zeros_like(fg, dtype=bool)
    h, w = fg.shape
    components: list[np.ndarray] = []
    ys, xs = np.nonzero(fg)
    for sy, sx in zip(ys.tolist(), xs.tolist()):
        if visited[sy, sx]:
            continue
        stack = [(sy, sx)]
        visited[sy, sx] = True
        coords: list[tuple[int, int]] = []
        while stack:
            y, x = stack.pop()
            coords.append((y, x))
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    if dy == 0 and dx == 0:
                        continue
                    ny, nx = y + dy, x + dx
                    if 0 <= ny < h and 0 <= nx < w and fg[ny, nx] and not visited[ny, nx]:
                        visited[ny, nx] = True
                        stack.append((ny, nx))
        comp = np.zeros_like(fg)
        comp_ys, comp_xs = zip(*coords)
        comp[list(comp_ys), list(comp_xs)] = True
        components.append(comp)
    return components


# Below this, "scale" collapses every foreground pixel of a component onto
# its own centroid rather than erroring on a division by zero -- shrinking a
# region past ~0 is still a well-defined (if degenerate) request. A
# scale_percent <= -100 (factor <= 0) clamps here too, rather than going
# negative: a negative factor would mirror the region through its own
# centroid, which "shrink by more than 100%" was never asking for.
_MIN_SCALE_FACTOR = 0.01


def _scale_component(component: np.ndarray, factor: float) -> np.ndarray:
    """Radially scales one connected component about its own centroid by
    `factor` (1.0 = unchanged, >1 grows, <1 shrinks) -- a true geometric
    scale, not a fixed-width boundary dilation, so a component far from its
    own center moves (and grows) more than one close to it.

    Windowed to the component's own (post-scale) bounding box, the same
    windowing discipline `transplant()` uses above -- a paint-mask edit
    shouldn't cost a full-frame array op when only a small region actually
    changes (memory/memory_tight_box_array_scale.md). Rendered via an inverse
    warp (for each *output* pixel, nearest-neighbor-sample the *source* pixel
    that scales onto it) rather than a forward warp, which would leave holes
    once factor > 1 spreads source pixels further apart than one unit.
    Nearest-neighbor, not bilinear, to keep the result bilevel -- same
    reasoning as every other mask resize in this module.
    """
    factor = max(factor, _MIN_SCALE_FACTOR)
    h, w = component.shape
    ys, xs = np.nonzero(component)
    cy, cx = float(ys.mean()), float(xs.mean())

    corners_y = np.array([ys.min(), ys.min(), ys.max(), ys.max()], dtype=np.float64)
    corners_x = np.array([xs.min(), xs.max(), xs.min(), xs.max()], dtype=np.float64)
    scaled_y = cy + (corners_y - cy) * factor
    scaled_x = cx + (corners_x - cx) * factor
    pad = 1  # rounding margin for the nearest-neighbor sampling below
    top = max(0, int(math.floor(scaled_y.min())) - pad)
    bottom = min(h, int(math.ceil(scaled_y.max())) + pad + 1)
    left = max(0, int(math.floor(scaled_x.min())) - pad)
    right = min(w, int(math.ceil(scaled_x.max())) + pad + 1)
    out = np.zeros_like(component)
    if bottom <= top or right <= left:
        return out

    out_y, out_x = np.mgrid[top:bottom, left:right].astype(np.float64)
    src_y = np.round(cy + (out_y - cy) / factor).astype(np.int64)
    src_x = np.round(cx + (out_x - cx) / factor).astype(np.int64)
    in_bounds = (src_y >= 0) & (src_y < h) & (src_x >= 0) & (src_x < w)
    sampled = np.zeros(src_y.shape, dtype=bool)
    sampled[in_bounds] = component[src_y[in_bounds], src_x[in_bounds]]
    out[top:bottom, left:right] = sampled
    return out


class ScaleMaskBackend(NativeBackend):
    """Grows (or, for a negative percent, shrinks) a mask's painted region --
    but as a true geometric scale about each closed region's own center, not
    a fixed-width boundary dilation: every one of a mask's connected
    components (8-connectivity) is found separately via `_label_components`
    and scaled about its *own* centroid via `_scale_component`, independently
    of every other component in the same mask. A mask with two disjoint
    blobs therefore grows each blob from its own middle, not from one shared
    center -- this was specifically requested over a uniform-dilation
    "grow_mask" design (2026-08-22).

    scale_percent=25 means factor 1.25 (25% larger, linearly, about each
    component's centroid); -25 means factor 0.75. Same single-channel "L",
    lit/white=masked-region convention and kind="mask" output as MaskBackend/
    MergeMaskBackend, so the result is interchangeable with any other mask
    asset everywhere downstream.
    """

    async def _run(self, execution_config: dict, inputs: dict[str, Any]) -> list[AssetRef]:
        mask = Image.open(BytesIO(inputs["mask"])).convert("L")
        percent = float(inputs.get("scale_percent", 0.0))
        factor = 1.0 + percent / 100.0

        arr = np.asarray(mask) > 127
        if arr.any() and abs(factor - 1.0) > 1e-9:
            out = np.zeros_like(arr)
            for component in _label_components(arr):
                out |= _scale_component(component, factor)
            arr = out

        result = Image.fromarray((arr.astype(np.uint8) * 255), mode="L")
        buf = BytesIO()
        result.save(buf, format="PNG")
        return [AssetRef(data=buf.getvalue(), mime_type="image/png", kind="mask")]
