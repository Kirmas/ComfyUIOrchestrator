import { useEffect, useRef, useState } from "react";
import { useT } from "../i18n";
import { BrushCursorDot, MaskZoomViewport, PaintMaskToolbar, useBrushCursor, useMaskZoom, usePaintMask } from "./paintMask";

/** The editor for native.transplant: two DOM layers, not two images.
 *
 * The top layer is the "hamburger" -- target + mask + source, punched and
 * composited into the one always-fully-baked result, exactly what
 * TransplantBackend would actually produce. It is never faded internally;
 * what's painted is literally what you see, the same bilevel PNG the backend
 * composites with (unlike MaskPreview, where the mask itself is the picture,
 * here the picture is the finished result).
 *
 * The bottom layer is a single plain compare-reference image, chosen by brush
 * mode: source while painting (what you're about to graft in), target while
 * erasing (what un-painting would give back). It only ever becomes visible by
 * fading the *entire top layer, as one wrapped unit*, down from 100% via the
 * top-opacity slider -- at 100% you see nothing but the true result, and
 * below that a blend of "the real result" against "the mode-appropriate
 * reference," never two half-composited layers ghosting into each other
 * (2026-09-14: the previous single-layer version applied that slider to the
 * hamburger's own compositing, which also meant swapping the bottom image by
 * mode made the whole thing look identical to itself while erasing, since
 * the hamburger's own base layer is already the target).
 *
 * The stack is sized by the *target*: it's the image being fixed, so it decides
 * the output's dimensions, and the source is stretched to fill the same box --
 * matching the backend, which scales the source to the target's size before
 * pasting (`object-fit: fill` is that same non-uniform scale, not a crop).
 */
