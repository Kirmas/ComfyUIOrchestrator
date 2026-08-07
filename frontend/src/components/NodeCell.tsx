import { useEffect, useMemo, useRef, useState, type ComponentType } from "react";
import { createPortal } from "react-dom";
import { resolveAssetUrl } from "../api/client";
import { assetsApi, dashboardsApi, jobsApi, nodesApi } from "../api/endpoints";
import { detectCropGroups, resolveCropImageField } from "../cropUtils";
import { isFileDrag } from "../dragUtils";
import { detectMaskGroups, resolveMaskImageField } from "../maskUtils";
import { copyAsset } from "../assetClipboard";
import { assetFace } from "../assetNodes";
import { resolveSlotAsset } from "../slotResolution";
import { useProjectStore } from "../state/projectStore";
import { defaultInputsForSchema, slotFields } from "../templateUtils";
import type { Asset, Backend, Capability, Dashboard, InputRef, Job, NodeItem, NodeStatus, NodeTemplate } from "../types";
import { useT, type TFunc } from "../i18n";
import { cx } from "../utils";
import { capabilityUsesMultiAngleLora } from "../multiAngleLora";
import { capabilityUsesIdeogram4 } from "../ideogram4";
import { CaptionBoxEditor, type CaptionBgOption } from "./CaptionBoxEditor";
import { CropPreview, type CropBox } from "./CropPreview";
import { MaskPreview } from "./MaskPreview";
import { IdeaTextPicker, MacroPreview } from "./IdeaTextPicker";
import { MultiAngleBuilder } from "./MultiAngleBuilder";
import { Model3DThumb } from "./Model3DThumb";
import { ZoomableImage } from "./ZoomableImage";

const STATUS_LABEL_KEY: Record<NodeStatus, `status.${NodeStatus}`> = {
  draft: "status.draft",
  queued: "status.queued",
  running: "status.running",
  done: "status.done",
  error: "status.error",
  discarded: "status.discarded",
};

function statusLabel(t: TFunc, status: NodeStatus): string {
  return t(STATUS_LABEL_KEY[status]);
}

// Shared by CandidatesGrid and SingleOutput -- resolution (read off the
// loaded <img>, not stored anywhere) plus, when known, which backend
// produced this asset, in one overlay instead of two competing for the same
// bottom-of-thumbnail spot.
function AssetMetaTag({ dims, backendName }: { dims: { w: number; h: number } | undefined; backendName: string | undefined }) {
  if (!dims && !backendName) return null;
  const parts = [dims ? `${dims.w}×${dims.h}` : null, backendName ?? null].filter((p): p is string => p !== null);
  return (
    <span className="asset-meta-tag" title={parts.join(" · ")}>
      {parts.join(" · ")}
    </span>
  );
}

export interface Props {
  node: NodeItem;
  templates: NodeTemplate[];
  backends: Backend[];
  capabilities: Capability[];
  outputs: Asset[];
  compareActive: boolean;
  isComparingSource: boolean;
  // True for an asset node sitting inside a spanning workflow node's row
  // range that ISN'T that workflow's actual materialized output (Grid.tsx's
  // isWorkflowOutput) -- e.g. one manually loaded via Change 3's "+ asset" in
  // an otherwise-empty spanned row. Drives the "not this workflow's output"
  // badge in BaseAssetNodeView.
  isManualPlacement: boolean;
  // Armed via this node's own "+ ref elsewhere" button -- Grid.tsx is waiting
  // for a click on an empty cell to complete the RefAsset placement.
  isRefSource: boolean;
  // Armed via this workflow node's own "⧉" button -- Grid.tsx is waiting for a
  // click on an empty workflow cell to place a real copy of this node there
  // (nodesApi.duplicate). Same click-to-complete shape as isRefSource above,
  // deliberately a different gesture and a different result: a second
  // independent node carrying all of this one's local settings, not a pointer.
  isCopySource: boolean;
  registerRef: (nodeId: string, el: HTMLDivElement | null) => void;
  onCellClicked: (node: NodeItem) => void;
  onStartCompare: (node: NodeItem, asset: Asset) => void;
  onStartRef: (node: NodeItem) => void;
  onStartCopy: (node: NodeItem) => void;
  // Manual, one-shot recompute of a workflow node's own row-span, removing
  // any trailing EMPTY rows it doesn't actually need down to the minimum
  // (its input slot count, or further if one of its own outputs still
  // reaches lower) -- see Grid.tsx's shrinkWorkflowToFit for why this is a
  // deliberate button press rather than an automatic effect.
  onShrinkToFit: (node: NodeItem) => void;
  // Set for a workflow node that's the *creator* half of a collapsed chain
  // (see Grid.tsx's collapseInfoByNode and db/models.py's
  // Node.collapse_target_id docstring) -- assetId is the pass-through
  // asset's own id (what the expand action targets), consumerId is the
  // second workflow node's id (neither of these renders its own cell --
  // see Grid.tsx's hiddenChainNodeIds -- so this is the only place their
  // ids are reachable for the "view chain" popup), combinedLabel joins
  // both workflow names (e.g. "Crop+Upscale"). Undefined for a node that
  // isn't a collapsed chain's creator.
  collapseInfo?: { assetId: string; consumerId: string; combinedLabel: string };
}

// node.is_picker (explicit, persistent -- see db/models.py) forces a decision
// on *each* candidate: select it (it stays right here; the rest move out to
// a new picker row) or reject it outright -- rather than a checkbox that
// quietly leaves the rest sitting here forever. A settled node (is_picker
// false) just renders its image, no decision needed.
function CandidatesGrid({
  outputs,
  onSelect,
  onDiscard,
  onImageOpen,
  onCompare,
}: {
  outputs: Asset[];
  onSelect: (asset: Asset) => void;
  onDiscard: (asset: Asset) => void;
  onImageOpen: (url: string) => void;
  onCompare: (asset: Asset) => void;
}) {
  const t = useT();
  const [dimsById, setDimsById] = useState<Record<string, { w: number; h: number }>>({});
  return (
    // No stopPropagation here (unlike the buttons below) -- the thumbnail is
    // most of a cell's tappable area, especially on a phone, so it has to let
    // a plain click through to the outer cell's onClick for "pick cell..." to
    // ever be reachable by tapping the picture itself.
    <div className="output-grid">
      {outputs.map((asset) => (
        <div key={asset.id} className="output-item">
          {asset.kind === "mesh" ? (
            <Model3DThumb url={resolveAssetUrl(asset.url)} />
          ) : (
            <div className="output-thumb">
              <img
                src={resolveAssetUrl(asset.url)}
                alt="output"
                // The grid renders every cell at once (no virtualization) and
                // these are full-size originals, several MB each -- lazy so
                // off-screen cells don't fetch, async-decoded so the ones that
                // do can't block the main thread mid-interaction.
                loading="lazy"
                decoding="async"
                onDoubleClick={() => onImageOpen(resolveAssetUrl(asset.url))}
                onLoad={(e) => {
                  const img = e.currentTarget;
                  if (!img) return;
                  setDimsById((d) => ({ ...d, [asset.id]: { w: img.naturalWidth, h: img.naturalHeight } }));
                }}
                title={t("cell.doubleClickFullSize")}
                style={{ cursor: "zoom-in" }}
              />
              {/* Double-click alone is unreliable on a phone (same reason the
                  params modal grew a button) -- a tappable icon is a sure thing. */}
              <button
                type="button"
                className="zoom-button"
                onClick={(e) => {
                  e.stopPropagation();
                  onImageOpen(resolveAssetUrl(asset.url));
                }}
                title={t("cell.openFullSize")}
              >
                🔍
              </button>
              <button
                type="button"
                className="zoom-button compare-button"
                onClick={(e) => {
                  e.stopPropagation();
                  onCompare(asset);
                }}
                title={t("cell.compareWith")}
              >
                ⇄
              </button>
              <AssetMetaTag
                dims={dimsById[asset.id]}
                backendName={typeof asset.meta.backend_name === "string" ? asset.meta.backend_name : undefined}
              />
            </div>
          )}
          <div style={{ display: "flex", gap: 4, marginTop: 2 }}>
            <button
              style={{ fontSize: 10, padding: "1px 4px" }}
              onClick={(e) => {
                e.stopPropagation();
                onSelect(asset);
              }}
              title={t("cell.selectTitle")}
            >
              {t("cell.select")}
            </button>
            <button
              style={{ fontSize: 10, padding: "1px 4px" }}
              onClick={(e) => {
                e.stopPropagation();
                onDiscard(asset);
              }}
              title={t("cell.discardCandidateTitle")}
            >
              {t("cell.discardCandidate")}
            </button>
          </div>
        </div>
      ))}
    </div>
  );
}

