import type { Asset } from "../types";
import { resolveAssetUrl } from "../api/client";
import { ZoomableImage } from "./ZoomableImage";

/** Side-by-side compare of two image assets, each independently zoomable/pannable.
 * Image-vs-image only for now -- mesh assets aren't offered as compare candidates. */
export function CompareModal({ left, right, onClose }: { left: Asset; right: Asset; onClose: () => void }) {
  return (
    <div className="image-modal-backdrop" onClick={onClose}>
      <div className="compare-modal-content" onClick={(e) => e.stopPropagation()}>
        <button type="button" className="image-modal-close" onClick={onClose} title="Close compare">
          ×
        </button>
        <div className="compare-pane">
          <ZoomableImage src={resolveAssetUrl(left.url)} maxWidth="calc(46vw - 24px)" maxHeight="calc(90vh - 24px)" />
        </div>
        <div className="compare-pane">
          <ZoomableImage src={resolveAssetUrl(right.url)} maxWidth="calc(46vw - 24px)" maxHeight="calc(90vh - 24px)" />
        </div>
      </div>
    </div>
  );
}
