"""Blender project files (.blend) -- kept, never interpreted by the app.

A .blend is stored as an ordinary project asset (`kind=other`, mime BLEND_MIME)
and shown only on the project's Storage page. The point is not to lose the
file; opening it is the Scenes machinery's job (core/blender_scene.py copies it
into a scene and lets Blender read it -- the stored original is never touched).

Plain files start with `BLENDER`. Blender 3.0+ may also write them compressed
(zstd, or gzip in older versions) -- those magics are shared with every other
zstd/gzip file, so they only count as a .blend when the *name* says so
(`is_blend_upload`); the bare-bytes sniff in upload_type.py takes the
unambiguous signature only.
"""

BLEND_MIME = "application/x-blender"

_ZSTD = b"\x28\xb5\x2f\xfd"
_GZIP = b"\x1f\x8b"


def is_blend(data: bytes) -> bool:
    return data[:7] == b"BLENDER"


def is_blend_upload(data: bytes, filename: str | None) -> bool:
    """A file the Storage page accepts: the plain signature, or a compressed
    container that is named .blend."""
    if is_blend(data):
        return True
    named = (filename or "").lower().endswith((".blend", ".blend1"))
    return named and (data[:4] == _ZSTD or data[:2] == _GZIP)
