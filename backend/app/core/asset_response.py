"""Serving an asset's payload out of a file that starts with a 64 KiB prefix
block (core/asset_prefix.py).

Starlette's FileResponse can't do this: it streams a whole file and does its
Range arithmetic against the raw st_size, with no notion of the body starting
part-way in. Everything it gave us for free -- streaming rather than reading
40 MB into RAM, ETag, 304, byte ranges for the board's audio/video -- therefore
has to be re-provided here, shifted by the offset.

The ETag is derived from the storage key rather than from mtime/size, and that
is a deliberate improvement over what FileResponse computed: a storage key is a
fresh uuid4 per payload and a payload is never rewritten in place, so the key
identifies the bytes exactly. Migrating a file (adding its prefix) changes both
mtime and size while leaving the payload identical -- an mtime-derived ETag
would have invalidated every cached original the first time we touched it, for
no reason at all.
"""

import hashlib
import os
import re
from pathlib import Path
from typing import Iterator

from starlette.requests import Request
from starlette.responses import Response, StreamingResponse

_CHUNK = 256 * 1024
_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")

# An asset URL keys on Asset.id and genuinely serves the same bytes for its
# whole life: storage keys are always a fresh uuid4 (storage.put_object) and a
# regenerate never rewrites an existing row -- native re-runs delete the old
# Asset rows and files outright (_clear_asset_node_outputs in worker/tasks.py)
# and insert new ones with new ids. "private" rather than "public" because the
# URL carries the shared token.
IMMUTABLE_CACHE = "private, max-age=31536000, immutable"

# Previews are the one thing here that *can* change under a stable id: the
# prefix block is padded precisely so a preview can be re-encoded in place
# later. So they revalidate instead of being pinned for a year.
REVALIDATE_CACHE = "private, max-age=0, must-revalidate"


def payload_etag(key: str) -> str:
    return '"' + hashlib.sha1(key.encode()).hexdigest()[:32] + '"'


def bytes_etag(data: bytes) -> str:
    return '"' + hashlib.sha1(data).hexdigest()[:32] + '"'


def _if_none_match(request: Request, etag: str) -> bool:
    header = request.headers.get("if-none-match", "")
    return bool(etag) and etag in {tag.strip() for tag in header.split(",")}


def _iter_range(path: Path, start: int, length: int) -> Iterator[bytes]:
    with path.open("rb") as f:
        f.seek(start)
        remaining = length
        while remaining > 0:
            chunk = f.read(min(_CHUNK, remaining))
            if not chunk:
                return
            remaining -= len(chunk)
            yield chunk


def _parse_range(header: str, size: int) -> tuple[int, int] | None:
    """(start, end-inclusive) within the payload, or None to send the whole
    thing. Only single ranges are honoured -- browsers ask for one when seeking
    media, and a multipart/byteranges reply for the rest isn't worth writing."""
    match = _RANGE_RE.match(header.strip())
    if not match or size == 0:
        return None
    first, last = match.group(1), match.group(2)
    if not first and not last:
        return None
    if not first:  # bytes=-N -> final N bytes
        start, end = max(0, size - int(last)), size - 1
    else:
        start = int(first)
        end = min(int(last), size - 1) if last else size - 1
    if start > end or start >= size:
        return None
    return start, end


def stream_payload(
    request: Request,
    path: Path,
    offset: int,
    media_type: str,
    etag: str,
    cache_control: str = IMMUTABLE_CACHE,
) -> Response:
    """The file from `offset` to EOF, as if that were the whole body."""
    size = max(0, os.path.getsize(path) - offset)
    headers = {"Cache-Control": cache_control, "ETag": etag, "Accept-Ranges": "bytes"}

    if _if_none_match(request, etag):
        return Response(status_code=304, headers=headers)

    requested = request.headers.get("range")
    span = _parse_range(requested, size) if requested else None
    if span is None:
        return StreamingResponse(
            _iter_range(path, offset, size),
            media_type=media_type,
            headers={**headers, "Content-Length": str(size)},
        )

    start, end = span
    length = end - start + 1
    return StreamingResponse(
        _iter_range(path, offset + start, length),
        status_code=206,
        media_type=media_type,
        headers={**headers, "Content-Length": str(length), "Content-Range": f"bytes {start}-{end}/{size}"},
    )
