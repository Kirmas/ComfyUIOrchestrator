import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { Asset } from "../types";
import { resolveAssetUrl } from "../api/client";
import { useT } from "../i18n";
import { cx } from "../utils";
import { usePinchPan } from "../usePinchPan";

type Dims = { w: number; h: number };
// Two images only compare cleanly if they share an aspect ratio -- otherwise
// the overlay's pixels don't line up with the base's and you'd be comparing
// unrelated regions. Same aspect but different resolution is fine: both are
// displayed fit-to-screen, so the smaller one is just scaled up to the same
// on-screen box. 1% tolerance absorbs rounding (e.g. 1152x896 vs 576x448).
const aspectMismatch = (l: Dims, r: Dims): boolean => {
  const ratioL = l.w / l.h;
  const ratioR = r.w / r.h;
  return Math.abs(ratioL - ratioR) > 0.01 * Math.max(ratioL, ratioR);
};

/** Overlay-slider compare of two image assets -- mirrors ComfyUI's own Image
 * Comparer node: everything left of the divider shows `left`, everything right
 * shows `right`, both aligned. Two explicit modes, toggled by one button:
 *
 *  - "compare" (default): drag with one pointer to move the divider line.
 *  - "zoom": the divider freezes (glued to the image content, so it tracks the
 *    same feature) and the gestures instead zoom + pan BOTH images together
 *    (usePinchPan, same as the single-image viewer) to inspect fine detail.
 *    Switching back to compare resets the zoom to fit.
 *
 * Image-vs-image only -- mesh assets aren't offered as compare candidates.
 *
 * onSelectLeft/onDiscardLeft: only supplied by the caller when `left` is one
 * candidate of an asset.select picker (Grid.tsx derives that from the source
 * node's node_type, since this component only ever sees bare Assets) -- lets
 * the user act on an undecided candidate right from the compare view instead
 * of having to close it and find the same candidate again in the grid. Never
 * offered for `right`: a picker can't be a compare target in the first place
 * (isPickable excludes it), so there's never a matching decision to make on
 * that side. */