export function TransplantPreview({
  targetUrl,
  sourceUrl,
  maskPng,
  feather,
  onCommit,
}: {
  targetUrl: string;
  sourceUrl: string;
  maskPng: string | null;
  /** Edge softening in target pixels, previewed here so what's on screen
   * matches what the backend will render. */
  feather: number;
  onCommit: (maskPng: string | null) => void;
}) {
  const t = useT();
  const viewRef = useRef<HTMLCanvasElement>(null);
  // Scratch buffer redraw() binarizes the mask's alpha into before using it
  // for destination-out -- never attached to the DOM, just a place to run
  // getImageData/putImageData on (see redraw()'s own comment for why).
  const scratchRef = useRef<HTMLCanvasElement>(document.createElement("canvas"));
  const [targetImg, setTargetImg] = useState<HTMLImageElement | null>(null);
  // Fades the whole hamburger layer (see the component docstring) so it can
  // be compared against the mode-appropriate reference before deciding what
  // to transplant/restore -- purely a viewing aid, nothing about it is
  // committed, and it never touches the hamburger's own compositing.
  const [topOpacity, setTopOpacity] = useState(1);

  useEffect(() => {
    const img = new Image();
    img.onload = () => setTargetImg(img);
    img.src = targetUrl;
    return () => {
      img.onload = null;
      setTargetImg(null);
    };
  }, [targetUrl]);

  const natural = targetImg ? { w: targetImg.naturalWidth, h: targetImg.naturalHeight } : null;

  /** Repaints the visible layer: the target, minus the painted region. The
   * mask canvas is hidden (it's the storage, not the view) and gets drawn in
   * with "destination-out", which is what turns painted pixels into holes.
   *
   * Sized from the target's own natural resolution, deliberately *not* the
   * mask canvas's (capped at MASK_MAX_DIM so the stored base64 stays small,
   * see paintMask.tsx) -- this view canvas is what the user actually looks
   * at, stretched by CSS to fill the viewport either way, so drawing the
   * target into a mere 768px buffer first made every non-painted pixel
   * visibly softer than the crisp backdrop <img>/final result sitting right
   * next to it for no reason: the mask's own resolution cap doesn't need to
   * limit anything but the mask. drawImage upscales the small mask onto this
   * larger canvas the same way CSS used to upscale the whole thing, so the
   * hole edges look the same -- only the untouched target detail improves. */
  const redraw = () => {
    const mask = paint.canvasRef.current;
    const view = viewRef.current;
    if (!mask || !view || !targetImg || mask.width === 0) return;
    if (view.width !== targetImg.naturalWidth || view.height !== targetImg.naturalHeight) {
      view.width = targetImg.naturalWidth;
      view.height = targetImg.naturalHeight;
    }
    const ctx = view.getContext("2d");
    if (!ctx) return;
    ctx.globalCompositeOperation = "source-over";
    ctx.filter = "none";
    ctx.clearRect(0, 0, view.width, view.height);
    ctx.drawImage(targetImg, 0, 0, view.width, view.height);

    // The mask canvas's alpha is a boolean "is this masked" flag, not a
    // reveal *amount* -- a reloaded mask redraws at a deliberately
    // translucent alpha (paintMask.tsx's LOADED_STROKE_ALPHA) purely so
    // MaskPreview's overlay reads as an annotation, and even a freshly-drawn
    // stroke has a naturally antialiased edge. destination-out below reads
    // it quantitatively though, so left as-is it only ever punches a
    // partial-strength hole. Binarize it on a scratch canvas first --
    // exactly what the backend does too (thresholds to a hard mask on
    // export, only ever blurs *that*) -- rather than trying to compensate
    // with a filter-based opacity boost: `opacity()` in a canvas filter
    // chain is unreliable past 100% across browsers, which is exactly why a
    // reloaded mask's hole settled at ~160/255 (visibly short of a full
    // reveal) instead of the intended full-strength one (2026-09-14).
    const scratch = scratchRef.current;
    if (scratch.width !== mask.width || scratch.height !== mask.height) {
      scratch.width = mask.width;
      scratch.height = mask.height;
    }
    const sctx = scratch.getContext("2d");
    if (!sctx) return;
    sctx.clearRect(0, 0, scratch.width, scratch.height);
    sctx.drawImage(mask, 0, 0);
    const maskData = sctx.getImageData(0, 0, scratch.width, scratch.height);
    for (let i = 3; i < maskData.data.length; i += 4) {
      maskData.data[i] = maskData.data[i] > 0 ? 255 : 0;
    }
    sctx.putImageData(maskData, 0, 0);

    // feather is already in the target's real pixels, and the view canvas
    // now *is* that resolution -- no more scaling into a capped canvas size.
    const blur = feather > 0 ? feather : 0;
    ctx.globalCompositeOperation = "destination-out";
    ctx.filter = blur >= 0.5 ? `blur(${blur}px)` : "none";
    ctx.drawImage(scratch, 0, 0, view.width, view.height);
    ctx.filter = "none";
    ctx.globalCompositeOperation = "source-over";
  };

  const paint = usePaintMask({
    maskPng,
    natural,
    resetKey: `${targetUrl}|${sourceUrl}`,
    onCommit,
    onChange: redraw,
  });
  const zoom = useMaskZoom();
  // brushRadius lives in paint.canvasRef's (the hidden storage canvas's) own
  // pixel space, not viewRef's (the visible one, sized from the target's
  // native resolution -- see redraw()) -- see useBrushCursor's docstring.
  const cursor = useBrushCursor(paint.brushRadius, paint.canvasRef, zoom.containerRef);

  // The mask itself only changes through the hook (which calls redraw), but
  // the *rendering* of it also depends on these two.
  useEffect(redraw, [targetImg, feather]); // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <div onClick={(e) => e.stopPropagation()}>
      <PaintMaskToolbar paint={paint} zoom={zoom}>
        <label style={{ fontSize: 10, display: "flex", alignItems: "center", gap: 4 }}>
          {t("transplant.topOpacity")}
          <input
            type="range"
            min={0}
            max={100}
            value={Math.round(topOpacity * 100)}
            onChange={(e) => setTopOpacity(Number(e.target.value) / 100)}
          />
        </label>
      </PaintMaskToolbar>
      <MaskZoomViewport natural={natural} zoom={zoom} overlay={<BrushCursorDot cursor={cursor} />}>
        {/* Bottom layer: the mode-appropriate compare reference. Plain <img>,
         * no compositing of its own -- see the component docstring. */}
        <img
          src={paint.erasing ? targetUrl : sourceUrl}
          alt={paint.erasing ? "transplant target" : "transplant source"}
          style={{ position: "absolute", inset: 0, width: "100%", height: "100%", objectFit: "fill", display: "block" }}
          draggable={false}
        />
        {/* Top layer: the hamburger, wrapped as one unit so topOpacity fades
         * it whole against the bottom layer instead of reaching inside its
         * own target/mask/source compositing. */}
        <div style={{ position: "absolute", inset: 0, opacity: topOpacity }}>
          <img
            src={sourceUrl}
            alt="transplant source"
            style={{ position: "absolute", inset: 0, width: "100%", height: "100%", objectFit: "fill", display: "block" }}
            draggable={false}
          />
          <canvas
            ref={paint.canvasRef}
            style={{ display: "none" }}
          />
          <canvas
            ref={viewRef}
            style={{
              position: "absolute",
              inset: 0,
              width: "100%",
              height: "100%",
              cursor: "crosshair",
              touchAction: "none",
            }}
            onPointerDown={paint.handlers.onPointerDown}
            onPointerMove={(e) => {
              paint.handlers.onPointerMove(e);
              cursor.handlers.onPointerMove(e);
            }}
            onPointerUp={paint.handlers.onPointerUp}
            onPointerCancel={paint.handlers.onPointerCancel}
            onPointerLeave={cursor.handlers.onPointerLeave}
          />
        </div>
      </MaskZoomViewport>
      <div style={{ fontSize: 10, color: "var(--text-dim)", marginTop: 4 }}>{t("transplant.hint")}</div>
    </div>
  );
}
