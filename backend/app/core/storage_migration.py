"""Move MEDIA_DIR to a new location from the running app, with no SSH/restart
needed (see Settings' Storage section, driven through api/routes/system.py).

The whole thing is one all-or-nothing sequence, in this exact order:

  1. copy every file old -> new, hashing the *source* bytes as they're read
  2. re-read every file back from the *new* location and hash it again,
     comparing against step 1 -- an independent verification that what
     actually landed on the new disk matches, not just that the copy calls
     reported success
  3. only once every file passes that diff: flip MEDIA_DIR in .env and
     hot-swap the live Settings/Storage singletons (the commit point)
  4. only after that commit succeeds: delete the old directory

Any failure in 1 or 2 aborts before anything in 3/4 runs: the old directory
is left completely untouched, .env is never written, and the partially
written new directory is removed (safe to do -- start_migration's own
validation already required it to be empty or nonexistent beforehand, so
this only ever undoes what this run itself created).

Single in-memory state (this app is one process, `core/ws_manager.py`'s
per-project fanout is the same "no separate broker needed" reasoning) --
only one migration can run at a time.
"""

import asyncio
import hashlib
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from app.config import get_settings
from app.core.env_file import set_env_var
from app.core.storage import get_storage

Status = Literal["idle", "copying", "verifying", "done", "error"]

_CHUNK_SIZE = 4 * 1024 * 1024  # 4 MiB
_FREE_SPACE_MARGIN = 100 * 1024 * 1024  # 100 MiB


@dataclass
class _MigrationState:
    status: Status = "idle"
    new_path: str | None = None
    files_done: int = 0
    files_total: int = 0
    bytes_done: int = 0
    bytes_total: int = 0
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "new_path": self.new_path,
            "files_done": self.files_done,
            "files_total": self.files_total,
            "bytes_done": self.bytes_done,
            "bytes_total": self.bytes_total,
            "error": self.error,
        }


_state = _MigrationState()


def get_status() -> dict:
    return _state.to_dict()


def disk_report(path: str) -> dict:
    usage = shutil.disk_usage(path)
    return {"total": usage.total, "used": usage.used, "free": usage.free}


def media_dir_stats() -> dict:
    """Walked fresh each call -- fine at current scale, not optimized further
    (see storage_settings memory / CLAUDE.md's "measure before optimizing").

    followlinks=True: os.walk()'s default (False) lists a symlinked
    subdirectory but never descends into it, so its files silently vanish
    from every count here -- not a permission issue, direct file access
    still works fine (2026-08-07 incident: media_dir/uploads turned out to be
    a symlink, and every stat/count in this app quietly ignored its contents
    until this was found via the orphan scanner in storage_gc.py)."""
    root = Path(get_settings().media_dir)
    size = 0
    count = 0
    if root.exists():
        for dirpath, _dirnames, filenames in os.walk(root, followlinks=True):
            for name in filenames:
                try:
                    size += (Path(dirpath) / name).stat().st_size
                except OSError:
                    continue
                count += 1
    return {"size_bytes": size, "file_count": count}


def list_dirs(path: str | None) -> dict:
    """Directories only, never files -- this is a folder picker, not a file
    browser. No path restriction: every other surface of this app (MCP tools,
    node/generation control) already sits behind one shared bearer token for
    a single-user home LAN (core/auth.py's own "single-user, no auth flow"),
    so a server-side directory listing isn't a new trust boundary."""
    root = Path(path).resolve() if path else Path(get_settings().media_dir).resolve().parent
    if not root.is_dir():
        raise NotADirectoryError(f"not a directory: {root}")
    entries = []
    for child in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        try:
            if not child.is_dir():
                continue
        except OSError:
            continue
        entries.append({"name": child.name, "path": str(child), "writable": os.access(child, os.W_OK)})
    parent = str(root.parent) if root.parent != root else None
    return {"path": str(root), "parent": parent, "entries": entries}


def make_dir(path: str) -> dict:
    p = Path(path)
    if not p.is_absolute():
        raise ValueError("path must be an absolute path")
    p.mkdir(parents=True, exist_ok=True)
    return {"path": str(p)}


def _is_relative_to(a: Path, b: Path) -> bool:
    try:
        a.relative_to(b)
        return True
    except ValueError:
        return False


def _validate_target(new_path: str) -> Path:
    raw = Path(new_path)
    if not raw.is_absolute():
        raise ValueError("new_path must be an absolute path")

    old_root = Path(get_settings().media_dir).resolve()
    new_root = raw.resolve()

    if new_root == old_root:
        raise ValueError("new location is the same as the current one")
    if _is_relative_to(new_root, old_root) or _is_relative_to(old_root, new_root):
        raise ValueError("new location can't be nested inside the current one, or vice versa")

    created_here = False
    if new_root.exists():
        if not new_root.is_dir():
            raise ValueError("target path exists and is not a directory")
        if any(new_root.iterdir()):
            raise ValueError("target directory is not empty")
    else:
        new_root.mkdir(parents=True, exist_ok=True)
        created_here = True

    try:
        stats = media_dir_stats()
        free = shutil.disk_usage(new_root).free
        needed = stats["size_bytes"] + _FREE_SPACE_MARGIN
        if free < needed:
            raise ValueError(f"not enough free space at target: {free} bytes free, need at least {needed} bytes")
    except Exception:
        if created_here:
            shutil.rmtree(new_root, ignore_errors=True)
        raise

    return new_root