export function CompareModal({
  left,
  right,
  onClose,
  onSelectLeft,
  onDiscardLeft,
}: {
  left: Asset;
  right: Asset;
  onClose: () => void;
  onSelectLeft?: () => void;
  onDiscardLeft?: () => void;
}) {
  const t = useT();
  const containerRef = useRef<HTMLDivElement>(null);
  const [mode, setMode] = useState<"compare" | "zoom">("compare");
  // The divider position as a fraction (0..1) of the image's own content
  // width -- NOT a fraction of the screen. That's what keeps it glued to the
  // same feature when the images are panned/zoomed in zoom mode: the clip is
  // applied to the (transformed) overlay img in its local coordinates, so the
  // transform carries the seam along with the pixels for free.
  const [u, setU] = useState(0.5);
  // contW = the (possibly full-screen) clip container's width; iw = the
  // displayed image's own width. On desktop the container hugs the image so
  // they're equal, but in the fullscreen phone viewer the container is the
  // whole screen while the image is only fit-sized (letterboxed) -- keeping
  // them separate is what lets the divider stay on the image AND lets a
  // zoomed image spill past the letterbox to fill the screen.
  const [metrics, setMetrics] = useState({ contW: 0, iw: 0 });
  const [dims, setDims] = useState<{ l?: Dims; r?: Dims }>({});
  const draggingRef = useRef(false);
  const stageRef = useRef<HTMLDivElement>(null);

  const { view, grabbing, reset, handlers } = usePinchPan(containerRef, mode === "zoom");

  useEffect(() => {
    const c = containerRef.current;
    const s = stageRef.current;
    if (!c || !s) return;
    const measure = () => setMetrics({ contW: c.clientWidth, iw: s.offsetWidth });
    const observer = new ResizeObserver(measure);
    observer.observe(c);
    observer.observe(s);
    measure();
    return () => observer.disconnect();
  }, []);

  const toggleMode = () => {
    setMode((m) => {
      if (m === "zoom") reset(); // going back to compare -> zoom snaps to fit
      return m === "zoom" ? "compare" : "zoom";
    });
  };

  // Screen X of the content-fraction u under the current transform, as a % of
  // the container width. The image is centered in the container, transform-
  // origin its own center, so: screenX = contW/2 + (u - 0.5)*iw*zoom + panX.
  // At fit (zoom 1, pan 0, contW == iw) this is just u.
  const { contW, iw } = metrics;
  const handlePct = contW ? (0.5 + ((u - 0.5) * iw * view.zoom + view.x) / contW) * 100 : u * 100;

  // Inverse of the above: which content fraction sits under a given client X.
  const uFromClientX = (clientX: number): number => {
    const rect = containerRef.current?.getBoundingClientRect();
    if (!rect || rect.width === 0 || iw === 0) return u;
    const localScreenX = clientX - rect.left;
    const localX = (localScreenX - rect.width / 2 - view.x) / view.zoom + iw / 2;
    return Math.min(1, Math.max(0, localX / iw));
  };

  // Slider-mode divider drag (only when not in zoom mode -- there usePinchPan's
  // own handlers, spread below, own the pointer instead).
  const onSliderDown = (e: React.PointerEvent) => {
    e.currentTarget.setPointerCapture(e.pointerId);
    draggingRef.current = true;
    setU(uFromClientX(e.clientX));
  };
  const onSliderMove = (e: React.PointerEvent) => {
    if (!draggingRef.current) return;
    setU(uFromClientX(e.clientX));
  };
  const endSliderDrag = () => {
    draggingRef.current = false;
  };

  const zoomMode = mode === "zoom";
  const containerHandlers = zoomMode
    ? handlers
    : { onPointerDown: onSliderDown, onPointerMove: onSliderMove, onPointerUp: endSliderDrag, onPointerCancel: endSliderDrag };

  const mismatch = dims.l && dims.r ? aspectMismatch(dims.l, dims.r) : false;
  const imgTransform = `translate(${view.x}px, ${view.y}px) scale(${view.zoom})`;

  // Portalled to document.body -- CompareModal is rendered from Grid, whose
  // .main-area is an overflow:auto scroll container. A position:fixed element
  // (the fullscreen phone viewer) nested inside a scroll container is
  // mispositioned relative to the scroll on mobile browsers, so the backdrop
  // showed but the images sat off-screen. Portalling out to the body (same as
  // NodeCell's modals) makes fixed positioning viewport-relative again.
  return createPortal(
    <div className="image-modal-backdrop" onClick={onClose}>
      <div className="compare-modal-content image-modal-fullscreen" onClick={(e) => e.stopPropagation()}>
        {mismatch ? (
          <div className="compare-error">
            <p className="error-text">{t("compare.mismatch")}</p>
            <p className="node-cell-hint">
              {dims.l!.w}×{dims.l!.h} vs {dims.r!.w}×{dims.r!.h}
            </p>
          </div>
        ) : (
          <div
            ref={containerRef}
            className={cx("compare-slider", zoomMode && "zooming")}
            style={{ cursor: zoomMode ? (grabbing ? "grabbing" : "grab") : "ew-resize" }}
            {...containerHandlers}
          >
            {/* The stage is sized to the fit image and centered in the (maybe
                full-screen) container; the imgs' zoom transform overflows it
                and is clipped only at the container edge, so zoom fills the
                screen instead of staying inside the letterboxed image box. */}
            <div ref={stageRef} className="compare-stage">
            <img
              src={resolveAssetUrl(left.url)}
              alt="left"
              className="compare-slider-base"
              draggable={false}
              style={{ transform: imgTransform }}
              onLoad={(e) => {
                // Capture the element now -- e.currentTarget is nulled once the
                // handler returns, so reading it inside the async setDims
                // updater would throw (it did: "reading 'naturalWidth' of null").
                const img = e.currentTarget;
                setDims((d) => ({ ...d, l: { w: img.naturalWidth, h: img.naturalHeight } }));
              }}
            />
            <div className="compare-slider-overlay">
              <img
                src={resolveAssetUrl(right.url)}
                alt="right"
                className="compare-overlay-img"
                draggable={false}
                style={{ transform: imgTransform, clipPath: `inset(0 0 0 ${u * 100}%)` }}
                onLoad={(e) => {
                  const img = e.currentTarget;
                  setDims((d) => ({ ...d, r: { w: img.naturalWidth, h: img.naturalHeight } }));
                }}
              />
            </div>
            {/* Which side is which -- when the two images are nearly identical
                it's otherwise impossible to tell. "1" = the one you started
                the compare from (left), "2" = the one you picked to compare
                against (right). Anchored to the stage (the image box) so they
                sit in the picture's own corners and stay within the visible
                area -- pinned to the full-screen container's bottom they hid
                behind the mobile browser's bottom toolbar. */}
            <div className="compare-label left">1</div>
            <div className="compare-label right">2</div>
            </div>
            <div className={cx("compare-slider-handle", zoomMode && "dashed")} style={{ left: `${handlePct}%` }}>
              {!zoomMode && <div className="compare-slider-grip">⇔</div>}
            </div>
          </div>
        )}

        {/* Rendered last (and stopping their own pointer events) so they stay
            on top of the slider and its full-screen zoomed image, which was
            swallowing taps on them in zoom mode. */}
        <button
          type="button"
          className="compare-mode-toggle"
          onClick={toggleMode}
          onPointerDown={(e) => e.stopPropagation()}
          onPointerUp={(e) => e.stopPropagation()}
          title={zoomMode ? t("compare.backTitle") : t("compare.zoomTitle")}
          disabled={mismatch}
        >
          {zoomMode ? t("compare.compare") : t("compare.zoom")}
        </button>
        {/* Only present when `left` is an asset.select candidate (Grid.tsx
            gates this) -- same "keep this one" / "reject this one" actions
            CandidatesGrid offers in the grid itself, so a candidate can be
            settled right from the compare view. Never shown for `right`. */}
        {(onSelectLeft || onDiscardLeft) && (
          <div className="compare-left-actions">
            {onSelectLeft && (
              <button
                type="button"
                className="compare-select-left"
                onClick={onSelectLeft}
                onPointerDown={(e) => e.stopPropagation()}
                onPointerUp={(e) => e.stopPropagation()}
                title={t("compare.selectLeftTitle")}
              >
                {t("compare.selectLeft")}
              </button>
            )}
            {onDiscardLeft && (
              <button
                type="button"
                className="compare-discard-left"
                onClick={onDiscardLeft}
                onPointerDown={(e) => e.stopPropagation()}
                onPointerUp={(e) => e.stopPropagation()}
                title={t("compare.discardLeftTitle")}
              >
                {t("compare.discardLeft")}
              </button>
            )}
          </div>
        )}
        <button
          type="button"
          className="image-modal-close"
          onClick={onClose}
          onPointerDown={(e) => e.stopPropagation()}
          onPointerUp={(e) => e.stopPropagation()}
          title={t("compare.closeTitle")}
        >
          ×
        </button>
      </div>
    </div>,
    document.body,
  );
}
