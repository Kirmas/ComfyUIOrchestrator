import asyncio
import uuid
from functools import lru_cache
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from app.config import get_settings
from app.core import asset_prefix
from app.core.asset_preview import build_preview_async, wants_preview
from app.db.models import AssetKind


class Storage:
    """Local-filesystem asset storage. Files live under settings.media_dir and
    are served back out through GET /api/assets/{id}/file -- no S3/MinIO
    server required for a single-box deployment.

    Every file written here carries core/asset_prefix.py's 64 KiB header block
    in front of its real bytes, so this class is the one place that knows the
    difference between "the file on disk" and "the asset". Callers asking for
    bytes (get_object) always get the payload; callers streaming the file from
    disk go through payload_offset() and skip the block themselves."""

    def __init__(self) -> None:
        settings = get_settings()
        self._root = Path(settings.media_dir).resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        # One lock per storage key, so two requests for the same un-migrated
        # asset don't both read and rewrite the same 45 MB file. Bounded by the
        # number of assets, which is hundreds -- not worth evicting.
        self._migration_locks: dict[str, asyncio.Lock] = {}

    async def put_object(
        self, data: bytes, mime_type: str, prefix: str = "assets", kind: AssetKind | None = None
    ) -> str:
        """Writes a new asset, prefix and all, so nothing created from now on
        ever needs the lazy retrofit in ensure_prefix().

        Async because building the preview decodes the image (~1 s and ~130 MB
        for an 8K PNG) -- this is called straight from request handlers and from
        the worker, all of which share this process's one event loop."""
        key = f"{prefix}/{uuid.uuid4()}"
        path = self._safe_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        kind = kind or AssetKind.for_mime(mime_type)
        built = await build_preview_async(kind, data, asset_prefix.PREVIEW_CAPACITY)
        block = asset_prefix.build_prefix(kind, *built) if built else b""
        await asyncio.to_thread(path.write_bytes, block + data)
        return key

    def get_object(self, key: str) -> bytes:
        """The asset's own bytes -- what a backend, a native node or an MCP tool
        means by "the image". Never includes the prefix block."""
        path = self._safe_path(key)
        with path.open("rb") as f:
            f.seek(asset_prefix.payload_offset(path))
            return f.read()

    def path_of(self, key: str) -> Path:
        """On-disk path for a key, for handing to a streaming response instead
        of reading the whole object into memory (GET /api/assets/{id}/file
        serves 40+ MB originals -- read_bytes() there both spiked RAM and
        blocked the event loop, since nothing in this app offloads sync I/O to a
        thread). The returned path is the *file*, prefix included; use
        payload_offset() to find where the asset itself starts.

        Goes through the same _safe_path escape check as every other accessor."""
        return self._safe_path(key)

    def payload_offset(self, key: str) -> int:
        return asset_prefix.payload_offset(self._safe_path(key))

    def read_header(self, key: str) -> asset_prefix.AssetHeader | None:
        return asset_prefix.read_header(self._safe_path(key))

    def dimensions(self, key: str) -> tuple[int, int] | None:
        """The asset's own pixel size, for a caller that wants to label it
        without decoding it.

        Comes off the prefix block when there is one. Falling back to Pillow for
        a file that hasn't been migrated yet matters more than it looks: without
        it, every un-migrated cell would show no dimensions until something
        happened to migrate it, and a cell whose <img> now points at a 384x384
        preview can no longer answer the question from naturalWidth."""
        header = self.read_header(key)
        if header:
            size = (header.descriptor.get("width") or 0, header.descriptor.get("height") or 0)
            return size if all(size) else None
        try:
            with Image.open(self._safe_path(key)) as img:
                return img.size
        except (UnidentifiedImageError, OSError):
            return None

    def read_preview(self, key: str) -> bytes | None:
        return asset_prefix.read_preview(self._safe_path(key))

    async def ensure_prefix(self, key: str, kind: AssetKind) -> asset_prefix.AssetHeader | None:
        """Retrofit for everything written before this shipped: on first read,
        build the preview and rewrite the file with a prefix, so every later
        read is cheap. Once every file has been touched -- a couple of months of
        normal use -- this method and its callers can simply be deleted.

        Returns None for a file that will never carry a preview (a picture
        already smaller than one, a mesh, anything Pillow can't read). No prefix
        is written in that case: a 64 KiB block of padding around nothing is
        worse than the "no magic means serve the original" fallback we need for
        un-migrated files anyway."""
        path = self._safe_path(key)
        header = asset_prefix.read_header(path)
        if header is not None:
            return header
        lock = self._migration_locks.setdefault(key, asyncio.Lock())
        async with lock:
            # Another request may have finished the work while we waited.
            header = asset_prefix.read_header(path)
            if header is not None:
                return header
            if not await asyncio.to_thread(wants_preview, kind, path):
                return None
            data = await asyncio.to_thread(path.read_bytes)
            built = await build_preview_async(kind, data, asset_prefix.PREVIEW_CAPACITY)
            if built is None:
                return None
            block = asset_prefix.build_prefix(kind, *built)
            await asyncio.to_thread(self._rewrite, path, block, data)
            return asset_prefix.parse_header(block)

    @staticmethod
    def _rewrite(path: Path, block: bytes, payload: bytes) -> None:
        """Write-then-rename, because the media in this library is not
        reproducible and a torn write over the original would lose it. The
        rename is atomic within the directory, and a response already streaming
        the old file keeps its open descriptor on the old inode -- so replacing
        the file underneath an in-flight download is safe on POSIX."""
        tmp = path.with_name(f".{path.name}.prefix-tmp")
        try:
            tmp.write_bytes(block + payload)
            tmp.replace(path)
        finally:
            tmp.unlink(missing_ok=True)

    def delete_object(self, key: str) -> None:
        self._safe_path(key).unlink(missing_ok=True)

    def _safe_path(self, key: str) -> Path:
        path = (self._root / key).resolve()
        if path != self._root and self._root not in path.parents:
            raise ValueError(f"storage key '{key}' escapes media_dir")
        return path


@lru_cache
def get_storage() -> Storage:
    return Storage()


def build_asset_url(asset_id: uuid.UUID | str) -> str:
    """API-relative URL (with the shared bearer token as a query param, since
    <img>/<model-viewer> tags can't send an Authorization header) for
    GET /api/assets/{id}/file."""
    settings = get_settings()
    return f"/api/assets/{asset_id}/file?token={settings.api_token}"


def build_preview_url(asset_id: uuid.UUID | str) -> str:
    """The grid-sized thumbnail out of the asset's prefix block. Falls back to
    the original server-side, so this is always safe to point an <img> at."""
    settings = get_settings()
    return f"/api/assets/{asset_id}/preview?token={settings.api_token}"
