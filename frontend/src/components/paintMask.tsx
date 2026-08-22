import { useEffect, useRef, useState } from "react";
import { useT } from "../i18n";
import { MIN_ZOOM, usePinchPan } from "../usePinchPan";

/** The brush-painting engine behind every "paint a region on this image"
 * editor: the canvas, the stroke/erase/undo/clear behaviour, and the bilevel
 * PNG encoding both ends of the app agree on.
 *
 * It exists because there are now two such editors with the same painting and
 * the same storage but different *presentation* -- MaskPreview.tsx draws the
 * strokes themselves in translucent red (the mask is the thing being looked
 * at), while TransplantPreview.tsx paints holes in a top layer to reveal the
 * one underneath (the composite is the thing being looked at). Sharing the
 * hook rather than adding a mode flag to one component keeps that difference
 * where it belongs, in each component's own JSX.
 */

// Capped independent of the source image's real resolution -- a painted
// brush mask doesn't need source-resolution precision, and this keeps the
// base64 stored in node.params small and predictable regardless of whether
// the underlying photo is 512px or 4K (the backends -- MaskBackend,
// TransplantBackend -- nearest-neighbor-upscale back to the source size at
// execution).
export const MASK_MAX_DIM = 768;
const UNDO_LIMIT = 20;

function emptyMaskImageData(ctx: CanvasRenderingContext2D, width: number, height: number): ImageData {
  return ctx.createImageData(width, height);
}

/** Decodes a previously-saved bilevel mask PNG (black = unmasked, white =
 * masked, see MaskBackend) into the painting canvas's own representation:
 * transparent where unmasked, translucent red where masked. Alpha (not RGB)
 * is what carries the "is this painted" signal, so painting can use
 * "destination-out" to erase regardless of stroke color. */
function decodeMaskPngToImageData(maskPng: string, width: number, height: number): Promise<ImageData> {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => {
      const tmp = document.createElement("canvas");
      tmp.width = width;
      tmp.height = height;
      const tctx = tmp.getContext("2d");
      if (!tctx) return reject(new Error("no 2d context"));
      tctx.drawImage(img, 0, 0, width, height);
      const src = tctx.getImageData(0, 0, width, height);
      const out = tctx.createImageData(width, height);
      for (let i = 0; i < src.data.length; i += 4) {
        const masked = src.data[i] > 127;
        out.data[i] = 255;
        out.data[i + 1] = 60;
        out.data[i + 2] = 60;
        out.data[i + 3] = masked ? 160 : 0;
      }
      resolve(out);
    };
    img.onerror = reject;
    img.src = `data:image/png;base64,${maskPng}`;
  });
}

/** Reads the painting canvas's alpha channel (the "is this painted" signal,
 * see decodeMaskPngToImageData) and thresholds it into a strictly bilevel
 * black/white PNG -- true bool-per-pixel storage, not a soft/anti-aliased
 * gradient, matching how these masks are actually used (edge softening is a
 * separate, explicit step: ComfyUI's InpaintCropImproved feathers downstream
 * of native.mask, native.transplant has its own feather param). */
function exportMaskPng(canvas: HTMLCanvasElement): string {
  const ctx = canvas.getContext("2d");
  if (!ctx) return "";
  const src = ctx.getImageData(0, 0, canvas.width, canvas.height);
  const out = document.createElement("canvas");
  out.width = canvas.width;
  out.height = canvas.height;
  const octx = out.getContext("2d");
  if (!octx) return "";
  const dst = octx.createImageData(canvas.width, canvas.height);
  for (let i = 0; i < src.data.length; i += 4) {
    const v = src.data[i + 3] > 127 ? 255 : 0;
    dst.data[i] = dst.data[i + 1] = dst.data[i + 2] = v;
    dst.data[i + 3] = 255;
  }
  octx.putImageData(dst, 0, 0);
  return out.toDataURL("image/png").split(",")[1] ?? "";
}

export interface PaintMask {
  /** Attach to the canvas that holds the mask. It may be the visible one
   * (MaskPreview) or a `display: none` one the consumer composites from
   * (TransplantPreview) -- either works, a hidden canvas still has a real
   * 2D context. */
  canvasRef: React.RefObject<HTMLCanvasElement>;
  brushRadius: number;
  setBrushRadius: (radius: number) => void;
  erasing: boolean;
  setErasing: (erasing: boolean) => void;
  hasStrokes: boolean;
  undoCount: number;
  undo: () => void;
  clear: () => void;
  /** Spread onto whatever element the user actually points at -- the mask
   * canvas itself, or the visible overlay stacked above it. Coordinates come
   * from that element's own rect, so the two cases behave identically. */
  handlers: {
    onPointerDown: (e: React.PointerEvent) => void;
    onPointerMove: (e: React.PointerEvent) => void;
    onPointerUp: (e: React.PointerEvent) => void;
    onPointerCancel: (e: React.PointerEvent) => void;
  };
}

