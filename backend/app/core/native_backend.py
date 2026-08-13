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

from PIL import Image, ImageFilter

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
