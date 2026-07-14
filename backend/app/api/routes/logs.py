from fastapi import APIRouter, Query

from app.core.logging_setup import log_file_path

router = APIRouter(prefix="/api/logs", tags=["logs"])


@router.get("")
async def tail_logs(lines: int = Query(300, ge=1, le=5000)):
    path = log_file_path()
    if not path.exists():
        return {"lines": []}
    with path.open("r", errors="replace") as f:
        content = f.readlines()
    return {"lines": [line.rstrip("\n") for line in content[-lines:]]}


@router.delete("", status_code=204)
async def clear_logs():
    """Truncates the log file in place (doesn't unlink it) -- the running
    RotatingFileHandler keeps its file descriptor open on the same inode, so
    this is safe to do while the app is up, unlike deleting/recreating the
    file out from under it."""
    path = log_file_path()
    if path.exists():
        path.write_text("")
