"""Small helper for persisting a single KEY=VALUE line into backend/.env.

Settings.model_config points pydantic-settings at this same file
(app/config.py's BASE_DIR / ".env"), and it's re-read from disk every time a
Settings() is constructed -- so rewriting the file here plus clearing
get_settings()'s lru_cache is enough to make a config change stick across
restarts *and* apply to the already-running process, with no separate
"write it somewhere else and reconcile on startup" step.
"""

import os
import tempfile
from pathlib import Path

from app.config import BASE_DIR

ENV_PATH = BASE_DIR / ".env"


def set_env_var(key: str, value: str) -> None:
    """Replace the line `key=...` in .env (any pre-existing casing/spacing of
    that one key), or append it if the key isn't present yet. Preserves every
    other line untouched, including comments and blank lines. Writes via a
    temp file + os.replace so a crash mid-write can never leave a half-written
    .env behind (the config every other setting -- API_TOKEN, DATABASE_URL --
    also lives in)."""
    lines: list[str] = []
    if ENV_PATH.exists():
        lines = ENV_PATH.read_text().splitlines()

    prefix = f"{key}="
    new_line = f"{key}={value}"
    replaced = False
    for i, line in enumerate(lines):
        if line.strip().startswith(prefix):
            lines[i] = new_line
            replaced = True
            break
    if not replaced:
        lines.append(new_line)

    fd, tmp_path = tempfile.mkstemp(dir=str(ENV_PATH.parent), prefix=".env.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write("\n".join(lines) + "\n")
        os.replace(tmp_path, ENV_PATH)
    except BaseException:
        Path(tmp_path).unlink(missing_ok=True)
        raise
