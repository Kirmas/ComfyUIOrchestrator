import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app.config import get_settings

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def configure_logging() -> None:
    """Adds a rotating file handler alongside the default stream handler.

    Uvicorn/systemd already send stdout to the journal -- this just gives the
    in-app Logs page (GET /api/logs) a file it can tail on its own, regardless
    of how the process was launched or who can read the system journal.
    """
    settings = get_settings()
    log_dir = Path(settings.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "orchestrator.log"

    root = logging.getLogger()
    if any(isinstance(h, RotatingFileHandler) for h in root.handlers):
        return  # already configured (e.g. reload)

    root.setLevel(logging.INFO)
    file_handler = RotatingFileHandler(log_file, maxBytes=5 * 1024 * 1024, backupCount=3)
    file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
    root.addHandler(file_handler)


def log_file_path() -> Path:
    return Path(get_settings().log_dir) / "orchestrator.log"
