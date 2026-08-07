"""Finds files under MEDIA_DIR with no matching Asset row -- the leftovers
from any DB row that got cascade-deleted (Track/Project -> Node/Asset via
ON DELETE CASCADE) without anyone calling storage.delete_object() for the
file it pointed at. See Settings' "orphan files" section.

nodes.py::delete_node and assets.py::delete_asset both already do the right
thing (delete the file, then the row); the gap is everywhere a *cascade*
does the deleting instead -- projects.py::delete_project and
tracks.py::delete_track just db.delete() the top row and let Postgres take
care of the rest, which cleans up rows but never touches disk.

Always a manual, user-reviewed action from Settings -- never scheduled --
because a false positive here is data loss."""

import mimetypes
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from PIL import Image
from PIL import UnidentifiedImageError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.asset_types import REFERENCING_ASSET_NODE_TYPES
from app.core.storage import get_storage
from app.db.models import Asset, AssetKind, Dashboard, Node


def guess_mime_type(path: Path) -> str:
    """storage_key files are always extension-less uuid4 names
    (storage.py::put_object) -- mimetypes.guess_type(path) can never work off
    the name alone, unlike a normal upload where Asset.mime_type comes from
    the browser's Content-Type at upload time. Peeking at the actual header
    bytes via Pillow (already a hard dependency, see native_backend.py) is
    cheap: Image.open only reads enough of the file to identify the format,
    it doesn't decode pixel data until .load()/.convert() is called."""
    try:
        with Image.open(path) as img:
            mime = img.get_format_mimetype()
            if mime:
                return mime
    except (UnidentifiedImageError, OSError):
        pass
    return mimetypes.guess_type(str(path))[0] or "application/octet-stream"

# A generation job (worker/tasks.py) always writes its file via
# storage.put_object *before* committing the Asset row -- so a file that's
# genuinely mid-flight can look identical to an orphan for the few seconds
# between those two steps. Excluding anything touched this recently means a
# real in-flight job clears the window in normal use; a real orphan just
# shows up on the next scan instead.
_MIN_ORPHAN_AGE_SECONDS = 300


@dataclass
class OrphanFile:
    path: str
    size_bytes: int
    modified_at: float
    mime_type_guess: str

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "size_bytes": self.size_bytes,
            "modified_at": self.modified_at,
            "mime_type_guess": self.mime_type_guess,
        }


def _walk_media_dir() -> tuple[dict[str, tuple[int, float]], list[str]]:
    """relpath -> (size, mtime) for every file currently under media_dir, plus
    any paths this walk could NOT read (surfaced to the caller instead of
    being silently dropped -- see scan_orphans' scan_errors, and the
    2026-08-07 incident this fixes: os.walk()'s default followlinks=False
    means a symlinked subdirectory (uploads/ turned out to be one) is listed
    but never descended into, so its files silently vanished from every
    count -- not a permission problem, direct path access worked the whole
    time (Storage.path_of/open() follow symlinks like any normal file open)."""
    root = Path(get_settings().media_dir)
    found: dict[str, tuple[int, float]] = {}
    errors: list[str] = []
    if not root.exists():
        return found, errors
    for dirpath, _dirnames, filenames in os.walk(root, onerror=lambda exc: errors.append(str(exc)), followlinks=True):
        for name in filenames:
            full = Path(dirpath) / name
            try:
                stat = full.stat()
            except OSError as exc:
                errors.append(f"{full}: {exc}")
                continue
            found[str(full.relative_to(root))] = (stat.st_size, stat.st_mtime)
    return found, errors


async def scan_orphans(db: AsyncSession) -> dict:
    on_disk, scan_errors = _walk_media_dir()
    known_keys = set((await db.execute(select(Asset.storage_key))).scalars().all())

    root = Path(get_settings().media_dir)
    cutoff = time.time() - _MIN_ORPHAN_AGE_SECONDS
    orphans = [
        OrphanFile(
            path=relpath,
            size_bytes=size,
            modified_at=mtime,
            mime_type_guess=guess_mime_type(root / relpath),
        )
        for relpath, (size, mtime) in on_disk.items()
        if relpath not in known_keys and mtime < cutoff
    ]
    orphans.sort(key=lambda o: o.modified_at)

    missing_file_count = sum(1 for key in known_keys if key not in on_disk)

    return {
        "orphan_files": [o.to_dict() for o in orphans],
        "missing_file_count": missing_file_count,
        # Non-empty means the scan is *incomplete*, not clean -- e.g. a path
        # it couldn't stat. Surfaced so "0 orphans" is never confused with
        # "0 orphans, but also couldn't check everything" (see 2026-08-07
        # incident where a symlinked subdirectory's files were silently
        # invisible to every count until this was added).
        "scan_errors": scan_errors,
    }


def _validate_orphan(path: str, on_disk: dict[str, tuple[int, float]], known_keys: set[str]) -> None:
    """Re-checked against a fresh scan right before acting on it, not just
    trusted from whatever the client last saw -- the set on disk/in the DB
    can have changed since the button was rendered."""
    if path not in on_disk:
        raise FileNotFoundError(f"no such file under media_dir: {path}")
    if path in known_keys:
        raise ValueError("this file is already tracked by an asset row, refusing to touch it")
    _size, mtime = on_disk[path]
    if mtime >= time.time() - _MIN_ORPHAN_AGE_SECONDS:
        raise ValueError("file was modified too recently to treat as an orphan -- rescan in a bit")


