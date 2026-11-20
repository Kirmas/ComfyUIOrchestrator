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


@dataclass
class AssetInput:
    """A slot's asset by reference. Backends that hand an input on by id instead
    of using its bytes (the agent task: the agent opens the asset itself) set
    `agentic = True`, and resolve_node_inputs then gives them this in place of the
    bytes -- no point loading a 140 MB mesh into the API process to pass its id."""

    asset_id: str
    kind: str
    mime_type: str


class JobBackend(Protocol):
    """Uniform interface every execution backend (ComfyUI instance or paid API) implements.

    The dispatcher and UI only ever talk to this interface.
    """

    async def submit(self, execution_config: dict, inputs: dict) -> str: ...

    async def status(self, job_id: str) -> JobStatus: ...

    async def error_detail(self, job_id: str) -> str | None:
        """Human-readable reason for a JobStatus.error, if the backend has one
        beyond the bare status -- e.g. the exception message a native/API
        backend caught, or ComfyUI's own execution_error payload. None if
        there's nothing more specific than "it errored"."""
        ...

    async def result(self, job_id: str) -> list[AssetRef]: ...

    async def capacity(self) -> CapacityInfo: ...

    async def cancel(self, job_id: str) -> None: ...
