import { useRef, useState } from "react";
import type { Asset } from "../types";
import { resolveAssetUrl } from "../api/client";

/** Overlay-slider compare of two image assets, fit-to-screen (no zoom/pan) --
 * mirrors ComfyUI's own Image Comparer node: everything left of the handle
 * shows `left`, everything right of it shows `right`, dragged with one
 * pointer instead of eyeballing two side-by-side panes. Image-vs-image only
 * for now -- mesh assets aren't offered as compare candidates. */
export function CompareModal({ left, right, onClose }: { left: Asset; right: Asset; onClose: () => void }) {
  const containerRef = useRef<HTMLDivElement>(null);
  const draggingRef = useRef(false);
  const [pct, setPct] = useState(50);

  const updateFromClientX = (clientX: number) => {
    const rect = containerRef.current?.getBoundingClientRect();
    if (!rect || rect.width === 0) return;
    setPct(Math.min(100, Math.max(0, ((clientX - rect.left) / rect.width) * 100)));
  };

  const onPointerDown = (e: React.PointerEvent) => {
    e.currentTarget.setPointerCapture(e.pointerId);
    draggingRef.current = true;
    updateFromClientX(e.clientX);
  };
  const onPointerMove = (e: React.PointerEvent) => {
    if (!draggingRef.current) return;
    updateFromClientX(e.clientX);
  };
  // pointerup/pointercancel only -- pointer capture redirects move/up
  // targeting regardless of where the cursor physically ends up, so there's
  // no boundary-leave case to also guard against here (see ZoomableImage).
  const endDrag = () => {
    draggingRef.current = false;
  };

  return (
    <div className="image-modal-backdrop" onClick={onClose}>
      <div className="compare-modal-content" onClick={(e) => e.stopPropagation()}>
        <button type="button" className="image-modal-close" onClick={onClose} title="Close compare">
          ×
        </button>
        <div
          ref={containerRef}
          className="compare-slider"
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={endDrag}
          onPointerCancel={endDrag}
        >
          <img src={resolveAssetUrl(left.url)} alt="left" className="compare-slider-base" draggable={false} />
          <div className="compare-slider-overlay" style={{ clipPath: `inset(0 0 0 ${pct}%)` }}>
            <img src={resolveAssetUrl(right.url)} alt="right" className="compare-slider-base" draggable={false} />
          </div>
          <div className="compare-slider-handle" style={{ left: `${pct}%` }}>
            <div className="compare-slider-grip">⇔</div>
          </div>
        </div>
      </div>
    </div>
  );
}