async def _revalidate(db: AsyncSession, path: str) -> None:
    on_disk, _errors = _walk_media_dir()
    known_keys = set((await db.execute(select(Asset.storage_key))).scalars().all())
    _validate_orphan(path, on_disk, known_keys)


async def delete_orphan(db: AsyncSession, path: str) -> None:
    await _revalidate(db, path)
    # Storage.path_of runs the file through the same _safe_path escape check
    # every other accessor uses (core/storage.py) -- path can't walk outside
    # media_dir even though it arrived as a raw string over the API.
    get_storage().path_of(path).unlink()


async def adopt_orphan(db: AsyncSession, path: str, project_id: uuid.UUID) -> Asset:
    """Turns an orphan file into a project-library asset in place -- same row
    shape as boards.py::upload_project_asset, but pointed at the file that's
    already there instead of writing a fresh copy via put_object."""
    await _revalidate(db, path)
    mime_type = guess_mime_type(get_storage().path_of(path))
    kind = AssetKind.for_mime(mime_type)
    asset = Asset(
        project_id=project_id,
        storage_key=path,
        mime_type=mime_type,
        kind=kind,
        selected=False,
        tags=[],
        meta={"recovered_from": "orphan-scan"},
    )
    db.add(asset)
    await db.commit()
    await db.refresh(asset)
    return asset


def preview_path(path: str) -> Path:
    return get_storage().path_of(path)


# ---------- unowned assets (node_id AND project_id both NULL) ----------
#
# A second, distinct GC case from the file-vs-DB scan above: a real Asset
# row that exists, has a real file, but is invisible to every UI surface --
# Board only lists Asset.project_id rows, Grid only lists Asset.node_id ones
# (2026-08-07 incident: /api/assets/upload, called by nothing in today's
# frontend, still left 7 such rows behind from whatever called it in the
# past). Not caught by scan_orphans() above since these files ARE tracked --
# the row just points nowhere useful.


async def _refasset_referenced_ids(db: AsyncSession, candidate_ids: set[str]) -> set[str]:
    """Which of candidate_ids (asset id strings) are pointed at by some
    referencing asset node's explicit input (REFERENCING_ASSET_NODE_TYPES --
    the asset kinds whose `references_by_input` is set, see
    core/asset_types.py), or by some Dashboard.result_asset_id, anywhere in
    the whole app -- such a reference resolves by asset id alone
    (resolveSlotAsset/explicit_ref_asset), so this has to be a global check,
    not scoped to one project."""
    referenced: set[str] = set()

    refasset_nodes = (
        (await db.execute(select(Node.inputs).where(Node.node_type.in_(REFERENCING_ASSET_NODE_TYPES)))).scalars().all()
    )
    for inputs in refasset_nodes:
        for inp in inputs or []:
            oid = inp.get("output_id") or inp.get("asset_id")
            if oid and str(oid) in candidate_ids:
                referenced.add(str(oid))

    result_ids = (await db.execute(select(Dashboard.result_asset_id))).scalars().all()
    for rid in result_ids:
        if rid and str(rid) in candidate_ids:
            referenced.add(str(rid))

    return referenced


async def scan_unowned_assets(db: AsyncSession) -> dict:
    rows = (await db.execute(select(Asset).where(Asset.node_id.is_(None), Asset.project_id.is_(None)))).scalars().all()
    if not rows:
        return {"unowned_assets": []}

    referenced = await _refasset_referenced_ids(db, {str(r.id) for r in rows})

    def _size(storage_key: str) -> int | None:
        p = preview_path(storage_key)
        return p.stat().st_size if p.exists() else None

    return {
        "unowned_assets": [
            {
                "id": str(r.id),
                "storage_key": r.storage_key,
                "mime_type": r.mime_type,
                "size_bytes": _size(r.storage_key),
                "created_at": r.created_at.isoformat(),
                "referenced": str(r.id) in referenced,
            }
            for r in rows
        ]
    }


async def _get_unowned(db: AsyncSession, asset_id: uuid.UUID) -> Asset:
    asset = await db.get(Asset, asset_id)
    if not asset:
        raise FileNotFoundError("no such asset")
    if asset.node_id is not None or asset.project_id is not None:
        raise ValueError("this asset is already owned by a node or a project, refusing to touch it")
    return asset


async def delete_unowned_asset(db: AsyncSession, asset_id: uuid.UUID) -> None:
    asset = await _get_unowned(db, asset_id)
    referenced = await _refasset_referenced_ids(db, {str(asset.id)})
    if referenced:
        raise ValueError("still referenced by a grid cell (asset.refasset) or a dashboard's result -- adopt it into a project instead")
    get_storage().delete_object(asset.storage_key)
    await db.delete(asset)
    await db.commit()


async def adopt_unowned_asset(db: AsyncSession, asset_id: uuid.UUID, project_id: uuid.UUID) -> Asset:
    """No file move, no new row -- the asset already exists exactly right,
    it's just missing a project_id to make it show up in that project's
    library (GET /api/projects/{id}/assets)."""
    asset = await _get_unowned(db, asset_id)
    asset.project_id = project_id
    await db.commit()
    await db.refresh(asset)
    return asset
