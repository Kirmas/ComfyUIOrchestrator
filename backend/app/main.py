import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes import annotations, assets, backends, boards, capabilities, dashboards, health, jobs, logs, node_templates, node_types, nodes, projects, tracks, ws
from app.config import get_settings
from app.core.auth import auth_middleware
from app.core.heartbeat import heartbeat_loop
from app.core.logging_setup import configure_logging
from app.core.queue import job_queue
from app.mcp import tools  # noqa: F401 -- importing registers the MCP tools
from app.mcp.client import set_app
from app.mcp.server import mcp_server
from app.worker.tasks import recover_orphaned_jobs


@asynccontextmanager
async def lifespan(app: FastAPI):
    # recover_orphaned_jobs must finish before anything can be asked for job
    # status: an agent polling await_node would otherwise read a stale
    # "running" job that no backend is actually working on and never learn to
    # re-roll it (roadmap.md, "handle persistence").
    await recover_orphaned_jobs()
    await job_queue.start()
    heartbeat_task = asyncio.create_task(heartbeat_loop())
    # The MCP session manager owns the streamable-HTTP transport's own
    # background state; it has to be entered for /mcp to answer at all.
    async with mcp_server.session_manager.run():
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
        annotations.router,
        boards.router,
        node_types.router,
        dashboards.router,
    ):
        app.include_router(router)

    app.include_router(ws.router)

    # streamable_http_app() must be called before lifespan touches
    # mcp_server.session_manager -- the manager is created lazily by this call
    # and raises if accessed first.
    app.mount("/mcp", mcp_server.streamable_http_app())

    # A Starlette Mount only matches paths *below* its prefix, so the exact
    # "/mcp" never reaches the mount above -- it falls through to the frontend's
    # catch-all StaticFiles mount, which answers POST with 405. Clients
    # overwhelmingly configure the URL without a trailing slash, so redirect it
    # (307 keeps the method and body intact).
    @app.api_route("/mcp", methods=["GET", "POST", "DELETE"], include_in_schema=False)
    async def _mcp_no_trailing_slash():
        return RedirectResponse("/mcp/", status_code=307)

    set_app(app)

    if settings.frontend_dist_dir:
        dist = Path(settings.frontend_dist_dir)
        if dist.exists():
            app.mount("/", StaticFiles(directory=str(dist), html=True), name="frontend")

    return app


app = create_app()
