"""Capability-filtered, least-loaded backend selection (SPEC section 2.4)."""
import asyncio
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.api_backend import build_api_backend
from app.core.comfyui_backend import ComfyUIBackend
from app.core.job_backend import JobBackend
from app.core.node_types import EffectiveTemplate
from app.db.models import ApiKeyPermission, Backend, Capability, ExecutionType

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
) -> list[Capability]:
    """mode: 'auto' | 'comfyui_only' | 'api_only' | 'manual'"""
    stmt = (
        select(Capability)
        .join(Backend, Capability.backend_id == Backend.id)
        .where(Capability.node_type_slug == node_type_slug, Capability.enabled.is_(True), Backend.is_active.is_(True))
    )
    result = await db.execute(stmt)
    capabilities = list(result.scalars().all())

    if mode == "manual" and manual_backend_id:
        return [c for c in capabilities if str(c.backend_id) == str(manual_backend_id)]
    if mode == "comfyui_only":
        return [c for c in capabilities if c.execution_type == ExecutionType.comfyui_workflow]
    if mode == "api_only":
        return [c for c in capabilities if c.execution_type == ExecutionType.api_call]
    return capabilities


async def _has_api_permission(db: AsyncSession, provider: str, node_type_slug: str) -> ApiKeyPermission | None:
    stmt = select(ApiKeyPermission).where(
        ApiKeyPermission.provider == provider,
        ApiKeyPermission.node_type_slug == node_type_slug,
        ApiKeyPermission.enabled.is_(True),
    )
    result = await db.execute(stmt)
    return result.scalars().first()


def _instantiate(backend: Backend, capability: Capability, permission: ApiKeyPermission | None) -> JobBackend | None:
    if capability.execution_type == ExecutionType.comfyui_workflow:
        if not backend.base_url:
            return None
        return ComfyUIBackend(base_url=backend.base_url)
    if capability.execution_type == ExecutionType.api_call:
        if permission is None:
            return None
        provider = capability.config.get("provider")
        model_id = capability.config.get("model_id")
        try:
            return build_api_backend(provider, api_key=permission.api_key, model_id=model_id)
        except ValueError:
            return None
    return None


async def select_backend(
    db: AsyncSession,
    effective: EffectiveTemplate,
    mode: str = "auto",
    manual_backend_id: str | None = None,
    exclude_backend_ids: set[str] | None = None,
) -> DispatchChoice | None:
    if effective.is_native:
        # No Capability/Backend row involved at all -- native types are a code
        # registry (see node_types.py), always available, nothing to pick
        # between. exclude_backend_ids/mode/manual_backend_id don't apply.
        assert effective.native is not None
        return DispatchChoice(backend=None, capability=None, instance=effective.native.backend_cls())

    node_type_slug = effective.node_type_slug
    exclude_backend_ids = exclude_backend_ids or set()
    capabilities = await eligible_capabilities(db, node_type_slug, mode, manual_backend_id)

    candidates: list[DispatchChoice] = []
    for capability in capabilities:
        if str(capability.backend_id) in exclude_backend_ids:
            continue
        backend = await db.get(Backend, capability.backend_id)
        if backend is None or not backend.is_active:
            continue

        permission = None
        if capability.execution_type == ExecutionType.api_call:
            permission = await _has_api_permission(db, capability.config.get("provider", ""), node_type_slug)
            if permission is None:
                continue  # SPEC 2.4 step 3: user must have key + explicit permission

        instance = _instantiate(backend, capability, permission)
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