def start_migration(new_path: str) -> None:
    if _state.status in ("copying", "verifying"):
        raise RuntimeError("a migration is already running")
    new_root = _validate_target(new_path)

    # Set state synchronously, before yielding to the event loop -- closes the
    # race window a second quick POST could otherwise slip through in between
    # this check and the task actually starting.
    _state.status = "copying"
    _state.new_path = str(new_root)
    _state.files_done = 0
    _state.files_total = 0
    _state.bytes_done = 0
    _state.bytes_total = 0
    _state.error = None
    asyncio.create_task(_run(new_root))


def _hash_file(path: Path) -> tuple[int, str]:
    hasher = hashlib.sha256()
    size = 0
    with path.open("rb") as f:
        while True:
            chunk = f.read(_CHUNK_SIZE)
            if not chunk:
                break
            hasher.update(chunk)
            size += len(chunk)
    return size, hasher.hexdigest()


def _copy_with_hash(src: Path, dst: Path) -> tuple[int, str]:
    """Single read pass over src: streams straight into dst while hashing the
    same bytes, so the source manifest costs no extra I/O beyond the copy
    itself. The verify phase re-reads dst independently afterward."""
    hasher = hashlib.sha256()
    size = 0
    with src.open("rb") as fsrc, dst.open("wb") as fdst:
        while True:
            chunk = fsrc.read(_CHUNK_SIZE)
            if not chunk:
                break
            fdst.write(chunk)
            hasher.update(chunk)
            size += len(chunk)
    return size, hasher.hexdigest()


def _copy_phase(old_root: Path, new_root: Path) -> dict[str, tuple[int, str]]:
    files = [p for p in old_root.rglob("*") if p.is_file()]
    _state.files_total = len(files)
    _state.bytes_total = sum(p.stat().st_size for p in files)

    manifest: dict[str, tuple[int, str]] = {}
    for src in files:
        rel = src.relative_to(old_root)
        dst = new_root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        size, digest = _copy_with_hash(src, dst)
        shutil.copystat(src, dst)
        manifest[str(rel)] = (size, digest)
        _state.files_done += 1
        _state.bytes_done += size
    return manifest


def _verify_phase(new_root: Path, manifest: dict[str, tuple[int, str]]) -> None:
    new_relpaths = {str(p.relative_to(new_root)) for p in new_root.rglob("*") if p.is_file()}
    old_relpaths = set(manifest.keys())
    if new_relpaths != old_relpaths:
        missing = old_relpaths - new_relpaths
        extra = new_relpaths - old_relpaths
        raise RuntimeError(f"file set mismatch after copy (missing={len(missing)}, extra={len(extra)})")

    # Independent read-back pass -- fresh done/total counters so the frontend
    # can show a second "verifying" progress bar distinct from the copy one.
    _state.files_done = 0
    _state.bytes_done = 0
    for rel, (size, digest) in manifest.items():
        actual_size, actual_digest = _hash_file(new_root / rel)
        if actual_size != size or actual_digest != digest:
            raise RuntimeError(f"content mismatch after copy: {rel}")
        _state.files_done += 1
        _state.bytes_done += actual_size


async def _run(new_root: Path) -> None:
    old_root = Path(get_settings().media_dir).resolve()
    try:
        manifest = await asyncio.to_thread(_copy_phase, old_root, new_root)
        _state.status = "verifying"
        await asyncio.to_thread(_verify_phase, new_root, manifest)
    except Exception as exc:
        _state.status = "error"
        _state.error = str(exc)
        shutil.rmtree(new_root, ignore_errors=True)
        return

    # Commit point: everything above is verified good but nothing is
    # permanent yet. From here on the old directory is what's at risk, so a
    # failure past this line must NOT roll back new_root (it's known-good) --
    # it just stays in "error" with both copies present until sorted out by hand.
    try:
        set_env_var("MEDIA_DIR", str(new_root))
        # Rewriting .env alone isn't enough: prod runs this service under
        # systemd's EnvironmentFile= (deploy/comfy-orchestrator-api.service),
        # which loads MEDIA_DIR as a real OS environment variable at process
        # start -- and pydantic-settings always prefers an actual env var over
        # re-parsing env_file, so get_settings.cache_clear() alone would keep
        # constructing Settings from the *old* value for the rest of this
        # process's life (2026-08-07 incident: migration completed and
        # deleted the old directory, but the still-running process kept
        # trying to serve assets from it until manually restarted). Setting
        # the process's own os.environ here is what actually changes what the
        # next get_settings() call reads.
        os.environ["MEDIA_DIR"] = str(new_root)
        get_settings.cache_clear()
        get_storage.cache_clear()
    except Exception as exc:
        _state.status = "error"
        _state.error = f"copy verified ok but failed to commit config: {exc}"
        return

    shutil.rmtree(old_root, ignore_errors=True)
    _state.status = "done"
