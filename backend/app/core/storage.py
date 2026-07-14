import uuid
from functools import lru_cache
from pathlib import Path

from app.config import get_settings


class Storage:
    """Local-filesystem asset storage. Files live under settings.media_dir and
    are served back out through GET /api/assets/{id}/file -- no S3/MinIO
    server required for a single-box deployment."""

    def __init__(self) -> None:
        settings = get_settings()
        self._root = Path(settings.media_dir).resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    def put_object(self, data: bytes, mime_type: str, prefix: str = "assets") -> str:
        del mime_type  # content-type is served from the Asset.mime_type DB column, not the filename
        key = f"{prefix}/{uuid.uuid4()}"
        path = self._safe_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return key

    def get_object(self, key: str) -> bytes:
        return self._safe_path(key).read_bytes()

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
