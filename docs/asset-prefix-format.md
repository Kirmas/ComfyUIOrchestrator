# Asset prefix block

Every file under `MEDIA_DIR` carries a fixed 64 KiB block in front of its real
bytes: a small self-describing header plus a ready-made thumbnail.

**Why.** An 8K PNG in this library is 40–49 MB and decodes to ~132 MB of bitmap
in the browser — for a grid cell that renders it at 118×118 CSS px. Caching
(shipped 2026-07-30) fixed the re-download but not the decode, which is paid on
every mount. The grid needed a thumbnail.

**Why inside the file rather than a sidecar.** A sidecar would have needed its
own lifecycle: `storage_gc` would report every one as an orphan, and
`delete_object` would have to remember a second path. One file, one lifecycle.

## Layout

```
 0   4  magic "COAP"
 4   1  version
 5   1  asset kind code        <- discriminator for everything below
 6   2  preview_len  uint16    0 = no preview in this file
 8  24  descriptor, per kind:
          image / mask : w uint32, h uint32, 16 B spare
          mesh         : verts uint32, faces uint32, 16 B spare
32 ..   preview (WebP), then zero padding to 65536
```

- **The kind is stored, not looked up.** A file that needs the database open to
  be interpreted is not self-describing — losing that was the main thing that
  made the sidecar unattractive. `storage_gc`'s orphan report reads the kind
  straight off disk.
- **Padded, not tightly packed.** `preview_len` makes the real size a runtime
  value, so a preview can be re-encoded later with one 64 KiB `pwrite` instead
  of rewriting a 45 MB payload.
- **64 KiB, a power of two**, so the payload behind it stays block-aligned.
  Measured worst case across the whole library is **53 420 B** at WebP q90
  against 65 504 B of capacity — a 32 KiB block would *not* have fit.

## Preview

384×384 centre square, WebP q90, encoded to budget (q90 → 80 → 70 → 60).

- 384 because the grid renders a face at 118 CSS px; ×1.4 max zoom ×2 for a
  hidpi display = 330 device px.
- A **centre square**, because `.output-item img` is `aspect-ratio: 1;
  object-fit: cover` — the grid never shows anything else, so nothing else is
  stored.
- Measured over 261 real assets: median 15.9 KB, max 53.4 KB.

## Producers (`core/asset_preview.py`)

One class per `AssetKind`, registered in `PREVIEW_PRODUCERS`. A preview is
*always a raster* whatever the payload is — a mesh would be a rendered still, a
video a grabbed frame — which is why kind never reaches `asset_prefix.py` as
anything but a discriminator.

| kind | producer | state |
|---|---|---|
| `image`, `mask` | centre crop → 384×384 WebP | live |
| `mesh` | rendered still | slot reserved, returns `None` (server-side 3D render is a new heavy dependency; `Model3DThumb.tsx` already renders in the browser) |
| `other` | — | `None` |

`None` means **no prefix is written at all** — a 64 KiB block of padding around
nothing is worse than the "no magic means serve the original" fallback that
un-migrated files need anyway. That is also the answer for a picture already
smaller than a preview (`min(w, h) <= 384`), which is the skip rule; it lives in
the producer, not as a global constant.

## Migration

Files written before this shipped get their prefix built on first `/preview`
read, and the request waits (~1.2 s for a 49 MB file). Decodes run in a thread
pool, three at a time, so the event loop stays free and other API calls do not
queue behind them. A per-key lock stops two requests rewriting the same file.

Rewrites go through a temporary in the same directory and are renamed into
place; a response already streaming the old file keeps its descriptor on the old
inode, so replacing it mid-download is safe.

`put_object` writes the prefix at creation time, so nothing new ever needs the
retrofit. **Once every file has been touched, `ensure_prefix` and its one caller
can simply be deleted.**

Rollback: `backend/.venv/bin/python scripts/strip_asset_prefix.py --apply`.

## Serving (`core/asset_response.py`)

Starlette's `FileResponse` cannot serve from an offset — it streams a whole file
and does Range arithmetic against the raw `st_size`. Streaming, ETag, 304 and
byte ranges are therefore re-provided, shifted by the offset.

The `/file` ETag derives from the **storage key**, not mtime/size. A key is a
fresh uuid4 per payload and a payload is never rewritten, so the key identifies
the bytes exactly — and migrating a file (which changes mtime and size while
leaving the payload identical) does not invalidate a year of cached originals.

`/preview` is the one thing that can change under a stable id, so it revalidates
(`max-age=0, must-revalidate`) rather than being pinned `immutable`.

## Who uses the preview

Grid cell faces, the candidates grid, the reference picker. **Zoom, compare,
download and board stickers keep the original** — judging an un-downscaled
result is the point of the tool.

Dimensions in the cell's size tag come from the descriptor, not from the loaded
`<img>`'s `naturalWidth`, which stops being the original's size the moment the
`<img>` points at a 384×384 preview.
