import { useEffect, useRef, useState } from "react";
import { useT } from "../i18n";
import { MaskZoomViewport, PaintMaskToolbar, useMaskZoom, usePaintMask } from "./paintMask";

/** The editor for native.transplant: two images stacked as layers, the target
 * on top and the source underneath, with a brush that punches holes in the top
 * one so the lower image's version of that area shows through. What's painted
 * is literally what you see, and it's the same bilevel PNG the backend
 * (TransplantBackend) composites with -- so unlike MaskPreview, where the mask
 * itself is the picture, here the picture is the finished result.
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
  const [targetImg, setTargetImg] = useState<HTMLImageElement | null>(null);
  // Fades the whole top layer so both images can be compared before deciding
  // what to transplant -- purely a viewing aid, nothing about it is committed.
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
   * with "destination-out", which is what turns painted pixels into holes. */
  const redraw = () => {
    const mask = paint.canvasRef.current;
    const view = viewRef.current;
    if (!mask || !view || !targetImg || mask.width === 0) return;
    if (view.width !== mask.width || view.height !== mask.height) {
      view.width = mask.width;
      view.height = mask.height;
    }
    const ctx = view.getContext("2d");
    if (!ctx) return;
    ctx.globalCompositeOperation = "source-over";
    ctx.filter = "none";
    ctx.clearRect(0, 0, view.width, view.height);
    ctx.drawImage(targetImg, 0, 0, view.width, view.height);
    // feather is in the target's real pixels; the canvas is capped
    // (MASK_MAX_DIM), so the blur has to be scaled into canvas pixels or a
    // 4px feather would look like a 20px one on a 4K image.
    const blur = feather > 0 && targetImg.naturalWidth > 0 ? (feather * view.width) / targetImg.naturalWidth : 0;
    ctx.globalCompositeOperation = "destination-out";
    ctx.filter = blur >= 0.5 ? `blur(${blur}px)` : "none";
    ctx.drawImage(mask, 0, 0);
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
      <MaskZoomViewport natural={natural} zoom={zoom}>
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
            opacity: topOpacity,
            cursor: "crosshair",
            touchAction: "none",
          }}
          {...paint.handlers}
        />
      </MaskZoomViewport>
      <div style={{ fontSize: 10, color: "var(--text-dim)", marginTop: 4 }}>{t("transplant.hint")}</div>
    </div>
  );
}
