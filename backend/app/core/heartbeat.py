import asyncio
import logging
from datetime import UTC, datetime

from sqlalchemy import select

from app.config import get_settings
from app.core.comfyui_backend import ComfyUIBackend
from app.db.base import async_session_maker
from app.db.models import Backend, BackendKind

logger = logging.getLogger(__name__)

# backend id -> last observed alive state, purely in-memory so it starts empty
# on every process start. That's what makes "or on restart" happen for free:
# the first heartbeat after boot always has no prior entry, so it always
# logs the current state once, then goes quiet until it actually flips.
_last_alive: dict[str, bool] = {}


async def _heartbeat_once() -> None:
    async with async_session_maker() as db:
        result = await db.execute(select(Backend).where(Backend.kind == BackendKind.comfyui, Backend.is_active.is_(True)))
        backends = list(result.scalars().all())

    for backend in backends:
        if not backend.base_url:
            continue
        instance = ComfyUIBackend(base_url=backend.base_url)
        alive = True
        try:
            stats = await instance.heartbeat()
            capacity = await instance.capacity()
            async with async_session_maker() as db:
                row = await db.get(Backend, backend.id)
                row.last_heartbeat_at = datetime.now(UTC)
                row.last_stats = {**stats, "alive": capacity.is_alive, "queue_length": capacity.queue_length}
                await db.commit()
        except Exception:
            alive = False
            async with async_session_maker() as db:
                row = await db.get(Backend, backend.id)
                row.last_stats = {**(row.last_stats or {}), "alive": False}
                await db.commit()

        # A ComfyUI box going up/down is routine in a home-lab setup (it gets
        # turned off between sessions), not something worth a line every
        # heartbeat_interval_seconds -- only log when it actually flips.
        backend_key = str(backend.id)
        if _last_alive.get(backend_key) != alive:
            _last_alive[backend_key] = alive
            logger.info("comfyui %s %s (%s)", backend.name, "up" if alive else "down", backend.base_url)


async def heartbeat_loop() -> None:
    settings = get_settings()
    while True:
        try:
            await _heartbeat_once()
        except Exception:
            logger.exception("heartbeat loop iteration failed")
        await asyncio.sleep(settings.heartbeat_interval_seconds)
