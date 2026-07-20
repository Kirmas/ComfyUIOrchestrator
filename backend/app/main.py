import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api.routes import assets, backends, capabilities, health, jobs, logs, node_templates, nodes, projects, tracks, ws
from app.config import get_settings
from app.core.auth import auth_middleware
from app.core.heartbeat import heartbeat_loop
from app.core.logging_setup import configure_logging
from app.core.queue import job_queue
from app.worker.tasks import recover_orphaned_jobs


@asynccontextmanager
async def lifespan(app: FastAPI):
    await recover_orphaned_jobs()
    await job_queue.start()
    heartbeat_task = asyncio.create_task(heartbeat_loop())
    yield
    heartbeat_task.cancel()
    await job_queue.stop()


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging()
    app = FastAPI(title="ComfyUI Orchestrator", lifespan=lifespan)

    # Registration order matters: Starlette wraps the *last*-registered middleware
    # outermost, so auth must be registered before CORS -- otherwise a preflight
    # OPTIONS request (which never carries our Authorization header) gets a 401
    # from auth_middleware before CORSMiddleware ever runs, and the browser reports
    # that as a generic CORS failure instead of the real 401.
    app.middleware("http")(auth_middleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    for router in (
        health.router,
        backends.router,
        capabilities.router,
        node_templates.router,
        projects.router,
        tracks.router,
        nodes.router,
        assets.router,
        jobs.router,
        logs.router,
    ):
        app.include_router(router)

    app.include_router(ws.router)

    if settings.frontend_dist_dir:
        dist = Path(settings.frontend_dist_dir)
        if dist.exists():
            app.mount("/", StaticFiles(directory=str(dist), html=True), name="frontend")

    return app


app = create_app()
