"""What an uploaded file is, decided from its bytes first.

The browser's declared MIME is whatever the client sent (usually derived from
the file name) and nothing on the server checks it, so it's only the fallback.
A file whose signature we recognise is stored with the type its bytes say --
a PNG sent as application/octet-stream comes out image/png, and the stored
type is what gets served back, so it has to be true.

Only signatures that can't be mistaken for something else: text formats
(OBJ/PLY/STL) have none and stay with the declared type. FBX has no viewer
yet, so it isn't classified here either.

Containers that can hold either picture or sound (ISO-BMFF, Matroska, Ogg)
are split by what the bytes can tell cheaply: the ISO-BMFF major brand says
M4A vs everything else, and a Matroska/WebM or Ogg file is taken as video /
audio respectively -- the common case for each. A declared type of the same
family wins over that guess (an audio-only .webm declared audio/webm stays
audio), so the guess only decides when the client said nothing useful.
"""

from app.db.models import AssetKind

_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"glTF", "model/gltf-binary"),
    (b"\x1aE\xdf\xa3", "video/webm"),
    (b"OggS", "audio/ogg"),
    (b"fLaC", "audio/flac"),
    (b"ID3", "audio/mpeg"),
)

# ISO-BMFF major brands (bytes 8..12, after the "ftyp" box type at 4..8) that
# mean a sound-only file. Everything else in that family is a video.
_AUDIO_BRANDS = (b"M4A ", b"M4B ", b"M4P ", b"F4A ")
_QUICKTIME_BRANDS = (b"qt  ",)

# The extension ComfyUI's input folder needs to see for a given type: its
# LoadVideo/LoadAudio list files by extension, so an mp4 uploaded under a
# .png name is simply invisible to them.
_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "model/gltf-binary": ".glb",
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/webm": ".webm",
    "audio/mp4": ".m4a",
    "audio/ogg": ".ogg",
    "audio/flac": ".flac",
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
}


def sniff_mime(data: bytes) -> str | None:
    """MIME type the leading bytes identify, or None when nothing recognised."""
    for signature, mime in _SIGNATURES:
        if data.startswith(signature):
            return mime
    if data[:4] == b"RIFF":
        if data[8:12] == b"WEBP":
            return "image/webp"
        if data[8:12] == b"WAVE":
            return "audio/wav"
        if data[8:12] == b"AVI ":
            return "video/x-msvideo"
    if data[4:8] == b"ftyp":
        brand = data[8:12]
        if brand in _AUDIO_BRANDS:
            return "audio/mp4"
        if brand in _QUICKTIME_BRANDS:
            return "video/quicktime"
        return "video/mp4"
    # A bare MPEG audio frame (an mp3 without an ID3 tag): 11 sync bits, then
    # a layer field that isn't the reserved 00. AAC/ADTS shares the sync bits
    # but has layer 00, so it's excluded here.
    if len(data) >= 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0 and (data[1] & 0x06) != 0:
        return "audio/mpeg"
    return None


def resolve_upload(data: bytes, declared_mime: str | None) -> tuple[str, AssetKind]:
    """(mime_type, kind) to store for an uploaded file. Bytes win when they're
    recognised; otherwise the declared MIME, else octet-stream."""
    sniffed = sniff_mime(data)
    mime_type = sniffed or declared_mime or "application/octet-stream"
    # A container the bytes can only half-identify (see module docstring):
    # a declared audio/* for a "video" container, or video/* for an "audio"
    # one, is the client knowing better than the guess.
    if sniffed in ("video/webm", "audio/ogg", "video/mp4") and declared_mime:
        declared_family = declared_mime.split("/", 1)[0]
        if declared_family in ("audio", "video") and declared_family != sniffed.split("/", 1)[0]:
            mime_type = declared_mime
    if mime_type == "model/gltf-binary":
        return mime_type, AssetKind.mesh
    return mime_type, AssetKind.for_mime(mime_type)


def extension_for(data: bytes, fallback: str = ".png") -> str:
    """File extension matching what the bytes are, for handing a file to
    something that decides by name (ComfyUI's /upload/image)."""
    mime = sniff_mime(data)
    return _EXTENSIONS.get(mime or "", fallback)
