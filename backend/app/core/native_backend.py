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
import uuid
from io import BytesIO
from typing import Any

from PIL import Image

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


class MaskBackend(NativeBackend):
    """Bakes a painted bilevel mask into the source image's alpha channel,
    producing a single RGBA PNG -- matches ComfyUI's own "clipspace-painted-
    masked" convention (its stock LoadImage node splits such a file back into
    IMAGE/MASK outputs from RGB/alpha), so the result drops into any workflow
    expecting a masked LoadImage input with no ComfyUI-side changes needed.
    mask_png (params, not a slot -- see node_types.py's "mask" entry) is a
    frontend-painted bilevel PNG capped at a small resolution regardless of
    the source image's real size (see MaskPreview.tsx); nearest-neighbor
    resize back up to the source size keeps its edges hard rather than
    introducing gradient values a painted mask never had. Painted (white)
    pixels are the region to mask out, so they become transparent (alpha 0) --
    ComfyUI's own mask editor punches the same transparent hole for painted
    pixels, which its LoadImage node then reads back as mask=1."""

    async def _run(self, execution_config: dict, inputs: dict[str, Any]) -> list[AssetRef]:
        image = Image.open(BytesIO(inputs["image"])).convert("RGB")
        mask_png = inputs.get("mask_png")
        if mask_png:
            mask = Image.open(BytesIO(base64.b64decode(mask_png))).convert("L")
            if mask.size != image.size:
                mask = mask.resize(image.size, Image.NEAREST)
            alpha = mask.point(lambda p: 0 if p >= 128 else 255)
        else:
            alpha = Image.new("L", image.size, 255)

        result = image.convert("RGBA")
        result.putalpha(alpha)

        buf = BytesIO()
        result.save(buf, format="PNG")
        return [AssetRef(data=buf.getvalue(), mime_type="image/png", kind="image")]
