from functools import lru_cache
from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# app/config.py -> backend/. Anchoring here (not the process's cwd) means
# `.env`, a relative DATABASE_URL, and MEDIA_DIR all resolve the same way no
# matter where uvicorn/arq gets launched from -- otherwise a different launch
# method (VSCode debugger, a different terminal, a shortcut) silently opens a
# *different*, empty SQLite file / media dir each time.
BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(BASE_DIR / ".env"), env_file_encoding="utf-8", extra="ignore")

    # Auth
    api_token: str = "change-me"

    # Postgres is the primary target; a SQLite URL (e.g.
    # "sqlite+aiosqlite:///./orchestrator.db") works too as a fallback when
    # Postgres isn't installed -- see app/db/types.py for the portable
    # UUID/JSON column types that make the same models/migration work on both.
    database_url: str = "postgresql+asyncpg://orchestrator:orchestrator@localhost:5432/orchestrator"

    # Where generated assets (images, 3D meshes, uploads) are stored on disk.
    # Served back out through GET /api/assets/{id}/file -- no object-storage
    # server needed for a single-box deployment.
    media_dir: str = "./media"

    # Frontend static bundle (served by the same process)
    frontend_dist_dir: str | None = None

    # Where the app's own rotating log file lives (read back by GET /api/logs
    # for the in-app Logs page). Separate from whatever journald/systemd does
    # with stdout -- this is a file the app controls itself so the Logs page
    # has something to read regardless of how the process is launched.
    log_dir: str = "./logs"

    # Derived renditions that are expensive to redo but can always be redone --
    # the page-size /fit pictures (asset_preview.py), an SVG rasterised once
    # rather than on every restart. Deliberately NOT under media_dir: the
    # backup's file-count check, storage GC and storage migration all treat
    # every file there as an asset. Safe to delete wholesale.
    cache_dir: str = "./cache"

    # Limits
    max_variants_per_node: int = 8
    # Bounds the *network calls* around a ComfyUI generation (submit, fetch
    # result, API-backend polling) -- never the ComfyUI generation itself. A
    # generation has no fixed duration; _wait_with_stall_detection in
    # worker/tasks.py asks ComfyUI whether the prompt is still alive and only
    # gives up on it when it's really gone or the backend is unreachable.
    job_timeout_seconds: int = 600
    max_retries: int = 2
    heartbeat_interval_seconds: int = 15
    dispatch_poll_interval_seconds: int = 3
    worker_concurrency: int = 4  # how many generation jobs this process runs in parallel

    cors_origins: list[str] = ["http://localhost:5173"]

    # Host header values the MCP server (/mcp) accepts -- DNS-rebinding
    # protection (mcp SDK's TransportSecuritySettings) rejects any request
    # whose Host doesn't match one of these, which is correct but means the
    # LAN address every non-localhost client actually connects through
    # (CLAUDE.md's documented 192.168.0.3) has to be listed explicitly, not
    # just 127.0.0.1/localhost.
    mcp_allowed_hosts: list[str] = ["127.0.0.1:*", "localhost:*", "[::1]:*", "192.168.0.3:*"]

    # Agent chats (app/api/routes/agent_chats.py). The agent runner is a
    # separate process (agent_runner/runner.py, its own user unit) so a chat
    # survives this service restarting; this service only proxies to it and
    # authenticates to it with its own api_token. agent_mcp_url is this
    # instance's own /mcp as the runner (same box) reaches it -- the MCP
    # server a project chat's agent works through.
    agent_runner_url: str = "http://127.0.0.1:8765"
    agent_mcp_url: str = "http://127.0.0.1:8000/mcp/"

    @model_validator(mode="after")
    def _resolve_relative_paths(self) -> "Settings":
        if self.database_url.startswith("sqlite") and ":///" in self.database_url:
            prefix, _, raw_path = self.database_url.partition(":///")
            if raw_path and not raw_path.startswith(":memory:") and not Path(raw_path).is_absolute():
                self.database_url = f"{prefix}:///{(BASE_DIR / raw_path).resolve().as_posix()}"

        media_path = Path(self.media_dir)
        if not media_path.is_absolute():
            self.media_dir = str((BASE_DIR / media_path).resolve())

        cache_path = Path(self.cache_dir)
        if not cache_path.is_absolute():
            self.cache_dir = str((BASE_DIR / cache_path).resolve())

        log_path = Path(self.log_dir)
        if not log_path.is_absolute():
            self.log_dir = str((BASE_DIR / log_path).resolve())

        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
