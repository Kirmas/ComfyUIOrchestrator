"""What an uploaded file is, decided from its bytes first.

The browser's declared MIME is whatever the client sent (usually derived from
the file name) and nothing on the server checks it, so it's only the fallback.
A file whose signature we recognise is stored with the type its bytes say --
a PNG sent as application/octet-stream comes out image/png, and the stored
type is what gets served back, so it has to be true.

Only signatures that can't be mistaken for something else: text formats
(OBJ/PLY/STL) have none and stay with the declared type. FBX has no viewer
yet, so it isn't classified here either.
"""

from app.db.models import AssetKind

_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"glTF", "model/gltf-binary"),
)


def sniff_mime(data: bytes) -> str | None:
    """MIME type the leading bytes identify, or None when nothing recognised."""
    for signature, mime in _SIGNATURES:
        if data.startswith(signature):
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def resolve_upload(data: bytes, declared_mime: str | None) -> tuple[str, AssetKind]:
    """(mime_type, kind) to store for an uploaded file. Bytes win when they're
    recognised; otherwise the declared MIME, else octet-stream."""
    mime_type = sniff_mime(data) or declared_mime or "application/octet-stream"
    if mime_type == "model/gltf-binary":
        return mime_type, AssetKind.mesh
    return mime_type, AssetKind.for_mime(mime_type)
