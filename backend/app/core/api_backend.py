"""ApiBackend: JobBackend implementation for paid image-generation APIs
(SPEC section 2.2/6). Treated by the dispatcher as a backend with effectively
unlimited capacity. Concrete providers subclass `ApiBackend` and implement
`_submit_request` / `_parse_result`; jobs are synchronous from the caller's
point of view (the provider call itself does the generation), so `submit`
runs the call immediately and stores the result keyed by a generated job id.
"""
import base64
import uuid
from typing import Any

import httpx

from app.core.job_backend import AssetRef, CapacityInfo, JobStatus

# In-memory result cache for the lifetime of the worker process. API calls are
# synchronous (submit() already has the full result), so this just lets
# status()/result() follow the same polling-based JobBackend Protocol as
# ComfyUIBackend without a second network round trip.
_RESULTS: dict[str, list[AssetRef]] = {}
_ERRORS: dict[str, str] = {}


class ApiBackend:
    provider: str = "generic"

    def __init__(self, api_key: str, model_id: str | None = None) -> None:
        self.api_key = api_key
        self.model_id = model_id

    async def submit(self, execution_config: dict, inputs: dict[str, Any]) -> str:
        job_id = str(uuid.uuid4())
        try:
            assets = await self._submit_request(execution_config, inputs)
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

    async def _submit_request(self, execution_config: dict, inputs: dict[str, Any]) -> list[AssetRef]:
        raise NotImplementedError


class GeminiImageBackend(ApiBackend):
    """Google Gemini ('nano banana') image generation, per SPEC's own example provider."""

    provider = "nano_banana"
    _ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

    async def _submit_request(self, execution_config: dict, inputs: dict[str, Any]) -> list[AssetRef]:
        param_mapping = execution_config.get("param_mapping", {})
        model_id = execution_config.get("model_id", self.model_id or "gemini-2.5-flash-image")

        prompt_field = param_mapping.get("prompt", "prompt")
        prompt = inputs.get(prompt_field, "")

        parts: list[dict] = [{"text": prompt}]
        for field_name, value in inputs.items():
            if field_name == prompt_field:
                continue
            if isinstance(value, (bytes, bytearray)):
                parts.append(
                    {"inline_data": {"mime_type": "image/png", "data": base64.b64encode(value).decode("ascii")}}
                )

        url = self._ENDPOINT.format(model=model_id)
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                url,
                params={"key": self.api_key},
                json={"contents": [{"parts": parts}]},
            )
            resp.raise_for_status()
            data = resp.json()

        assets: list[AssetRef] = []
        for candidate in data.get("candidates", []):
            for part in candidate.get("content", {}).get("parts", []):
                inline = part.get("inlineData") or part.get("inline_data")
                if inline:
                    assets.append(
                        AssetRef(
                            data=base64.b64decode(inline["data"]),
                            mime_type=inline.get("mimeType", inline.get("mime_type", "image/png")),
                            kind="image",
                        )
                    )
        return assets


PROVIDERS: dict[str, type[ApiBackend]] = {
    GeminiImageBackend.provider: GeminiImageBackend,
}


def build_api_backend(provider: str, api_key: str, model_id: str | None = None) -> ApiBackend:
    cls = PROVIDERS.get(provider)
    if cls is None:
        raise ValueError(f"unknown API provider '{provider}'")
    return cls(api_key=api_key, model_id=model_id)
