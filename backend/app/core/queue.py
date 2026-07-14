import asyncio
import logging
from collections.abc import Awaitable, Callable

from app.config import get_settings

logger = logging.getLogger(__name__)


class InProcessQueue:
    """Minimal in-process replacement for an Arq/Redis queue: an asyncio.Queue
    of (coroutine function, args) fed by a fixed pool of worker tasks. Good
    enough for a single-box, single-user deployment -- no separate broker or
    worker process to run. Job state itself (pending/running/done/error) is
    still durably tracked in the `jobs` DB table by the callers, so a crash
    mid-generation just leaves a job requiring a manual re-generate rather
    than corrupting state."""

    def __init__(self, concurrency: int = 4) -> None:
        self._queue: asyncio.Queue[tuple[Callable[..., Awaitable[None]], tuple, float]] = asyncio.Queue()
        self._concurrency = concurrency
        self._workers: list[asyncio.Task] = []

    async def enqueue(self, func: Callable[..., Awaitable[None]], *args, delay: float = 0) -> None:
        await self._queue.put((func, args, delay))

    async def _worker_loop(self) -> None:
        while True:
            func, args, delay = await self._queue.get()
            try:
                if delay:
                    await asyncio.sleep(delay)
                await func(*args)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("job queue task %s failed", getattr(func, "__name__", func))
            finally:
                self._queue.task_done()

    async def start(self) -> None:
        self._workers = [asyncio.create_task(self._worker_loop()) for _ in range(self._concurrency)]

    async def stop(self) -> None:
        for worker in self._workers:
            worker.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers = []


job_queue = InProcessQueue(concurrency=get_settings().worker_concurrency)
