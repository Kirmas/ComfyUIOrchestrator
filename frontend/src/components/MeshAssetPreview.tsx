import { useEffect, useState } from "react";
import type { SyntheticEvent } from "react";
import { resolveAssetUrl } from "../api/client";
import { assetsApi } from "../api/endpoints";
import type { Asset } from "../types";
import { MeshThumb } from "./Mesh3D";
import { FullSizeModal } from "./NodeCell";

const stop = (e: SyntheticEvent) => e.stopPropagation();

/** A 3D asset shown outside the grid -- a board sticker, a chat message --
 * where all the caller has is the asset id. Same still + fullscreen viewer as
 * a grid cell (MeshThumb / FullSizeModal), so a model looks and opens the same
 * everywhere; there's just no compare, since nothing here has a second one.
 *
 * The modal is portaled to <body>, but React still bubbles its events up the
 * component tree: a drag in the viewer would reach the board sticker (and
 * start moving it, capturing the pointer away from the orbit controls) and a
 * double-click would reach the board canvas (and drop a new note). The
 * wrapper below cuts that off for everything inside the modal. */
export function MeshAssetPreview({ assetId }: { assetId: string }) {
  const [asset, setAsset] = useState<Asset | null>(null);
  const [failed, setFailed] = useState(false);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    let alive = true;
    setFailed(false);
    assetsApi
      .get(assetId)
      .then((a) => {
        if (alive) setAsset(a);
      })
      .catch(() => {
        if (alive) setFailed(true);
      });
    return () => {
      alive = false;
    };
  }, [assetId]);

  if (failed) return <div className="mesh-preview mesh-preview-missing">3D ✕</div>;
  if (!asset) return <div className="mesh-preview"><div className="model-thumb-loading" /></div>;

  return (
    <div className="mesh-preview">
      <MeshThumb asset={asset} onOpen={() => setOpen(true)} />
      {open && (
        <span onPointerDown={stop} onPointerMove={stop} onPointerUp={stop} onDoubleClick={stop} onClick={stop}>
          <FullSizeModal url={resolveAssetUrl(asset.url)} asset={asset} onClose={() => setOpen(false)} />
        </span>
      )}
    </div>
  );
}
