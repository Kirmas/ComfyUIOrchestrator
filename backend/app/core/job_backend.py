import enum
from dataclasses import dataclass
from typing import Any, Protocol


class JobStatus(str, enum.Enum):
    pending = "pending"
    running = "running"
    done = "done"
    error = "error"
    cancelled = "cancelled"


@dataclass
class CapacityInfo:
    is_alive: bool
    queue_length: int
    max_queue_length: int | None = None  # None = effectively unlimited (e.g. paid API backends)


@dataclass
class AssetRef:
    data: bytes
    mime_type: str
    kind: str = "image"
    meta: dict[str, Any] | None = None


class JobBackend(Protocol):
    """Uniform interface every execution backend (ComfyUI instance or paid API) implements.

    The dispatcher and UI only ever talk to this interface — see SPEC.md section 2.2.
    """

    async def submit(self, execution_config: dict, inputs: dict) -> str: ...

    async def status(self, job_id: str) -> JobStatus: ...

    async def result(self, job_id: str) -> list[AssetRef]: ...

    async def capacity(self) -> CapacityInfo: ...

    async def cancel(self, job_id: str) -> None: ...
