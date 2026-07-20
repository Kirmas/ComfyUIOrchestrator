"""Capability-filtered, least-loaded backend selection (SPEC section 2.4)."""
import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.api_backend import build_api_backend
from app.core.comfyui_backend import ComfyUIBackend
from app.core.job_backend import JobBackend
from app.core.node_types import EffectiveTemplate
from app.db.models import ApiUsageLog, Backend, Capability, ExecutionType

# Same rolling-window choice as backends.py's used_today computation -- must
# match exactly, or a backend could read as "over quota" in one place and
# "fine" in the other.
_USAGE_WINDOW = timedelta(hours=24)

# Several worker tasks can call select_backend() concurrently (worker_concurrency
# jobs dispatched in the same instant, e.g. "generate 4 variants"). capacity()
# reads each backend's *live* queue length over HTTP, which doesn't yet include
# a job this same batch already chose to send but hasn't POSTed /prompt for --
# so every concurrent caller sees the same "empty" backend and they all pile
# onto it. This in-memory counter tracks picks that haven't been submitted yet
# so concurrent selections see each other and spread out instead of racing.
_reservation_lock = asyncio.Lock()
_reserved: dict[str, int] = {}


async def release_backend(backend_id: str) -> None:
    async with _reservation_lock:
        remaining = _reserved.get(backend_id, 0) - 1
        if remaining <= 0:
            _reserved.pop(backend_id, None)
        else:
            _reserved[backend_id] = remaining


@dataclass
class DispatchChoice:
    # None for a native node type -- there's no real Backend/Capability row to
    # point at (see core/node_types.py), it always runs right here in-process.
    backend: Backend | None
    capability: Capability | None
    instance: JobBackend


async def eligible_capabilities(
    db: AsyncSession,
    node_type_slug: str,
    mode: str = "auto",
    manual_backend_id: str | None = None,
    use_api: bool = False,
) -> list[Capability]:
    """mode: 'auto' | 'comfyui_only' | 'api_only' | 'manual'

    use_api is a hard gate independent of mode -- Node.use_api (see
    db/models.py) must be explicitly True for an api_call capability to ever
    appear here, even under mode="api_only" or a manual pick of an
    api_provider backend. Without it, "auto" mode picking whichever
    capability happens to be eligible would silently start spending money on
    a node the user never opted into that for."""
    stmt = (
        select(Capability)
        .join(Backend, Capability.backend_id == Backend.id)
        .where(Capability.node_type_slug == node_type_slug, Capability.enabled.is_(True), Backend.is_active.is_(True))
    )
    result = await db.execute(stmt)
    capabilities = list(result.scalars().all())
    if not use_api:
        capabilities = [c for c in capabilities if c.execution_type != ExecutionType.api_call]

    if mode == "manual" and manual_backend_id:
        return [c for c in capabilities if str(c.backend_id) == str(manual_backend_id)]
    if mode == "comfyui_only":
        return [c for c in capabilities if c.execution_type == ExecutionType.comfyui_workflow]
    if mode == "api_only":
        return [c for c in capabilities if c.execution_type == ExecutionType.api_call]
    return capabilities


async def _backend_within_quota(db: AsyncSession, backend: Backend) -> bool:
    """Backend.daily_limit (see db/models.py docstring) is one key shared by
    every node type pointed at this backend, so quota is checked per-backend,
    not per node type. NULL means unlimited."""
    if backend.daily_limit is None:
        return True
    cutoff = datetime.now(UTC) - _USAGE_WINDOW
    count_stmt = select(func.count()).select_from(ApiUsageLog).where(
        ApiUsageLog.backend_id == backend.id, ApiUsageLog.created_at >= cutoff
    )
    used = (await db.execute(count_stmt)).scalar_one()
    return used < backend.daily_limit


def _instantiate(backend: Backend, capability: Capability) -> JobBackend | None:
    if capability.execution_type == ExecutionType.comfyui_workflow:
        if not backend.base_url:
            return None
        return ComfyUIBackend(base_url=backend.base_url)
    if capability.execution_type == ExecutionType.api_call:
        if not backend.provider or not backend.api_key:
            return None
        model_id = capability.config.get("model_id")
        try:
            return build_api_backend(backend.provider, api_key=backend.api_key, model_id=model_id)
        except ValueError:
            return None
    return None


async def select_backend(
    db: AsyncSession,
    effective: EffectiveTemplate,
    mode: str = "auto",
    manual_backend_id: str | None = None,
    exclude_backend_ids: set[str] | None = None,
    use_api: bool = False,
) -> DispatchChoice | None:
    if effective.is_native:
        # No Capability/Backend row involved at all -- native types are a code
        # registry (see node_types.py), always available, nothing to pick
        # between. exclude_backend_ids/mode/manual_backend_id don't apply.
        assert effective.native is not None
        return DispatchChoice(backend=None, capability=None, instance=effective.native.backend_cls())

    node_type_slug = effective.node_type_slug
    exclude_backend_ids = exclude_backend_ids or set()
    capabilities = await eligible_capabilities(db, node_type_slug, mode, manual_backend_id, use_api)

    candidates: list[DispatchChoice] = []
    for capability in capabilities:
        if str(capability.backend_id) in exclude_backend_ids:
            continue
        backend = await db.get(Backend, capability.backend_id)
        if backend is None or not backend.is_active:
            continue

        if capability.execution_type == ExecutionType.api_call and not await _backend_within_quota(db, backend):
            continue  # over daily_limit -- treated exactly like the backend not being eligible at all

        instance = _instantiate(backend, capability)
        if instance is None:
            continue
        candidates.append(DispatchChoice(backend=backend, capability=capability, instance=instance))

    if not candidates:
        return None

    # capacity() is a live HTTP round trip -- run these concurrently-safe outside
    # the lock. Only the read-_reserved/pick-best/write-_reserved step needs to
    # be atomic, otherwise two callers can both read the same stale reservation
    # count before either has incremented it (see module docstring above).
    infos = [(choice, await choice.instance.capacity()) for choice in candidates]

    best: DispatchChoice | None = None
    async with _reservation_lock:
        best_effective_length = None
        for choice, info in infos:
            if not info.is_alive:
                continue
            effective_length = info.queue_length + _reserved.get(str(choice.backend.id), 0)
            if info.max_queue_length is not None and effective_length >= info.max_queue_length:
                continue  # backend already has as much queued as we're willing to commit -- let it drain first
            if best is None or effective_length < best_effective_length:
                best = choice
                best_effective_length = effective_length

        if best is not None:
            backend_id = str(best.backend.id)
            _reserved[backend_id] = _reserved.get(backend_id, 0) + 1

    return best


async def reconnect_instance(db: AsyncSession, backend_id: str, node_type_slug: str) -> JobBackend | None:
    """Rebuilds the same JobBackend instance a previously-dispatched job was
    already running on, given just the (backend, node_type_slug) select_backend
    originally dispatched it to -- used by recover_orphaned_jobs
    (worker/tasks.py) to reconnect an orphaned job to its real backend-side
    state after a restart (still running there, or already finished while we
    were down) instead of blindly failing it just because *our* process lost
    track of it. Mirrors select_backend's own instantiation (_instantiate)
    exactly so the two never drift apart. Returns None if the backend or its
    capability for this node type no longer exists -- nothing to reconnect to."""
    backend = await db.get(Backend, backend_id)
    if backend is None:
        return None
    result = await db.execute(
        select(Capability).where(Capability.backend_id == backend.id, Capability.node_type_slug == node_type_slug)
    )
    capability = result.scalars().first()
    if capability is None:
        return None
    return _instantiate(backend, capability)
