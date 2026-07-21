import { useEffect, useRef, useState } from "react";

// Capped independent of the source image's real resolution -- a painted
// brush mask doesn't need source-resolution precision, and this keeps the
// base64 stored in node.params small and predictable regardless of whether
// the underlying photo is 512px or 4K (see MaskBackend on the backend side,
// which nearest-neighbor-upscales back to the source size at execution).
const MASK_MAX_DIM = 768;
const UNDO_LIMIT = 20;

function emptyMaskImageData(ctx: CanvasRenderingContext2D, width: number, height: number): ImageData {
  return ctx.createImageData(width, height);
}

/** Decodes a previously-saved bilevel mask PNG (black = unmasked, white =
 * masked, see MaskBackend) into the display canvas's own representation:
 * transparent where unmasked, translucent red where masked. Alpha (not RGB)
 * is what carries the "is this painted" signal on the display canvas, so
 * painting can use "destination-out" to erase regardless of stroke color. */
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

/** Reads the display canvas's alpha channel (the "is this painted" signal,
 * see decodeMaskPngToImageData) and thresholds it into a strictly bilevel
 * black/white PNG -- true bool-per-pixel storage, not a soft/anti-aliased
 * gradient, matching how these masks are actually used (ComfyUI's own
 * InpaintCropImproved node does its own edge feathering downstream). */
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

/** Freehand binary mask painter: brush over the source image, committed as a
 * bilevel PNG in node.params (see native.mask / MaskBackend). Coordinates
 * and brush size are tracked in the canvas's own (capped, see MASK_MAX_DIM)
 * internal pixel space throughout, converted from pointer events via the
 * ratio between its rendered CSS size and that internal resolution -- same
 * approach as CropPreview.tsx's natural-image-pixel tracking. */
export function MaskPreview({
  imageUrl,
  maskPng,
  onCommit,
}: {
  imageUrl: string;
  maskPng: string | null;
  onCommit: (maskPng: string | null) => void;
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [natural, setNatural] = useState<{ w: number; h: number } | null>(null);
  const [brushRadius, setBrushRadius] = useState(24);
  const [erasing, setErasing] = useState(false);
  const [hasStrokes, setHasStrokes] = useState(Boolean(maskPng));
  const drawing = useRef(false);
  const lastPoint = useRef<{ x: number; y: number } | null>(null);
  const undoStack = useRef<ImageData[]>([]);
  const [undoCount, setUndoCount] = useState(0);

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
      decodeMaskPngToImageData(maskPng, canvas.width, canvas.height).then((data) => ctx.putImageData(data, 0, 0));
      setHasStrokes(true);
    } else {
      ctx.putImageData(emptyMaskImageData(ctx, canvas.width, canvas.height), 0, 0);
      setHasStrokes(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [natural, imageUrl]);

  const pointFromEvent = (e: React.PointerEvent): { x: number; y: number } | null => {
    const canvas = canvasRef.current;
    if (!canvas) return null;
    const rect = canvas.getBoundingClientRect();
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
    commit();
  };

  const clear = () => {
    const canvas = canvasRef.current;
    const ctx = canvas?.getContext("2d");
    if (!canvas || !ctx) return;
    pushUndo();
    ctx.putImageData(emptyMaskImageData(ctx, canvas.width, canvas.height), 0, 0);
    setHasStrokes(false);
    onCommit(null);
  };

  return (
    <div onClick={(e) => e.stopPropagation()}>
      <div style={{ display: "flex", gap: 6, marginBottom: 4, alignItems: "center", flexWrap: "wrap" }}>
        <label style={{ fontSize: 10, display: "flex", alignItems: "center", gap: 4 }}>
          Brush
          <input type="range" min={4} max={100} value={brushRadius} onChange={(e) => setBrushRadius(Number(e.target.value))} />
        </label>
        <button className={erasing ? "active" : ""} style={{ fontSize: 10, padding: "1px 6px" }} onClick={() => setErasing((v) => !v)}>
          {erasing ? "Erasing" : "Painting"}
        </button>
        <button style={{ fontSize: 10, padding: "1px 6px" }} onClick={undo} disabled={undoCount === 0}>
          Undo
        </button>
        <button style={{ fontSize: 10, padding: "1px 6px" }} onClick={clear} disabled={!hasStrokes}>
          Clear
        </button>
      </div>
      <div style={{ position: "relative", userSelect: "none", lineHeight: 0 }}>
        <img
          src={imageUrl}
          alt="mask source"
          style={{ width: "100%", display: "block", borderRadius: 4 }}
          onLoad={(e) => setNatural({ w: e.currentTarget.naturalWidth, h: e.currentTarget.naturalHeight })}
          draggable={false}
        />
        <canvas
          ref={canvasRef}
          style={{ position: "absolute", inset: 0, width: "100%", height: "100%", cursor: "crosshair", touchAction: "none" }}
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerUp}
          onPointerCancel={onPointerUp}
        />
      </div>
    </div>
  );
}
