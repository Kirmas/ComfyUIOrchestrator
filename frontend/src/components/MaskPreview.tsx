import { useState } from "react";
import { BrushCursorDot, MaskZoomViewport, PaintMaskToolbar, useBrushCursor, useMaskZoom, usePaintMask } from "./paintMask";

/** Freehand binary mask painter: brush over the source image, committed as a
 * bilevel PNG in node.params (see native.mask / MaskBackend). The painting
 * itself lives in usePaintMask (paintMask.tsx) -- what's specific here is that
 * the mask *is* the picture: the painted canvas is the visible one, drawn in
 * translucent red straight over the source image. Zoom/pan (useMaskZoom, also
 * shared with TransplantPreview) lets a stroke land precisely on a small
 * detail without the brush itself needing to shrink below a usable size. */
export function MaskPreview({
  imageUrl,
  maskPng,
  onCommit,
}: {
  imageUrl: string;
  maskPng: string | null;
  onCommit: (maskPng: string | null) => void;
}) {
  const [natural, setNatural] = useState<{ w: number; h: number } | null>(null);
  const paint = usePaintMask({ maskPng, natural, resetKey: imageUrl, onCommit });
  const zoom = useMaskZoom();
  // Same canvas serves as both the paint storage and the visible/interactive
  // one here (unlike TransplantPreview, which paints onto a hidden one),
  // so it's passed for both of useBrushCursor's ref params.
  const cursor = useBrushCursor(paint.brushRadius, paint.canvasRef, zoom.containerRef);

  return (
    <div onClick={(e) => e.stopPropagation()}>
      <PaintMaskToolbar paint={paint} zoom={zoom} />
      <MaskZoomViewport natural={natural} zoom={zoom} overlay={<BrushCursorDot cursor={cursor} />}>
        <img
          src={imageUrl}
          alt="mask source"
          style={{ width: "100%", height: "100%", display: "block", borderRadius: 4 }}
          onLoad={(e) => setNatural({ w: e.currentTarget.naturalWidth, h: e.currentTarget.naturalHeight })}
          draggable={false}
        />
        <canvas
          ref={paint.canvasRef}
          style={{ position: "absolute", inset: 0, width: "100%", height: "100%", cursor: "crosshair", touchAction: "none" }}
          onPointerDown={paint.handlers.onPointerDown}
          onPointerMove={(e) => {
            paint.handlers.onPointerMove(e);
            cursor.handlers.onPointerMove(e);
          }}
          onPointerUp={paint.handlers.onPointerUp}
          onPointerCancel={paint.handlers.onPointerCancel}
          onPointerLeave={cursor.handlers.onPointerLeave}
        />
      </MaskZoomViewport>
    </div>
  );
}