export function usePaintMask({
  maskPng,
  natural,
  resetKey,
  onCommit,
  onChange,
}: {
  maskPng: string | null;
  /** The source image's real pixel size; the canvas is sized from it (capped
   * at MASK_MAX_DIM) once it's known. */
  natural: { w: number; h: number } | null;
  /** Re-initialises the canvas when it changes -- the URL(s) of the image(s)
   * being painted over, so switching images starts from a clean canvas. */
  resetKey: string;
  onCommit: (maskPng: string | null) => void;
  /** Fires after every pixel change (load, stroke, undo, clear) for consumers
   * that render their own view of the mask and need to redraw it. */
  onChange?: () => void;
}): PaintMask {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [brushRadius, setBrushRadius] = useState(24);
  const [erasing, setErasing] = useState(false);
  const [hasStrokes, setHasStrokes] = useState(Boolean(maskPng));
  const drawing = useRef(false);
  const lastPoint = useRef<{ x: number; y: number } | null>(null);
  const undoStack = useRef<ImageData[]>([]);
  const [undoCount, setUndoCount] = useState(0);
  // Held in a ref so a consumer's inline arrow function doesn't have to be
  // memoized just to keep the init effect below from re-running.
  const onChangeRef = useRef(onChange);
  onChangeRef.current = onChange;
  const changed = () => onChangeRef.current?.();

  // Size the canvas's internal resolution once the image's real dimensions
  // are known, then load any existing mask into it (re-run only when the
  // image itself changes -- not on every maskPng echo from a commit, which
  // would otherwise fight an in-progress stroke the same way CropPreview's
  // dragging guard protects against).
  useEffect(() => {
    if (!natural) return;
    const canvas = canvasRef.current;
    const ctx = canvas?.getContext("2d");
    if (!canvas || !ctx) return;
    const scale = Math.min(1, MASK_MAX_DIM / Math.max(natural.w, natural.h));
    canvas.width = Math.max(1, Math.round(natural.w * scale));
    canvas.height = Math.max(1, Math.round(natural.h * scale));
    undoStack.current = [];
    setUndoCount(0);
    if (maskPng) {
      decodeMaskPngToImageData(maskPng, canvas.width, canvas.height).then((data) => {
        ctx.putImageData(data, 0, 0);
        changed();
      });
      setHasStrokes(true);
    } else {
      ctx.putImageData(emptyMaskImageData(ctx, canvas.width, canvas.height), 0, 0);
      setHasStrokes(false);
      changed();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [natural, resetKey]);

  /** Pointer position in the mask canvas's own (capped) pixel space, derived
   * from the pointed-at element's rendered CSS size -- same approach as
   * CropPreview.tsx's natural-image-pixel tracking. */
  const pointFromEvent = (e: React.PointerEvent): { x: number; y: number } | null => {
    const canvas = canvasRef.current;
    if (!canvas) return null;
    const rect = e.currentTarget.getBoundingClientRect();
    return { x: ((e.clientX - rect.left) / rect.width) * canvas.width, y: ((e.clientY - rect.top) / rect.height) * canvas.height };
  };

  const strokeTo = (p: { x: number; y: number }) => {
    const ctx = canvasRef.current?.getContext("2d");
    if (!ctx) return;
    ctx.globalCompositeOperation = erasing ? "destination-out" : "source-over";
    ctx.fillStyle = ctx.strokeStyle = "rgba(255, 60, 60, 1)";
    ctx.lineWidth = brushRadius * 2;
    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    if (lastPoint.current) {
      ctx.beginPath();
      ctx.moveTo(lastPoint.current.x, lastPoint.current.y);
      ctx.lineTo(p.x, p.y);
      ctx.stroke();
    } else {
      ctx.beginPath();
      ctx.arc(p.x, p.y, brushRadius, 0, Math.PI * 2);
      ctx.fill();
    }
    lastPoint.current = p;
    changed();
  };

  const pushUndo = () => {
    const canvas = canvasRef.current;
    const ctx = canvas?.getContext("2d");
    if (!canvas || !ctx) return;
    undoStack.current.push(ctx.getImageData(0, 0, canvas.width, canvas.height));
    if (undoStack.current.length > UNDO_LIMIT) undoStack.current.shift();
    setUndoCount(undoStack.current.length);
  };

  const commit = () => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    onCommit(exportMaskPng(canvas));
  };

  const onPointerDown = (e: React.PointerEvent) => {
    // Only the primary button/touch draws -- a middle-click drives useMaskZoom's
    // pan gesture instead (see below), and without this guard it would also
    // start a stroke underneath the pan.
    if (e.button !== 0) return;
    e.preventDefault();
    e.currentTarget.setPointerCapture(e.pointerId);
    pushUndo();
    drawing.current = true;
    lastPoint.current = null;
    const p = pointFromEvent(e);
    if (p) strokeTo(p);
    setHasStrokes(true);
  };
  const onPointerMove = (e: React.PointerEvent) => {
    if (!drawing.current) return;
    const p = pointFromEvent(e);
    if (p) strokeTo(p);
  };
  const onPointerUp = (e: React.PointerEvent) => {
    if (!drawing.current) return;
    drawing.current = false;
    lastPoint.current = null;
    e.currentTarget.releasePointerCapture(e.pointerId);
    commit();
  };

  const undo = () => {
    const canvas = canvasRef.current;
    const ctx = canvas?.getContext("2d");
    const prev = undoStack.current.pop();
    if (!canvas || !ctx || !prev) return;
    setUndoCount(undoStack.current.length);
    ctx.putImageData(prev, 0, 0);
    changed();
    commit();
  };

  const clear = () => {
    const canvas = canvasRef.current;
    const ctx = canvas?.getContext("2d");
    if (!canvas || !ctx) return;
    pushUndo();
    ctx.putImageData(emptyMaskImageData(ctx, canvas.width, canvas.height), 0, 0);
    setHasStrokes(false);
    changed();
    onCommit(null);
  };

  return {
    canvasRef,
    brushRadius,
    setBrushRadius,
    erasing,
    setErasing,
    hasStrokes,
    undoCount,
    undo,
    clear,
    handlers: { onPointerDown, onPointerMove, onPointerUp, onPointerCancel: onPointerUp },
  };
}

export type MaskZoom = ReturnType<typeof useMaskZoom>;

/** Zoom + pan for a paint-mask viewport, built on the same usePinchPan gesture
 * math as ZoomableImage/CompareModal/Board (scroll/pinch to zoom, focal-stable
 * on the cursor). Panning is bound to a middle-mouse drag rather than the
 * primary drag those other viewers use, because the primary drag here already
 * means "paint a stroke" (usePaintMask's own handlers ignore any other
 * button, for the same reason). A two-finger touch pinch still zooms. */
export function useMaskZoom() {
  const containerRef = useRef<HTMLDivElement>(null);
  const pan = usePinchPan(containerRef, true, { panButton: "middle" });
  return { ...pan, containerRef };
}

/** The outer frame both paint editors zoom/pan inside: a fixed, clipped
 * viewport sized from the source image's aspect ratio, with an inner layer
 * (the image + mask canvas(es), passed as `children`) that actually carries
 * the zoom/pan transform. Shared because the sizing and transform math is
 * identical for MaskPreview (one canvas) and TransplantPreview (two) -- only
 * what's inside differs. */
export function MaskZoomViewport({
  natural,
  zoom,
  children,
}: {
  natural: { w: number; h: number } | null;
  zoom: MaskZoom;
  children: React.ReactNode;
}) {
  return (
    <div
      ref={zoom.containerRef}
      {...zoom.handlers}
      style={{
        position: "relative",
        overflow: "hidden",
        userSelect: "none",
        lineHeight: 0,
        borderRadius: 4,
        aspectRatio: natural ? `${natural.w} / ${natural.h}` : undefined,
        cursor: zoom.grabbing ? "grabbing" : undefined,
      }}
    >
      <div
        style={{
          position: "absolute",
          inset: 0,
          transform: `translate(${zoom.view.x}px, ${zoom.view.y}px) scale(${zoom.view.zoom})`,
          transformOrigin: "center",
        }}
      >
        {children}
      </div>
    </div>
  );
}

/** Brush size / paint-erase / undo / clear / zoom -- identical controls and
 * identical strings in both editors, so it's one component rather than the
 * same elements written twice. `children` takes whatever extra control an
 * editor has of its own (the transplant editor's opacity slider). */
export function PaintMaskToolbar({ paint, zoom, children }: { paint: PaintMask; zoom: MaskZoom; children?: React.ReactNode }) {
  const t = useT();
  return (
    <div style={{ display: "flex", gap: 6, marginBottom: 4, alignItems: "center", flexWrap: "wrap" }}>
      <label style={{ fontSize: 10, display: "flex", alignItems: "center", gap: 4 }}>
        {t("mask.brush")}
        <input type="range" min={4} max={100} value={paint.brushRadius} onChange={(e) => paint.setBrushRadius(Number(e.target.value))} />
      </label>
      <button className={paint.erasing ? "active" : ""} style={{ fontSize: 10, padding: "1px 6px" }} onClick={() => paint.setErasing(!paint.erasing)}>
        {paint.erasing ? t("mask.erasing") : t("mask.painting")}
      </button>
      <button style={{ fontSize: 10, padding: "1px 6px" }} onClick={paint.undo} disabled={paint.undoCount === 0}>
        {t("mask.undo")}
      </button>
      <button style={{ fontSize: 10, padding: "1px 6px" }} onClick={paint.clear} disabled={!paint.hasStrokes}>
        {t("common.clear")}
      </button>
      <span style={{ display: "flex", alignItems: "center", gap: 2 }} title={t("mask.zoomHint")}>
        <button style={{ fontSize: 10, padding: "1px 6px" }} onClick={() => zoom.zoomBy(1 / 1.4)} disabled={zoom.view.zoom <= MIN_ZOOM}>
          −
        </button>
        <button style={{ fontSize: 10, padding: "1px 6px", minWidth: 34 }} onClick={zoom.reset} disabled={zoom.view.zoom <= MIN_ZOOM}>
          {Math.round(zoom.view.zoom * 100)}%
        </button>
        <button style={{ fontSize: 10, padding: "1px 6px" }} onClick={() => zoom.zoomBy(1.4)}>
          +
        </button>
      </span>
      {children}
    </div>
  );
}