/** The single picture an asset cell shows -- its *face*, in assetNodes.ts's
 * terms -- with the two affordances every asset kind is entitled to: zoom and
 * compare.
 *
 * Shared by all three asset views. The pointer kinds (RefAsset, Subgraph) used
 * to hand-roll a cut-down copy of this: a bare <img> with double-click zoom and
 * neither button, so a referenced or subgraph picture couldn't be opened from
 * the 🔍 nor started as a compare, purely because nobody had copied those lines
 * across a third and fourth time.
 *
 * `borrowed` dims the thumbnail slightly -- the RefAsset card has always done
 * that to signal the picture belongs to another cell. */
function AssetFaceView({
  asset,
  borrowed = false,
  onImageOpen,
  onCompare,
}: {
  asset: Asset;
  borrowed?: boolean;
  onImageOpen: (url: string) => void;
  onCompare: (asset: Asset) => void;
}) {
  const t = useT();
  const [dims, setDims] = useState<{ w: number; h: number } | undefined>(undefined);
  const url = resolveAssetUrl(asset.url);
  return (
    <div className="output-grid">
      <div className="output-item">
        {asset.kind === "mesh" ? (
          <Model3DThumb url={url} />
        ) : (
          <div className="output-thumb">
            <img
              src={url}
              alt="output"
              loading="lazy"
              decoding="async"
              draggable={false}
              onDoubleClick={() => onImageOpen(url)}
              onLoad={(e) => {
                const img = e.currentTarget;
                if (!img) return;
                setDims({ w: img.naturalWidth, h: img.naturalHeight });
              }}
              title={t("cell.doubleClickFullSize")}
              style={{ cursor: "zoom-in", opacity: borrowed ? 0.85 : undefined }}
            />
            <button
              type="button"
              className="zoom-button"
              onClick={(e) => {
                e.stopPropagation();
                onImageOpen(url);
              }}
              title={t("cell.openFullSize")}
            >
              🔍
            </button>
            <button
              type="button"
              className="zoom-button compare-button"
              onClick={(e) => {
                e.stopPropagation();
                onCompare(asset);
              }}
              title={t("cell.compareWith")}
            >
              ⇄
            </button>
            <AssetMetaTag dims={dims} backendName={typeof asset.meta.backend_name === "string" ? asset.meta.backend_name : undefined} />
          </div>
        )}
      </div>
    </div>
  );
}

/** The full-size overlay every asset view opens on zoom.
 *
 * Portalled to document.body, which is load-bearing twice over and was
 * previously re-derived in each of the three views: a cell sits inside Grid's
 * pan/zoom CSS transform (so an inline position:fixed backdrop is no longer
 * viewport-relative), and its wrapper is HTML5 draggable={true} (so a pan
 * gesture started inside a non-portalled modal gets hijacked into a native
 * image-drag). See CLAUDE.md's two 2026-07-21 incidents. */
function FullSizeModal({ url, onClose }: { url: string; onClose: () => void }) {
  const t = useT();
  return createPortal(
    <div className="image-modal-backdrop" onClick={onClose}>
      <div className="image-modal-content image-modal-fullscreen" onClick={(e) => e.stopPropagation()}>
        <button type="button" className="image-modal-close" onClick={onClose} title={t("cell.closeFullSize")}>
          ×
        </button>
        <ZoomableImage src={url} />
      </div>
    </div>,
    document.body,
  );
}

/** This cell's face, resolved by its own asset kind (assetNodes.ts): its own
 * selected/latest output for the kinds that own Asset rows, the pointed-at
 * asset for the two that don't. */
