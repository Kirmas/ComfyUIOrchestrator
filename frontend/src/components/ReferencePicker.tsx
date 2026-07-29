import { useEffect, useMemo, useState } from "react";
import { createPortal } from "react-dom";
import { resolveAssetUrl } from "../api/client";
import { boardApi } from "../api/endpoints";
import type { Asset } from "../types";
import { cx } from "../utils";

/** Picks an image out of the project's reference library (the assets the idea
 * board owns) to place in a grid cell.
 *
 * This is the flat, tag-filterable presentation of exactly the same storage the
 * board shows by position -- one library, two views (roadmap.md §1). Free
 * coordinates are right for curating an idea; a filter is right for finding one
 * picture among sixty.
 *
 * The caller places the result as an `asset.refasset`, never as an owning node:
 * Asset.node_id is ondelete=CASCADE, so a cell that owned a board image would
 * destroy it on deletion. The grid references, the board owns.
 */

interface Props {
  projectId: string;
  onPick: (asset: Asset) => void | Promise<void>;
  onClose: () => void;
}

export function ReferencePicker({ projectId, onPick, onClose }: Props) {
  const [assets, setAssets] = useState<Asset[]>([]);
  const [activeTag, setActiveTag] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    boardApi
      .assets(projectId)
      .then(setAssets)
      .finally(() => setLoading(false));
  }, [projectId]);

  const tags = useMemo(() => [...new Set(assets.flatMap((a) => a.tags ?? []))].sort(), [assets]);
  const shown = activeTag ? assets.filter((a) => (a.tags ?? []).includes(activeTag)) : assets;

  return createPortal(
    // Portaled to the body: the cell this opens from sits inside the grid's
    // pan/zoom transform, and any ancestor transform makes position: fixed
    // resolve against that ancestor instead of the viewport -- which opens the
    // dialog hundreds of pixels off-screen (2026-07-21 incident).
    <div className="image-modal-backdrop" onClick={onClose}>
      <div className="params-modal-content reference-picker" onClick={(e) => e.stopPropagation()}>
        <h3>З референсів</h3>

        {tags.length > 0 && (
          <div className="reference-tags">
            <button className={cx(!activeTag && "active")} onClick={() => setActiveTag(null)}>
              всі
            </button>
            {tags.map((tag) => (
              <button key={tag} className={cx(activeTag === tag && "active")} onClick={() => setActiveTag(tag)}>
                {tag}
              </button>
            ))}
          </div>
        )}

        {loading ? (
          <p style={{ color: "var(--text-dim)" }}>Завантаження…</p>
        ) : shown.length === 0 ? (
          <p style={{ color: "var(--text-dim)" }}>
            У бібліотеці проєкту порожньо. Завантажте зображення на дошку ідей (вкладка Board).
          </p>
        ) : (
          <div className="reference-grid">
            {shown.map((asset) => (
              <button key={asset.id} className="reference-item" onClick={() => void onPick(asset)} title={(asset.tags ?? []).join(", ")}>
                <img src={resolveAssetUrl(asset.url)} alt="" />
              </button>
            ))}
          </div>
        )}

        <div style={{ display: "flex", justifyContent: "flex-end", marginTop: 8 }}>
          <button onClick={onClose}>Скасувати</button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
