"""What is in a 3D asset, for an agent that can't open the viewer.

`describe(data)` gives the facts (format, counts, size, what the file carries);
`contact_sheet(data)` draws four views of it as one PNG, with no Blender --
points sampled over the surface, a z-buffer, flat lambert shading. It is a
look at the shape, not a render: enough to tell a head from a sword and to see
what is wrong with it, and it costs ~a second instead of a Blender start-up.
"""

import io

import numpy as np
from PIL import Image, ImageDraw

from app.core import glb_stats
from app.core.mesh_simplify import _split_glb
from app.core.stl_convert import mesh_triangles

_SAMPLES = 400_000  # surface points per view set
_MAX_TRIS = 1_500_000  # beyond this, triangles are subsampled before sampling
_PANEL = 360


def describe(data: bytes) -> dict:
    tris, fmt = mesh_triangles(data)
    lo, hi = tris.reshape(-1, 3).min(axis=0), tris.reshape(-1, 3).max(axis=0)
    info: dict = {
        "format": fmt,
        "bytes": len(data),
        "triangles": int(len(tris)),
        "bounding_box_min": [round(float(x), 5) for x in lo],
        "bounding_box_size": [round(float(x), 5) for x in hi - lo],
    }
    if fmt == "glb":
        doc, _ = _split_glb(data)
        counts = glb_stats.counts_from_bytes(data)
        if counts:
            info["vertices"] = counts[0]
        info.update(
            materials=len(doc.get("materials", [])),
            textures=len(doc.get("textures", [])),
            animations=len(doc.get("animations", [])),
            skins=len(doc.get("skins", [])),
            meshes=len(doc.get("meshes", [])),
        )
        info["axes"] = "glTF: Y up, +Z toward the viewer, unit = metre"
    else:
        info["axes"] = "STL: no axes or unit are stored; Z up and millimetres by convention (3D printing)"
        info["note"] = "geometry only: no colour, textures, materials or animation"
    return info


def _sample_surface(tris: np.ndarray, rng: np.random.Generator) -> tuple[np.ndarray, np.ndarray]:
    """Area-weighted random points on the triangles and the unit normal under each."""
    if len(tris) > _MAX_TRIS:
        tris = tris[rng.choice(len(tris), _MAX_TRIS, replace=False)]
    a, b, c = tris[:, 0].astype(np.float64), tris[:, 1].astype(np.float64), tris[:, 2].astype(np.float64)
    cross = np.cross(b - a, c - a)
    area = np.linalg.norm(cross, axis=1)
    if not area.sum():
        raise ValueError("the mesh has no surface area")
    cdf = np.cumsum(area)
    pick = np.minimum(np.searchsorted(cdf, rng.random(_SAMPLES) * cdf[-1]), len(tris) - 1)
    u, v = rng.random(_SAMPLES), rng.random(_SAMPLES)
    flip = u + v > 1
    u[flip], v[flip] = 1 - u[flip], 1 - v[flip]
    pts = a[pick] + (b[pick] - a[pick]) * u[:, None] + (c[pick] - a[pick]) * v[:, None]
    normals = cross[pick] / np.maximum(area[pick], 1e-30)[:, None]
    return pts, normals


def _rotation(azimuth: float, elevation: float) -> np.ndarray:
    ca, sa, ce, se = np.cos(azimuth), np.sin(azimuth), np.cos(elevation), np.sin(elevation)
    yaw = np.array([[ca, 0, sa], [0, 1, 0], [-sa, 0, ca]])
    pitch = np.array([[1, 0, 0], [0, ce, -se], [0, se, ce]])
    return pitch @ yaw


def _panel(pts: np.ndarray, normals: np.ndarray, rot: np.ndarray, scale: float, label: str) -> Image.Image:
    p = pts @ rot.T
    n = normals @ rot.T
    # Camera looks down -Z; a normal facing the camera has n_z > 0. Shade by how
    # directly the surface faces it (two-sided, so a flipped face still shows).
    shade = 0.25 + 0.75 * np.abs(n[:, 2])
    x = ((p[:, 0] * scale) + _PANEL / 2).astype(int)
    y = ((-p[:, 1] * scale) + _PANEL / 2).astype(int)
    ok = (x >= 0) & (x < _PANEL) & (y >= 0) & (y < _PANEL)
    x, y, z, shade = x[ok], y[ok], p[ok, 2], shade[ok]
    order = np.argsort(z)  # far first, so the nearest point is written last
    img = np.full((_PANEL, _PANEL), 28, dtype=np.uint8)
    img[y[order], x[order]] = (60 + 190 * shade[order]).astype(np.uint8)
    out = Image.fromarray(img, "L").convert("RGB")
    ImageDraw.Draw(out).text((8, 6), label, fill=(150, 200, 255))
    return out


def contact_sheet(data: bytes) -> bytes:
    """PNG with front / side / top / three-quarter views of a GLB or STL."""
    tris, fmt = mesh_triangles(data)
    pts, normals = _sample_surface(tris, np.random.default_rng(1))
    if fmt == "stl":  # Z-up -> Y-up, so "front"/"top" mean the same for both
        up = np.array([[1, 0, 0], [0, 0, 1], [0, -1, 0]], dtype=np.float64)
        pts, normals = pts @ up.T, normals @ up.T
    centre = (pts.min(axis=0) + pts.max(axis=0)) / 2
    pts = pts - centre
    radius = float(np.linalg.norm(pts, axis=1).max()) or 1.0
    scale = (_PANEL / 2 - 14) / radius
    views = [
        ("front", _rotation(0, 0)),
        ("side", _rotation(-np.pi / 2, 0)),
        ("top", _rotation(0, np.pi / 2)),
        ("3/4", _rotation(np.radians(-35), np.radians(25))),
    ]
    sheet = Image.new("RGB", (_PANEL * 2, _PANEL * 2))
    for i, (label, rot) in enumerate(views):
        sheet.paste(_panel(pts, normals, rot, scale, label), ((i % 2) * _PANEL, (i // 2) * _PANEL))
    buf = io.BytesIO()
    sheet.save(buf, "PNG")
    return buf.getvalue()