function useAssetFace(node: NodeItem): Asset | null {
  const outputsByNode = useProjectStore((s) => s.outputsByNode);
  const [face, setFace] = useState<Asset | null>(null);
  useEffect(() => {
    let cancelled = false;
    assetFace(node).then((asset) => {
      if (!cancelled) setFace(asset);
    });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [node.id, node.node_type, node.inputs, node.subgraph_dashboard_id, outputsByNode]);
  return face;
}

function BaseAssetNodeView({
  node,
  outputs,
  compareActive,
  isComparingSource,
  isManualPlacement,
  isRefSource,
  registerRef,
  onCellClicked,
  onStartCompare,
  onStartRef,
}: Props) {
  const t = useT();
  const setNode = useProjectStore((s) => s.setNode);
  const refreshOutputs = useProjectStore((s) => s.refreshNodeOutputs);
  const removeNode = useProjectStore((s) => s.removeNode);
  const insideSubgraph = useProjectStore((s) => s.dashboardId !== null);
  const tracks = useProjectStore((s) => s.tracks);
  const loadProject = useProjectStore((s) => s.loadProject);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const isCandidatesGrid = node.node_type === "asset.select";

  const [fullSizeUrl, setFullSizeUrl] = useState<string | null>(null);

  const openImage = (url: string) => setFullSizeUrl(url);
  const closeImage = () => setFullSizeUrl(null);

  // Starting a compare here just arms Grid-level state (compareFor) -- the
  // second asset comes from clicking a *different* asset node cell anywhere
  // in the project, same click-to-complete gesture the ref gesture uses.
  const startCompare = (asset: Asset) => onStartCompare(node, asset);

  // Deletes only this one node -- never anything else in the track (see
  // nodes.py's delete_node: an asset never cascades to whatever's next
  // positionally). May still promote a sibling spawned-track into the
  // freed-up slot if this asset was itself a verified workflow output with
  // a leftover-picker sibling, which can touch a track beyond this one
  // cell, so a full reload is simpler and safer than trying to hand-patch
  // the store to match.
  const deleteCell = async () => {
    if (!confirm(t("cell.confirmDelete"))) return;
    await nodesApi.remove(node.id);
    const projectId = tracks.find((t) => t.id === node.track_id)?.project_id;
    if (projectId) await loadProject(projectId, useProjectStore.getState().dashboardId);
  };

  // "The source doesn't matter anymore, I just want to keep this result and
  // get rid of the workflow that made it" -- clears created_by_node_id
  // FIRST so delete_node's own_output_nodes sweep (nodes.py) no longer
  // counts this asset as one of that workflow's outputs, then deletes the
  // workflow node itself (which still cascades to any OTHER still-pending
  // result of the same generation the user didn't explicitly keep). Once
  // unbound, this asset is also free to move anywhere -- isPositionAllowedFor/
  // _ensure_output_binding only constrain nodes that still have a creator.
  // Turns this free asset cell into a smart pointer on a brand-new, empty
  // subgraph, and makes it that subgraph's main pointer. The node keeps its
  // identity and position -- only node_type and subgraph_dashboard_id change --
  // so nothing referencing this cell has to be rewired.
  /** Marks this asset as the face of the subgraph we're currently inside.
   *
   * Only offered while standing in a sub-dashboard: the result belongs to the
   * subgraph, and the backend refuses an asset that lives in a different grid.
   * Stored on the dashboard rather than on any one pointer, so every pointer
   * into this subgraph shows the same picture.
   */
  const markAsResult = async () => {
    const dashboardId = useProjectStore.getState().dashboardId;
    if (!dashboardId) return;
    const asset = outputs.find((o) => o.selected) ?? outputs[0];
    if (!asset) return;
    try {
      await dashboardsApi.setResult(dashboardId, asset.id);
      alert(t("subgraph.resultSet"));
    } catch (err) {
      alert(err instanceof Error ? err.message : t("subgraph.resultSetFailed"));
    }
  };

  const makeSubgraph = async () => {
    const name = prompt(t("subgraph.createPrompt"), "");
    if (name === null) return;
    try {
      await dashboardsApi.create(node.id, name);
      const updated = await nodesApi.get(node.id);
      setNode(updated);
    } catch (err) {
      alert(err instanceof Error ? err.message : t("subgraph.deleteFailed"));
    }
  };

  const detachFromWorkflow = async () => {
    const creatorId = node.created_by_node_id;
    if (!creatorId) return;
    if (!confirm(t("cell.confirmDetach"))) return;
    await nodesApi.update(node.id, { created_by_node_id: null });
    await nodesApi.remove(creatorId);
    const projectId = tracks.find((t) => t.id === node.track_id)?.project_id;
    if (projectId) await loadProject(projectId, useProjectStore.getState().dashboardId);
  };

  // Only offered when this asset is unambiguously a workflow's own output
  // (created_by_node_id set) and not already collapsed -- the backend
  // (collapse_node, nodes.py) does the real eligibility check (sole output,
  // sole consumer right after it, not referenced elsewhere) and 409s with a
  // specific reason if it doesn't hold; that's surfaced here rather than
  // trying to duplicate the same graph check client-side just to grey the
  // button out preemptively.
  // Collapsing hides this cell's own wrapper entirely once Grid.tsx's
  // hiddenChainNodeIds picks up the fresh collapse_target_id from this
  // setNode -- the corresponding "expand" trigger lives on the creator
  // workflow node's own card instead (BaseWorkflowNodeView's expandChain),
  // since that's the only cell left once this one disappears.
  const collapseChain = async () => {
    try {
      const updated = await nodesApi.collapse(node.id);
      setNode(updated);
    } catch (err) {
      alert(err instanceof Error ? err.message : t("cell.collapseFailed"));
    }
  };

  const uploadFiles = async (files: File[]) => {
    for (const file of files) {
      await nodesApi.uploadAsset(node.id, file);
    }
    await refreshOutputs(node.id);
    const updated = await nodesApi.get(node.id);
    setNode(updated);
  };

  // Drop straight from the OS file manager -- the pointer equivalent of
  // handlePaste below, gated on the same "this cell is still empty" condition
  // (isFileDrag lives in dragUtils.ts, shared with Grid.tsx's own file-drop
  // targets for empty cells). stopPropagation matters here: the cell's wrapper
  // div in Grid.tsx has its own onDragOver/onDrop for the internal
  // asset-move drag, and without this the two gestures see each other's events.
  const acceptsFileDrop = outputs.length === 0 && !isCandidatesGrid;
  const [fileDragOver, setFileDragOver] = useState(false);
  const handleFileDragOver = (e: React.DragEvent<HTMLDivElement>) => {
    if (!acceptsFileDrop || !isFileDrag(e.dataTransfer)) return;
    e.preventDefault();
    e.stopPropagation();
    setFileDragOver(true);
  };
  const handleFileDrop = (e: React.DragEvent<HTMLDivElement>) => {
    if (!acceptsFileDrop || !isFileDrag(e.dataTransfer)) return;
    e.preventDefault();
    e.stopPropagation();
    setFileDragOver(false);
    const files = Array.from(e.dataTransfer.files);
    if (files.length > 0) uploadFiles(files);
  };

  // Lets an empty asset cell accept Ctrl+V straight from the OS clipboard --
  // no "Upload..." dialog needed for the common case of a screenshot or a
  // copied image sitting in the clipboard already. Only wired up while the
  // cell has no output yet, mirroring when the "Upload..." button itself
  // shows (outputs.length === 0 && !isCandidatesGrid).
  const handlePaste = (e: React.ClipboardEvent<HTMLDivElement>) => {
    if (outputs.length > 0 || isCandidatesGrid) return;
    const files = Array.from(e.clipboardData.items)
      .filter((item) => item.kind === "file" && item.type.startsWith("image/"))
      .map((item) => item.getAsFile())
      .filter((f): f is File => f !== null);
    if (files.length === 0) return;
    e.preventDefault();
    uploadFiles(files);
  };

  // Keep this candidate; the backend forks the rest onto their own line
  // (pick-candidate does all the placement -- settle in place, relocate the
  // leftover picker, spawn the next step). The frontend just names the kept
  // asset and re-fetches the authoritative layout.
  const selectCandidate = async (asset: Asset) => {
    try {
      await nodesApi.pickCandidate(node.id, asset.id);
      const projectId = tracks.find((t) => t.id === node.track_id)?.project_id;
      if (projectId) await loadProject(projectId, useProjectStore.getState().dashboardId);
    } catch (e) {
      alert(e instanceof Error ? e.message : t("cell.keepCandidateFailed"));
    }
  };

  // Discarding down to zero (never selecting anything) empties the picker
  // out entirely -- it disappears on its own instead of leaving an empty
  // cell behind. Only safe to remove automatically when it's still the last
  // cell in its track (see NodeCell's isLastInTrack invariant for manual
  // deletes).
  const discardCandidate = async (asset: Asset) => {
    if (!confirm(t("cell.confirmDiscardImage"))) return;
    await assetsApi.remove(asset.id);
    await refreshOutputs(node.id);
    const remaining = await nodesApi.outputs(node.id).catch(() => outputs);
    // An emptied-out picker is meaningless -- remove it, wherever it sits. The
    // old `&& isLastInTrack` guard existed only because deleting a non-last
    // node used to cascade forward through the track; delete_node no longer
    // does that for an asset (nodes.py), so it's safe at any position now.
    if (remaining.length === 0) {
      await nodesApi.remove(node.id);
      removeNode(node.id);
    }
  };

  const discardAll = async () => {
    if (!confirm(t("cell.confirmDiscardAll", { n: outputs.length }))) return;
    for (const asset of outputs) {
      await assetsApi.remove(asset.id);
    }
    await refreshOutputs(node.id);
    // Emptied out -> remove the picker regardless of position (see discardCandidate).
    await nodesApi.remove(node.id);
    removeNode(node.id);
  };

  // Cascade every candidate into its own line -- one backend call now
  // (pick-all-candidates loops the same fork server-side).
  const selectAll = async () => {
    try {
      await nodesApi.pickAllCandidates(node.id);
      const projectId = tracks.find((t) => t.id === node.track_id)?.project_id;
      if (projectId) await loadProject(projectId, useProjectStore.getState().dashboardId);
    } catch (e) {
      alert(e instanceof Error ? e.message : t("cell.settleFailed"));
    }
  };

  // A collapsed asset (node.collapse_target_id set) never reaches this
  // component at all -- Grid.tsx's hiddenChainNodeIds skips its wrapper div
  // entirely, letting its creator workflow node's own cell span the 3
  // columns the whole chain used to occupy as one card instead. expandChain
  // above still exists for the "not yet collapsed, offer the button" case.

  const cls = cx(
    "node-cell",
    "node-cell-asset",
    `status-${node.status}`,
    compareActive && "picking-target",
    isComparingSource && "picking-source",
    fileDragOver && "file-drag-over",
  );

  return (
    <div
      ref={(el) => registerRef(node.id, el)}
      className={cls}
      tabIndex={outputs.length === 0 && !isCandidatesGrid ? 0 : undefined}
      onPaste={handlePaste}
      onDragOver={handleFileDragOver}
      // Same containment check Grid.tsx's own canvas-level dragleave uses:
      // dragleave also fires for every inner element boundary the pointer
      // crosses, which would flicker the highlight off and on across the cell's
      // own contents.
      onDragLeave={(e) => {
        if (!e.currentTarget.contains(e.relatedTarget as Node | null)) setFileDragOver(false);
      }}
      onDrop={handleFileDrop}
      onClick={() => compareActive && onCellClicked(node)}
    >
      <div className="node-cell-header">
        <span>{isCandidatesGrid ? t("cell.assetSelect") : t("cell.asset")}</span>
        {isManualPlacement && (
          <span title={t("cell.manualPinTitle")} style={{ fontSize: 11, opacity: 0.7 }}>
            📌
          </span>
        )}
        <span className="status-pill">{statusLabel(t, node.status)}</span>
      </div>

      <input
        ref={fileInputRef}
        type="file"
        accept="image/*"
        multiple
        style={{ display: "none" }}
        onChange={(e) => {
          if (e.target.files?.length) uploadFiles(Array.from(e.target.files));
          e.target.value = "";
        }}
      />
      <div className="node-actions" onClick={(e) => e.stopPropagation()}>
        {outputs.length === 1 && !isCandidatesGrid && (
          <a className="primary" style={{ textDecoration: "none", padding: "4px 8px" }} href={resolveAssetUrl(outputs[0].url)} download>
            {t("cell.download")}
          </a>
        )}
        {outputs.length === 0 && (
          <button className="primary" onClick={() => fileInputRef.current?.click()} title={t("cell.uploadTitle")}>
            {t("cell.upload")}
          </button>
        )}
        {isCandidatesGrid && (
          <>
            <button className="primary" onClick={selectAll} title={t("cell.selectAllTitle")}>
              {t("cell.selectAll")}
            </button>
            <button onClick={discardAll} title={t("cell.discardAllTitle")}>
              {t("cell.discardAll")}
            </button>
          </>
        )}
        {!isCandidatesGrid && outputs.length >= 1 && (
          <button onClick={() => onStartRef(node)} title={t("cell.refElsewhereTitle")}>
            {t("cell.refElsewhere")}
          </button>
        )}
        {/* "+ ref elsewhere" is a click-to-complete gesture within one rendered
            grid, so it can't reach into another dashboard -- the target cell
            isn't on screen at the same time. Copying puts the asset id where
            the other grid can pick it up. */}
        {!isCandidatesGrid && outputs.length >= 1 && (
          <button
            onClick={() => {
              const asset = outputs.find((o) => o.selected) ?? outputs[0];
              copyAsset({ assetId: asset.id, label: node.node_type ?? t("cell.asset"), url: asset.url });
            }}
            title={t("cell.copyRefTitle")}
          >
            {t("cell.copyRef")}
          </button>
        )}
        {/* Only on a free cell: a workflow's own materialized output is bound to
            its creator's position, so it can't also be the way into a subgraph
            (the backend rejects it too). */}
        {!isCandidatesGrid && !node.created_by_node_id && !node.subgraph_dashboard_id && (
          <button onClick={makeSubgraph} title={t("subgraph.createTitle")}>
            {t("subgraph.create")}
          </button>
        )}
        {/* Only inside a subgraph -- that's the only place the question "what
            does this subgraph look like from outside" even applies. */}
        {!isCandidatesGrid && outputs.length >= 1 && insideSubgraph && (
          <button onClick={markAsResult} title={t("subgraph.markResultTitle")}>
            {t("subgraph.markResult")}
          </button>
        )}
        {!isCandidatesGrid && node.created_by_node_id && (
          <button
            onClick={detachFromWorkflow}
            title={t("cell.detachTitle")}
          >
            {t("cell.detach")}
          </button>
        )}
        {!isCandidatesGrid && node.created_by_node_id && (
          <button
            onClick={collapseChain}
            title={t("cell.collapseChainTitle")}
          >
            {t("cell.collapseChain")}
          </button>
        )}
        {!isCandidatesGrid && (
          <button onClick={deleteCell} title={t("cell.deleteOnlyTitle")}>
            {t("common.delete")}
          </button>
        )}
      </div>

      {node.error && <div className="error-text">{node.error}</div>}

      {outputs.length === 0 && !isCandidatesGrid && (
        <div style={{ fontSize: 10, color: "var(--text-dim)" }}>{t("cell.dropOrPaste")}</div>
      )}

      {isCandidatesGrid ? (
        <CandidatesGrid outputs={outputs} onSelect={selectCandidate} onDiscard={discardCandidate} onImageOpen={openImage} onCompare={startCompare} />
      ) : outputs.length === 1 ? (
        <AssetFaceView asset={outputs[0]} onImageOpen={openImage} onCompare={startCompare} />
      ) : null}

      {isComparingSource && (
        <div style={{ fontSize: 10, color: "var(--accent)" }}>{t("cell.clickToCompare")}</div>
      )}

      {isRefSource && (
        <div style={{ fontSize: 10, color: "var(--warning)" }}>{t("cell.clickToPlaceRef")}</div>
      )}

      {fullSizeUrl && <FullSizeModal url={fullSizeUrl} onClose={closeImage} />}
    </div>
  );
}

function BaseWorkflowNodeView({ node, templates, backends, capabilities, registerRef, onShrinkToFit, onStartCopy, isCopySource, collapseInfo }: Props) {
  const t = useT();
  const setNode = useProjectStore((s) => s.setNode);
  const tracks = useProjectStore((s) => s.tracks);
  const nodesById = useProjectStore((s) => s.nodesById);
  const outputsByNode = useProjectStore((s) => s.outputsByNode);
  const loadProject = useProjectStore((s) => s.loadProject);
  const reloadTracks = useProjectStore((s) => s.reloadTracks);
  const template = templates.find((t) => t.node_type === node.node_type) ?? null;
  // Native execution always runs in-process (no backend to pick between) and
  // is deterministic (no seed field -- enqueue_node_job forces variants to 1
  // server-side regardless of what's requested), so neither control means
  // anything here.
  const isNative = node.node_type?.startsWith("native.") ?? false;
  const [jobs, setJobs] = useState<Job[]>([]);
  // The face only shows what's needed to glance at status and hit Generate --
  // template name, plan, and other node's worth of pixels (see NodeCell for
  // BaseAssetNodeView's compact size). Everything else (slot sources, param
  // fields, variants, backend) lives behind this modal so the node stays the
  // same footprint whether it has 2 fields or 20.
  const [paramsOpen, setParamsOpen] = useState(false);
  // Which text field currently has the "from ideas" picker open, if any.
  const [ideaPickerField, setIdeaPickerField] = useState<string | null>(null);
  // A node's project is reached through its track, the same way every other
  // handler in this file does it -- the cell isn't handed one directly.
  const projectId = tracks.find((t) => t.id === node.track_id)?.project_id ?? null;
  // Read-only peek at the 3 hidden cells of a collapsed chain (this node,
  // the pass-through asset, the consumer workflow -- see collapseInfo's
  // docstring) -- no editing, no slot resolution, just "what was this".
  const [viewChainOpen, setViewChainOpen] = useState(false);

  const cropGroups = useMemo(() => (template ? detectCropGroups(template.param_schema.fields ?? []) : []), [template]);
  const [cropImages, setCropImages] = useState<Record<string, string | null>>({});

  // A node type whose ComfyUI workflow loads the Multiple-Angles LoRA gets the
  // angle prompt builder under each of its editable text fields (same detection
  // as the Settings prompt editor). Any capability for this slug can carry the
  // workflow, so check them all.
  const isMultiAngle = useMemo(
    () => Boolean(template && capabilities.some((c) => c.node_type_slug === template.node_type_slug && capabilityUsesMultiAngleLora(c))),
    [template, capabilities],
  );

  // An Ideogram 4 node's prompt isn't prose, it's a structured JSON caption
  // (see ideogramCaption.ts), so its text fields get the box editor instead of
  // being edited as raw JSON in a 2-row textarea. Same detection shape as the
  // angle builder above.
  const isIdeogram4 = useMemo(
    () => Boolean(template && capabilities.some((c) => c.node_type_slug === template.node_type_slug && capabilityUsesIdeogram4(c))),
    [template, capabilities],
  );
  const [captionField, setCaptionField] = useState<string | null>(null);

  // Backgrounds to draw the layout over: this node's own generated images and
  // nothing else. A text-to-image node has no input slots, and its outputs
  // don't live on the node itself -- they're a separate asset node bound back
  // to it by created_by_node_id (the same link Grid.tsx's isWorkflowOutput
  // reads), which may hold several undecided candidates.
  const captionBgOptions = useMemo<CaptionBgOption[]>(() => {
    const options: CaptionBgOption[] = [];
    for (const candidate of Object.values(nodesById)) {
      if (candidate.created_by_node_id !== node.id) continue;
      for (const asset of outputsByNode[candidate.id] ?? []) {
        if (asset.kind !== "image" || !asset.url) continue;
        options.push({
          id: asset.id,
          url: resolveAssetUrl(asset.url),
          label: asset.selected ? t("ideogram.bgSelected") : `#${options.length + 1}`,
        });
      }
    }
    // Newest first, so the default background is the most recent generation.
    return options.reverse();
  }, [nodesById, outputsByNode, node.id, t]);

  useEffect(() => {
    if (!paramsOpen || !template || cropGroups.length === 0) {
      setCropImages({});
      return;
    }
    let cancelled = false;
    (async () => {
      const slots = slotFields(template.param_schema);
      const capability = capabilities.find((c) => c.node_type_slug === template.node_type_slug);
      const next: Record<string, string | null> = {};
      for (const group of cropGroups) {
        const fieldName = resolveCropImageField(capability, group, template.param_schema.fields ?? []);
        const slotIndex = fieldName ? slots.findIndex((f) => f.name === fieldName) : -1;
        if (slotIndex < 0) {
          next[group.prefix] = null;
          continue;
        }
        const asset = await resolveSlotAsset(node, slotIndex, tracks, nodesById);
        next[group.prefix] = asset ? resolveAssetUrl(asset.url) : null;
      }
      if (!cancelled) setCropImages(next);
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [paramsOpen, template, node.inputs, cropGroups, capabilities]);

  const maskGroups = useMemo(() => (template ? detectMaskGroups(template.param_schema.fields ?? []) : []), [template]);
  const [maskImages, setMaskImages] = useState<Record<string, string | null>>({});

  useEffect(() => {
    if (!paramsOpen || !template || maskGroups.length === 0) {
      setMaskImages({});
      return;
    }
    let cancelled = false;
    (async () => {
      const slots = slotFields(template.param_schema);
      const next: Record<string, string | null> = {};
      for (const group of maskGroups) {
        const fieldName = resolveMaskImageField(template.param_schema.fields ?? []);
        const slotIndex = fieldName ? slots.findIndex((f) => f.name === fieldName) : -1;
        if (slotIndex < 0) {
          next[group.maskField] = null;
          continue;
        }
        const asset = await resolveSlotAsset(node, slotIndex, tracks, nodesById);
        next[group.maskField] = asset ? resolveAssetUrl(asset.url) : null;
      }
      if (!cancelled) setMaskImages(next);
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [paramsOpen, template, node.inputs, maskGroups]);

  // Always available, not just for the last cell in a track -- unlike an
  // asset cell, a workflow node's own output(s) aren't guaranteed to still
  // be sitting right after it in the same track (_claim_new_output_cell,
  // worker/tasks.py, can grow one into a different track entirely when the
  // home cell was already taken), so "is this the last node in its own
  // track" stopped meaning "does this have nothing depending on it" a while
  // ago. The backend sweeps every one of this node's own outputs
  // (created_by_node_id) wherever they are, not just whatever's positionally
  // after it (see nodes.py's delete_node) -- that can touch tracks well
  // beyond this one cell, so a full reload is simpler and safer than trying
  // to hand-patch the store to match (same reason BaseAssetNodeView's own
  // deleteCell already reloads instead of just removing one id).
  const deleteCell = async () => {
    if (!confirm(t("cell.confirmDeleteWorkflow"))) return;
    await nodesApi.remove(node.id);
    const projectId = tracks.find((t) => t.id === node.track_id)?.project_id;
    if (projectId) await loadProject(projectId, useProjectStore.getState().dashboardId);
  };

  // collapseInfo.assetId is the pass-through asset's own id -- the same one
  // BaseAssetNodeView's own expand button targets (see Grid.tsx's
  // collapseInfoByNode). Offered here too since that asset cell is now a
  // minimized placeholder, not the natural place to look for this action.
  const expandChain = async () => {
    if (!collapseInfo) return;
    try {
      await nodesApi.expand(collapseInfo.assetId);
      const projectId = tracks.find((t) => t.id === node.track_id)?.project_id;
      if (projectId) await loadProject(projectId, useProjectStore.getState().dashboardId);
    } catch (err) {
      alert(err instanceof Error ? err.message : t("cell.expandFailed"));
    }
  };

  useEffect(() => {
    if (node.status !== "running" && node.status !== "queued") return;
    let cancelled = false;
    const tick = async () => {
      const list = await nodesApi.jobs(node.id).catch(() => []);
      if (!cancelled) setJobs(list);
    };
    tick();
    const id = setInterval(tick, 2500);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [node.id, node.status]);

  const chooseTemplate = async (templateId: string) => {
    const chosen = templates.find((t) => t.id === templateId);
    const inputs = defaultInputsForSchema(chosen?.param_schema, node.inputs);
    const params = { ...(chosen?.defaults ?? {}), ...node.params };
    const updated = await nodesApi.update(node.id, { node_type: chosen?.node_type, inputs, params });
    setNode(updated);
    // Assigning a template can make the backend materialize this workflow's
    // input-slot rows (ensure_span_rows) -- re-fetch the track list so those
    // new empty rows actually appear.
    const projectId = tracks.find((t) => t.id === node.track_id)?.project_id;
    if (projectId) await reloadTracks(projectId);
  };

  const updateParam = async (name: string, value: unknown) => {
    const params = { ...node.params, [name]: value };
    setNode({ ...node, params });
    const updated = await nodesApi.update(node.id, { params });
    setNode(updated);
  };

  const setSlotSource = async (slotIndex: number, ref: InputRef) => {
    const inputs = [...node.inputs];
    inputs[slotIndex] = ref;
    const updated = await nodesApi.update(node.id, { inputs });
    setNode(updated);
  };

  const generate = async () => {
    const updated = await nodesApi.generate(node.id);
    setNode(updated);
  };

  const discard = async () => {
    const updated = await nodesApi.discard(node.id);
    setNode(updated);
  };

  const reroll = async () => {
    await nodesApi.reroll(node.id);
    const list = await nodesApi.jobs(node.id).catch(() => []);
    setJobs(list);
  };

  // Layer 4 of the "money" protection stack (see BackendsSection in
  // Settings.tsx for layers 1/3, Node.use_api for layer 2): even with the
  // node's own "Use API" flag on, the last click before a paid call actually
  // fires still has to go through an explicit confirmation.
  const apiCapability = template
    ? capabilities.find((c) => c.node_type_slug === template.node_type_slug && c.execution_type === "api_call" && c.enabled)
    : undefined;
  // Provider/key live on the Backend row now (see db/models.py's
  // Backend.provider/api_key docstring), not on the capability's own config.
  const apiBackend = apiCapability ? backends.find((b) => b.id === apiCapability.backend_id) : undefined;
  const [payConfirmAction, setPayConfirmAction] = useState<null | "generate" | "reroll">(null);

  const requestGenerate = () => {
    if (node.use_api && apiCapability) setPayConfirmAction("generate");
    else generate();
  };
  const requestReroll = () => {
    if (node.use_api && apiCapability) setPayConfirmAction("reroll");
    else reroll();
  };
  const confirmPaidAction = async () => {
    const action = payConfirmAction;
    setPayConfirmAction(null);
    if (action === "generate") await generate();
    else if (action === "reroll") await reroll();
  };

  // Only touches Job rows (pending/running/waiting_for_backend -> cancelled)
  // -- never deletes or discards any already-produced Asset, so a variant
  // that already finished stays exactly as it is when you cancel the rest.
  const cancel = async () => {
    await Promise.all(
      jobs
        .filter((j) => j.status === "pending" || j.status === "running" || j.status === "waiting_for_backend")
        .map((j) => jobsApi.cancel(j.id)),
    );
    const [updatedNode, list] = await Promise.all([nodesApi.get(node.id), nodesApi.jobs(node.id).catch(() => [])]);
    setNode(updatedNode);
    setJobs(list);
  };

  // picking-source is reused as-is (a solid accent outline) -- an armed copy is
  // the same "this cell is the source, now click a target" state a compare or a
  // ref is in, and it reads better as one visual language than a third one.
  const cls = cx("node-cell", `status-${node.status}`, isNative && "node-cell-native", isCopySource && "picking-source");

  // Row-span paradigm: a slot's source is purely positional now (which row,
  // within this node's own span, feeds it -- see cell_index in types/index.ts
  // and Grid.tsx's rowSpanByNode/effectiveRow). The old ref-kind dropdown
  // doesn't apply anymore (its four other ref types are gone entirely now) --
  // where the input comes from is decided by dragging the source asset (or a
  // RefAsset stand-in) to align with this row, not by choosing a ref kind
  // here. This is just the number that says *which* row.
  const slotSourceSelects = template &&
    slotFields(template.param_schema).map((field, slotIndex) => {
      const ref = node.inputs[slotIndex];
      const index = ref?.type === "cell_index" ? ref.index : slotIndex;
      return (
        <div key={field.name} className="slot-row">
          <span>{t("cell.slotCell", { label: field.label ?? field.name })}</span>
          <input
            type="number"
            min={0}
            value={index}
            title={t("cell.slotRowTitle")}
            style={{ width: 44 }}
            onChange={(e) => setSlotSource(slotIndex, { type: "cell_index", index: Math.max(0, Number(e.target.value)) })}
          />
        </div>
      );
    });

  const cropFieldNames = useMemo(
    () => new Set(cropGroups.flatMap((g) => [g.xField, g.yField, g.widthField, g.heightField])),
    [cropGroups],
  );
  const maskFieldNames = useMemo(() => new Set(maskGroups.map((g) => g.maskField)), [maskGroups]);

  const paramFieldInputs = template &&
    (template.param_schema.fields ?? [])
      // "seed" fields are randomized fresh per variant by the backend
      // (resolve_node_inputs in worker/tasks.py) -- nothing for the user to set.
      // Crop-group fields render as one draggable box instead (see cropGroups
      // below) -- 4 raw x/y/width/height numbers are one entity, not 4 params.
      // Mask fields render as a paint canvas instead (see maskGroups below).
      .filter((f) => f.type !== "image" && f.type !== "file" && f.type !== "seed" && !cropFieldNames.has(f.name) && !maskFieldNames.has(f.name))
      .map((field) => (
        <div key={field.name} className="field-row">
          <label>{field.label ?? field.name}</label>
          {field.type === "enum" ? (
            <select value={(node.params[field.name] as string) ?? ""} onChange={(e) => updateParam(field.name, e.target.value)}>
              {(field.options ?? []).map((opt) => (
                <option key={opt} value={opt}>
                  {opt}
                </option>
              ))}
            </select>
          ) : field.type === "text" ? (
            <>
              <textarea
                rows={2}
                value={(node.params[field.name] as string) ?? ""}
                onChange={(e) => updateParam(field.name, e.target.value)}
              />
              {/* Bridge 2 of the idea board: prompt text can come from a
                  sticker, by value (frozen) or by {tag} (resolved at run
                  time, with the preview below showing what that expands to).
                  Scoped to this node instance's own params -- never to the
                  capability's baked workflow_json, which is global and would
                  leak one project's description into every other project. */}
              {projectId && (
                <div className="idea-picker-anchor">
                  <button
                    type="button"
                    className="idea-picker-open"
                    onClick={() => setIdeaPickerField((f) => (f === field.name ? null : field.name))}
                    title={t("cell.fromIdeasTitle")}
                  >
                    {t("cell.fromIdeas")}
                  </button>
                  {ideaPickerField === field.name && (
                    <IdeaTextPicker
                      projectId={projectId}
                      value={(node.params[field.name] as string) ?? ""}
                      onInsert={(next) => updateParam(field.name, next)}
                      onClose={() => setIdeaPickerField(null)}
                    />
                  )}
                  <MacroPreview projectId={projectId} text={(node.params[field.name] as string) ?? ""} />
                </div>
              )}
              {isMultiAngle && (
                <MultiAngleBuilder
                  value={(node.params[field.name] as string) ?? ""}
                  onChange={(v) => updateParam(field.name, v)}
                />
              )}
              {isIdeogram4 && (
                <button type="button" className="caption-open" onClick={() => setCaptionField(field.name)}>
                  {t("ideogram.openEditor")}
                </button>
              )}
            </>
          ) : field.type === "bool" ? (
            <input
              type="checkbox"
              checked={Boolean((node.params[field.name] as boolean | undefined) ?? field.default ?? false)}
              onChange={(e) => updateParam(field.name, e.target.checked)}
            />
          ) : (
            <input
              type="number"
              value={(node.params[field.name] as number) ?? field.default ?? 0}
              min={field.min}
              max={field.max}
              onChange={(e) => updateParam(field.name, Number(e.target.value))}
            />
          )}
        </div>
      ));

  // Optional per-node display label (params.__label) -- a cosmetic reserved
  // key the backend ignores (not in param_schema/param_mapping), used purely to
  // tell two same-type nodes apart on the grid. Replaces the shown name when set.
  const customLabel = ((node.params.__label as string | undefined) ?? "").trim();
  const displayName = customLabel || collapseInfo?.combinedLabel || template?.name || t("cell.chooseTemplate");
  // Locked/history (see collapseInfo) means there's no regeneration left to tune
  // params for, so the modal/gear/hint are suppressed there. Otherwise any
  // workflow node with a template can open the modal -- if only to set a label.
  const canEditParams = !collapseInfo && Boolean(template);

  return (
    <div
      ref={(el) => registerRef(node.id, el)}
      className={cls}
      onDoubleClick={() => canEditParams && setParamsOpen(true)}
      title={canEditParams ? t("cell.editParamsHint") : undefined}
    >
      <div className="node-cell-header">
        <span title={customLabel ? template?.name : undefined}>{displayName}</span>
        {isNative && !collapseInfo && (
          <span className="native-pill" title={t("cell.nativeTitle")}>
            {t("cell.native")}
          </span>
        )}
        {collapseInfo && (
          // Replaces the native pill rather than sitting alongside it -- two
          // badges plus the status pill and 3 icon buttons was overflowing
          // the header row (2026-07-21, reported from a real collapsed cell
          // on prod).
          <span
            className="native-pill"
            title={t("cell.collapsedTitle")}
          >
            {t("cell.collapsed")}
          </span>
        )}
        <span className="status-pill">{statusLabel(t, node.status)}</span>
        {canEditParams && (
          // Double-click still works (desktop), but it's an unreliable gesture
          // on a phone once the face is full of tappable fields -- a plain
          // button is a sure thing on both.
          <button
            onClick={(e) => {
              e.stopPropagation();
              setParamsOpen(true);
            }}
            title={t("cell.editParams")}
          >
            ⚙
          </button>
        )}
        {collapseInfo && (
          <button
            onClick={(e) => {
              e.stopPropagation();
              setViewChainOpen(true);
            }}
            title={t("cell.viewChainTitle")}
          >
            👁
          </button>
        )}
        {!collapseInfo && template && (
          // A real copy, not a reference: the asset world's "+ ref elsewhere"
          // has no workflow equivalent, since two cells can share one picture
          // but not one set of parameters. Needs a template to be worth
          // offering -- there's nothing to carry over from a cell that hasn't
          // picked one yet. Suppressed for a collapsed chain too: this card
          // stands for 3 cells there, and copying just the one workflow node
          // out of it isn't what "copy this card" would mean.
          <button
            onClick={(e) => {
              e.stopPropagation();
              onStartCopy(node);
            }}
            title={t("cell.copyNodeTitle")}
          >
            ⧉
          </button>
        )}
        <button
          onClick={(e) => {
            e.stopPropagation();
            onShrinkToFit(node);
          }}
          title={t("cell.shrinkTitle")}
        >
          ⤢
        </button>
        <button
          onClick={(e) => {
            e.stopPropagation();
            deleteCell();
          }}
          title={t("cell.removeWorkflowTitle")}
        >
          ×
        </button>
      </div>

      {!template && (
        <select onChange={(e) => chooseTemplate(e.target.value)} value="" onClick={(e) => e.stopPropagation()}>
          <option value="" disabled>
            {t("cell.selectNodeType")}
          </option>
          {/* Grouped so native (code-registry, no DB row) and template.<slug>
              (uploaded workflow.json) types are visually distinguishable
              here too, not just on the placed cell. */}
          <optgroup label={t("cell.groupNative")}>
            {templates
              .filter((t) => t.node_type.startsWith("native."))
              .map((t) => (
                <option key={t.id} value={t.id}>
                  ⚡ {t.name}
                </option>
              ))}
          </optgroup>
          <optgroup label={t("cell.groupTemplates")}>
            {templates
              .filter((t) => !t.node_type.startsWith("native."))
              .map((t) => (
                <option key={t.id} value={t.id}>
                  {t.name}
                </option>
              ))}
          </optgroup>
        </select>
      )}

      {template && (
        <div onClick={(e) => e.stopPropagation()} onDoubleClick={(e) => e.stopPropagation()}>
          {slotSourceSelects}

          {!isNative && (
            <div className="field-row">
              <label>{t("cell.variants")}</label>
              <input
                type="number"
                min={1}
                value={node.requested_variants}
                onChange={async (e) => {
                  const updated = await nodesApi.update(node.id, { requested_variants: Number(e.target.value) });
                  setNode(updated);
                }}
              />
            </div>
          )}

          {!isNative && (
            <div className="field-row">
              <label>{t("cell.backend")}</label>
              <select
                value={node.backend_mode}
                onChange={async (e) => {
                  const updated = await nodesApi.update(node.id, { backend_mode: e.target.value as NodeItem["backend_mode"] });
                  setNode(updated);
                }}
              >
                <option value="auto">{t("cell.backendAuto")}</option>
                <option value="comfyui_only">{t("cell.backendComfyOnly")}</option>
                {/* Picking this without "Use API" checked would just never
                    dispatch anywhere -- dispatcher.eligible_capabilities hard-
                    gates every api_call capability on Node.use_api regardless
                    of backend_mode, so there's nothing this option could do
                    yet. Only offered once it could actually work. */}
                {node.use_api && apiCapability && <option value="api_only">{t("cell.backendApiOnly")}</option>}
                <option value="manual">{t("cell.backendManual")}</option>
              </select>
              {node.backend_mode === "manual" && (
                <select
                  value={node.manual_backend_id ?? ""}
                  onChange={async (e) => {
                    const updated = await nodesApi.update(node.id, { manual_backend_id: e.target.value || null });
                    setNode(updated);
                  }}
                >
                  <option value="">{t("cell.chooseBackend")}</option>
                  {backends
                    .filter((b) => b.kind !== "api_provider" || (node.use_api && apiCapability))
                    .map((b) => (
                      <option key={b.id} value={b.id}>
                        {b.name}
                      </option>
                    ))}
                </select>
              )}
            </div>
          )}

          {!isNative && apiCapability && (
            <div className="field-row">
              <label title={t("cell.useApiTitle")}>{t("cell.useApi")}</label>
              <input
                type="checkbox"
                checked={node.use_api}
                onChange={async (e) => {
                  const updated = await nodesApi.update(node.id, { use_api: e.target.checked });
                  setNode(updated);
                }}
              />
            </div>
          )}
        </div>
      )}

      {!collapseInfo && ((paramFieldInputs && paramFieldInputs.length > 0) || cropGroups.length > 0 || maskGroups.length > 0) && (
        <div className="node-cell-hint">{t("cell.moreParams")}</div>
      )}

      {jobs.length > 0 && node.status !== "done" && node.status !== "discarded" && (
        <div style={{ display: "flex", flexDirection: "column", gap: 3 }} onClick={(e) => e.stopPropagation()}>
          {jobs.map((job) => (
            <div key={job.id} className="progress-bar" title={`${job.status} ${job.progress}%`}>
              <div className="progress-bar-fill" style={{ width: `${job.status === "error" ? 100 : job.progress}%`, background: job.status === "error" ? "var(--danger)" : undefined }} />
            </div>
          ))}
        </div>
      )}

      {node.error && <div className="error-text">{node.error}</div>}

      {isCopySource && <div style={{ fontSize: 10, color: "var(--accent)" }}>{t("cell.clickToPlaceCopy")}</div>}

      {template && collapseInfo && (
        <div className="node-actions" onClick={(e) => e.stopPropagation()} onDoubleClick={(e) => e.stopPropagation()}>
          <button onClick={expandChain} title={t("cell.expandTitle")}>
            {t("cell.expand")}
          </button>
        </div>
      )}

      {template && !collapseInfo && (
        <div className="node-actions" onClick={(e) => e.stopPropagation()} onDoubleClick={(e) => e.stopPropagation()}>
          <button className="primary" onClick={requestGenerate} disabled={node.status === "queued" || node.status === "running"}>
            {t("cell.generate")}
          </button>
          {(node.status === "queued" || node.status === "running") && (
            <button onClick={cancel} title={t("cell.cancelRunTitle")}>
              {t("common.cancel")}
            </button>
          )}
          {(node.status === "done" || node.status === "error") && (
            <button onClick={requestReroll} title={t("cell.rerollTitle")}>
              {t("cell.reroll")}
            </button>
          )}
          <button onClick={discard} disabled={node.status === "discarded"}>
            {t("cell.discard")}
          </button>
        </div>
      )}

      {payConfirmAction &&
        createPortal(
          <div className="image-modal-backdrop" onClick={() => setPayConfirmAction(null)}>
            <div className="image-modal-content" onClick={(e) => e.stopPropagation()} style={{ maxWidth: 380 }}>
              <div className="node-cell-header">
                <span>{t("cell.paidTitle")}</span>
              </div>
              <p style={{ fontSize: 13 }}>
                {t("cell.paidBodyPrefix")}
                <strong>{apiBackend?.name ?? apiBackend?.provider ?? t("cell.paidApiFallback")}</strong>
                {apiCapability?.config.model_id ? ` (${apiCapability.config.model_id as string})` : ""}
                {t("cell.paidBodySuffix")}
              </p>
              <div className="node-actions">
                <button className="primary" onClick={confirmPaidAction}>
                  {t("cell.confirmPaid")}
                </button>
                <button onClick={() => setPayConfirmAction(null)}>{t("common.cancel")}</button>
              </div>
            </div>
          </div>,
          document.body,
        )}

      {paramsOpen &&
        template &&
        createPortal(
          // Portal to document.body -- this cell sits inside Grid's pan/zoom
          // transform (Grid.tsx), which establishes a new containing block for
          // any `position: fixed` descendant per the CSS transform spec.
          // Rendered in place (as this used to be), the backdrop's "fixed"
          // stops being viewport-relative and the modal opens hundreds of
          // pixels off-screen -- surfaced by MaskPreview's paint canvas, which
          // is tall enough to actually need the params-modal-content scroll
          // area, but it silently applied to CropPreview too. The other three
          // image-modal-backdrop usages in this file already portal for the
          // same reason.
          <div className="image-modal-backdrop" onClick={(e) => { e.stopPropagation(); setParamsOpen(false); }}>
            <div className="params-modal-content" onClick={(e) => e.stopPropagation()}>
              <button type="button" className="image-modal-close" onClick={() => setParamsOpen(false)} title={t("common.close")}>
                ×
              </button>
              <div className="node-cell-header">
                <span>{template.name}</span>
                <span className="status-pill">{statusLabel(t, node.status)}</span>
              </div>

              <div className="field-row">
                <label>
                  {t("cell.label")} <span style={{ color: "var(--text-dim)" }}>{t("cell.labelHint")}</span>
                </label>
                <input
                  type="text"
                  value={(node.params.__label as string | undefined) ?? ""}
                  placeholder={template.name}
                  onChange={(e) => updateParam("__label", e.target.value)}
                />
              </div>

              {cropGroups.map((group) => {
                const box: CropBox = {
                  x: Number(node.params[group.xField] ?? 0),
                  y: Number(node.params[group.yField] ?? 0),
                  width: Number(node.params[group.widthField] ?? 0),
                  height: Number(node.params[group.heightField] ?? 0),
                };
                const imageUrl = cropImages[group.prefix];
                return (
                  <div key={group.prefix} className="field-row">
                    <label>{group.prefix.charAt(0).toUpperCase() + group.prefix.slice(1)}</label>
                    {imageUrl ? (
                      <CropPreview
                        imageUrl={imageUrl}
                        box={box}
                        onCommit={async (next) => {
                          const updated = await nodesApi.update(node.id, {
                            params: {
                              ...node.params,
                              [group.xField]: Math.round(next.x),
                              [group.yField]: Math.round(next.y),
                              [group.widthField]: Math.round(next.width),
                              [group.heightField]: Math.round(next.height),
                            },
                          });
                          setNode(updated);
                        }}
                      />
                    ) : (
                      <div style={{ fontSize: 11, color: "var(--text-dim)" }}>
                        {t("cell.noCropSource", { x: box.x, y: box.y, w: box.width, h: box.height })}
                      </div>
                    )}
                  </div>
                );
              })}

              {maskGroups.map((group) => {
                const imageUrl = maskImages[group.maskField];
                return (
                  <div key={group.maskField} className="field-row">
                    <label>{group.label}</label>
                    {imageUrl ? (
                      <MaskPreview
                        imageUrl={imageUrl}
                        maskPng={(node.params[group.maskField] as string) ?? null}
                        onCommit={(maskPng) => updateParam(group.maskField, maskPng)}
                      />
                    ) : (
                      <div style={{ fontSize: 11, color: "var(--text-dim)" }}>{t("cell.noMaskSource")}</div>
                    )}
                  </div>
                );
              })}

              {paramFieldInputs}
            </div>
          </div>,
          document.body,
        )}

      {/* Portals itself, so it renders as a sibling of the params modal rather
          than inside it -- the layout editor is full-screen and shouldn't be
          clipped by the modal's own scroll area. */}
      {captionField && (
        <CaptionBoxEditor
          value={(node.params[captionField] as string) ?? ""}
          aspectHint={node.params["aspect_ratio"]}
          bgOptions={captionBgOptions}
          onCommit={(next) => updateParam(captionField, next)}
          onClose={() => setCaptionField(null)}
        />
      )}

      {viewChainOpen &&
        collapseInfo &&
        createPortal(
          <div className="image-modal-backdrop" onClick={() => setViewChainOpen(false)}>
            <div className="params-modal-content" onClick={(e) => e.stopPropagation()}>
              <button type="button" className="image-modal-close" onClick={() => setViewChainOpen(false)} title={t("common.close")}>
                ×
              </button>
              <div className="node-cell-header">
                <span>{collapseInfo.combinedLabel}</span>
              </div>
              {[node, nodesById[collapseInfo.assetId], nodesById[collapseInfo.consumerId]].map((n, i) =>
                n ? <ChainMemberView key={n.id} node={n} templates={templates} outputs={outputsByNode[n.id] ?? []} /> : <div key={i} />,
              )}
            </div>
          </div>,
          document.body,
        )}
    </div>
  );
}

// Read-only summary of one node inside a collapsed chain's "view chain"
// popup (BaseWorkflowNodeView above) -- no slot resolution, no editing, no
// generate/reroll -- just enough to see what this cell used to be.
function ChainMemberView({ node, templates, outputs }: { node: NodeItem; templates: NodeTemplate[]; outputs: Asset[] }) {
  const t = useT();
  // `tpl`, not `t`: the translate function owns that name in this scope now.
  const template = templates.find((tpl) => tpl.node_type === node.node_type);
  return (
    <div className="field-row" style={{ flexDirection: "column", alignItems: "flex-start", gap: 4 }}>
      <label>
        {node.kind === "asset" ? t("cell.asset") : (template?.name ?? node.node_type ?? "?")} <span className="status-pill">{statusLabel(t, node.status)}</span>
      </label>
      {node.kind === "asset" ? (
        outputs[0] ? (
          <img
            src={resolveAssetUrl(outputs[0].url)}
            alt=""
            loading="lazy"
            decoding="async"
            style={{ maxWidth: 200, borderRadius: 4 }}
          />
        ) : (
          <span style={{ fontSize: 11, color: "var(--text-dim)" }}>{t("cell.chainNoOutput")}</span>
        )
      ) : Object.keys(node.params).length > 0 ? (
        <pre style={{ fontSize: 11, whiteSpace: "pre-wrap", margin: 0 }}>{JSON.stringify(node.params, null, 2)}</pre>
      ) : (
        <span style={{ fontSize: 11, color: "var(--text-dim)" }}>{t("cell.chainNoParams")}</span>
      )}
    </div>
  );
}

// A RefAsset is a lightweight pointer, not a real asset: kind="asset",
// node_type="asset.refasset", inputs=[{type:"explicit", node_id, output_id}]
// pointing at the real node/output it stands in for (see Grid.tsx's
// createRefAssetAt). It owns no Asset row itself -- it borrows the real one
// for display, which is RefAssetNode.face's job in assetNodes.ts, same as for
// every other asset kind. This is the ONE case position-based layout can't
// express (the real asset is already spoken for elsewhere), so it's also the
// only node left that gets a drawn arrow (Grid.tsx's edges memo, kind "ref").
function RefAssetNodeView({ node, registerRef, compareActive, onCellClicked, onStartCompare }: Props) {
  const t = useT();
  const removeNode = useProjectStore((s) => s.removeNode);
  const resolved = useAssetFace(node);
  const [fullSizeUrl, setFullSizeUrl] = useState<string | null>(null);

  const deleteCell = async () => {
    if (!confirm(t("cell.confirmRemoveRef"))) return;
    await nodesApi.remove(node.id);
    removeNode(node.id);
  };

  return (
    <div
      ref={(el) => registerRef(node.id, el)}
      className={cx("node-cell", "node-cell-asset", "node-cell-ref", `status-${node.status}`, compareActive && "picking-target")}
      onClick={() => compareActive && onCellClicked(node)}
    >
      <div className="node-cell-header">
        <span>{t("cell.reference")}</span>
        <span className="status-pill">{t("cell.refPill")}</span>
      </div>

      {resolved && (
        <AssetFaceView asset={resolved} borrowed onImageOpen={setFullSizeUrl} onCompare={(asset) => onStartCompare(node, asset)} />
      )}

      <div className="node-actions">
        <button onClick={deleteCell} title={t("cell.removeRefTitle")}>
          {t("common.delete")}
        </button>
      </div>

      {fullSizeUrl && <FullSizeModal url={fullSizeUrl} onClose={() => setFullSizeUrl(null)} />}
    </div>
  );
}

/** A smart pointer: an asset cell that also opens a sub-dashboard.
 *
 * Its face is whichever asset the sub-dashboard was given as its result
 * (Dashboard.result_asset_id) -- resolved through the same useAssetFace hook
 * every other asset kind uses, so it renders as a normal asset (zoom, compare,
 * drag) with one extra affordance: dive in. It used to render a hand-rolled
 * <img> off `result_asset_url` and be excluded from Grid's isPickable, so it
 * showed a picture that nothing else in the app would accept as one.
 *
 * Deletion is guarded server-side, not here: the main pointer of a subgraph
 * that still holds work is refused (409), and the alert surfaces that reason.
 * That rule is what keeps a whole chart from being one click from gone.
 */
function SubgraphNodeView({ node, registerRef, compareActive, onCellClicked, onStartCompare }: Props) {
  const t = useT();
  const removeNode = useProjectStore((s) => s.removeNode);
  const enterDashboard = useProjectStore((s) => s.enterDashboard);
  // The dashboard itself, for the name/counts/ownership row below only -- the
  // picture comes from the kind's own face resolution, not from this payload.
  const [info, setInfo] = useState<Dashboard | null>(null);
  const [fullSizeUrl, setFullSizeUrl] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const face = useAssetFace(node);

  useEffect(() => {
    if (!node.subgraph_dashboard_id) return;
    let cancelled = false;
    dashboardsApi
      .get(node.subgraph_dashboard_id)
      .then((d) => !cancelled && setInfo(d))
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [node.subgraph_dashboard_id]);

  const open = () => {
    if (node.subgraph_dashboard_id) void enterDashboard(node.subgraph_dashboard_id, info?.name || t("subgraph.untitled"));
  };

  const takeOwnership = async () => {
    if (!node.subgraph_dashboard_id || busy) return;
    setBusy(true);
    try {
      setInfo(await dashboardsApi.transferOwnership(node.subgraph_dashboard_id, node.id));
    } catch (err) {
      alert(err instanceof Error ? err.message : t("subgraph.takeOwnershipFailed"));
    } finally {
      setBusy(false);
    }
  };

  const rename = async () => {
    if (!node.subgraph_dashboard_id) return;
    const name = prompt(t("subgraph.renamePrompt"), info?.name ?? "");
    if (name === null) return;
    setInfo(await dashboardsApi.rename(node.subgraph_dashboard_id, name));
  };

  const deleteCell = async () => {
    if (busy) return;
    if (!confirm(t("cell.confirmRemoveSubgraph"))) return;
    setBusy(true);
    try {
      await nodesApi.remove(node.id);
      removeNode(node.id);
    } catch (err) {
      alert(err instanceof Error ? err.message : t("subgraph.deleteFailed"));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div
      ref={(el) => registerRef(node.id, el)}
      className={cx("node-cell", "node-cell-asset", "node-cell-subgraph", `status-${node.status}`, compareActive && "picking-target")}
      onClick={() => compareActive && onCellClicked(node)}
    >
      <div className="node-cell-header">
        <span onDoubleClick={rename} title={t("subgraph.renameTitle")}>
          {info?.name || t("subgraph.untitled")}
        </span>
        <span className="status-pill">{t("subgraph.pill")}</span>
      </div>

      {face ? (
        <AssetFaceView asset={face} onImageOpen={setFullSizeUrl} onCompare={(asset) => onStartCompare(node, asset)} />
      ) : (
        <div className="subgraph-empty">{t("subgraph.noFace")}</div>
      )}

      {info && (
        <div className="subgraph-meta">
          {t("subgraph.contents", { nodes: info.node_count })}
          {info.pointer_count > 1 && ` · ${t("subgraph.pointers", { count: info.pointer_count })}`}
        </div>
      )}

      <div className="node-actions">
        <button className="primary" onClick={open} title={t("subgraph.openTitle")}>
          {t("subgraph.open")}
        </button>
        {/* Only meaningful on an extra pointer: the owner is what guarantees the
            subgraph stays reachable, so moving that role here is how you make
            *this* the way in. Refused server-side if this pointer is only
            reachable through the very dashboard it would then own. */}
        {info && info.owner_node_id !== node.id && (
          <button onClick={takeOwnership} disabled={busy} title={t("subgraph.takeOwnershipTitle")}>
            {t("subgraph.takeOwnership")}
          </button>
        )}
        <button onClick={deleteCell} disabled={busy} title={t("subgraph.deleteTitle")}>
          {t("common.delete")}
        </button>
      </div>

      {fullSizeUrl && <FullSizeModal url={fullSizeUrl} onClose={() => setFullSizeUrl(null)} />}
    </div>
  );
}

export type NodeViewComponent = ComponentType<Props>;

// Per-node_type override, checked before falling back to the base view for
// this node's `kind`. A future node type that needs genuinely different
// rendering gets ONE entry here pointing at its own component, instead of
// threading another `if` through BaseWorkflowNodeView/BaseAssetNodeView --
// which are shared by every other node type and shouldn't have to change (or
// risk regressing) to accommodate one outlier.
export const NODE_VIEWS: Record<string, NodeViewComponent> = {
  "asset.refasset": RefAssetNodeView,
  "asset.subgraph": SubgraphNodeView,
};

export function NodeCell(props: Props) {
  const override = props.node.node_type ? NODE_VIEWS[props.node.node_type] : undefined;
  if (override) {
    const View = override;
    return <View {...props} />;
  }
  return props.node.kind === "asset" ? <BaseAssetNodeView {...props} /> : <BaseWorkflowNodeView {...props} />;
}
