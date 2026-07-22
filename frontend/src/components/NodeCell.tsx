import { useEffect, useMemo, useRef, useState, type ComponentType } from "react";
import { createPortal } from "react-dom";
import { resolveAssetUrl } from "../api/client";
import { assetsApi, jobsApi, nodesApi } from "../api/endpoints";
import { detectCropGroups, resolveCropImageField } from "../cropUtils";
import { detectMaskGroups, resolveMaskImageField } from "../maskUtils";
import { resolveSlotAsset } from "../slotResolution";
import { useProjectStore } from "../state/projectStore";
import { defaultInputsForSchema, slotFields } from "../templateUtils";
import type { Asset, Backend, Capability, InputRef, Job, NodeItem, NodeTemplate } from "../types";
import { cx } from "../utils";
import { CropPreview, type CropBox } from "./CropPreview";
import { MaskPreview } from "./MaskPreview";
import { Model3DThumb } from "./Model3DThumb";
import { ZoomableImage } from "./ZoomableImage";

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
  isLastInTrack: boolean;
  // True for an asset node sitting inside a spanning workflow node's row
  // range that ISN'T that workflow's actual materialized output (Grid.tsx's
  // isWorkflowOutput) -- e.g. one manually loaded via Change 3's "+ asset" in
  // an otherwise-empty spanned row. Drives the "not this workflow's output"
  // badge in BaseAssetNodeView.
  isManualPlacement: boolean;
  // Armed via this node's own "+ ref elsewhere" button -- Grid.tsx is waiting
  // for a click on an empty cell to complete the RefAsset placement.
  isRefSource: boolean;
  registerRef: (nodeId: string, el: HTMLDivElement | null) => void;
  onCellClicked: (node: NodeItem) => void;
  onSelectCandidate: (node: NodeItem, kept: Asset, others: Asset[]) => Promise<NodeItem | undefined>;
  onStartCompare: (node: NodeItem, asset: Asset) => void;
  onStartRef: (node: NodeItem) => void;
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
                onDoubleClick={() => onImageOpen(resolveAssetUrl(asset.url))}
                onLoad={(e) => {
                  const img = e.currentTarget;
                  if (!img) return;
                  setDimsById((d) => ({ ...d, [asset.id]: { w: img.naturalWidth, h: img.naturalHeight } }));
                }}
                title="Double-click to open full size"
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
                title="Open full size"
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
                title="Compare with another asset node"
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
              title="Keep this one here -- moves the other candidates to a new row"
            >
              select ★
            </button>
            <button
              style={{ fontSize: 10, padding: "1px 4px" }}
              onClick={(e) => {
                e.stopPropagation();
                onDiscard(asset);
              }}
              title="Reject this candidate"
            >
              discard ✕
            </button>
          </div>
        </div>
      ))}
    </div>
  );
}

function SingleOutput({ asset, onImageOpen, onCompare }: { asset: Asset; onImageOpen: (url: string) => void; onCompare: (asset: Asset) => void }) {
  const [dims, setDims] = useState<{ w: number; h: number } | undefined>(undefined);
  return (
    <div className="output-grid">
      <div className="output-item">
        {asset.kind === "mesh" ? (
          <Model3DThumb url={resolveAssetUrl(asset.url)} />
        ) : (
          <div className="output-thumb">
            <img
              src={resolveAssetUrl(asset.url)}
              alt="output"
              draggable={false}
              onDoubleClick={() => onImageOpen(resolveAssetUrl(asset.url))}
              onLoad={(e) => {
                const img = e.currentTarget;
                if (!img) return;
                setDims({ w: img.naturalWidth, h: img.naturalHeight });
              }}
              title="Double-click to open full size"
              style={{ cursor: "zoom-in" }}
            />
            <button
              type="button"
              className="zoom-button"
              onClick={(e) => {
                e.stopPropagation();
                onImageOpen(resolveAssetUrl(asset.url));
              }}
              title="Open full size"
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
              title="Compare with another asset node"
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

function BaseAssetNodeView({
  node,
  outputs,
  compareActive,
  isComparingSource,
  isLastInTrack,
  isManualPlacement,
  isRefSource,
  registerRef,
  onCellClicked,
  onSelectCandidate,
  onStartCompare,
  onStartRef,
}: Props) {
  const setNode = useProjectStore((s) => s.setNode);
  const refreshOutputs = useProjectStore((s) => s.refreshNodeOutputs);
  const removeNode = useProjectStore((s) => s.removeNode);
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
    if (!confirm("Delete this cell? This can't be undone.")) return;
    await nodesApi.remove(node.id);
    const projectId = tracks.find((t) => t.id === node.track_id)?.project_id;
    if (projectId) await loadProject(projectId);
  };

  // "The source doesn't matter anymore, I just want to keep this result and
  // get rid of the workflow that made it" -- clears created_by_node_id
  // FIRST so delete_node's own_output_nodes sweep (nodes.py) no longer
  // counts this asset as one of that workflow's outputs, then deletes the
  // workflow node itself (which still cascades to any OTHER still-pending
  // result of the same generation the user didn't explicitly keep). Once
  // unbound, this asset is also free to move anywhere -- isPositionAllowedFor/
  // _ensure_output_binding only constrain nodes that still have a creator.
  const detachFromWorkflow = async () => {
    const creatorId = node.created_by_node_id;
    if (!creatorId) return;
    if (
      !confirm(
        "Detach this asset from its workflow? The workflow node (and any other still-pending result of the same generation) will be deleted -- this asset stays. Can't be undone.",
      )
    )
      return;
    await nodesApi.update(node.id, { created_by_node_id: null });
    await nodesApi.remove(creatorId);
    const projectId = tracks.find((t) => t.id === node.track_id)?.project_id;
    if (projectId) await loadProject(projectId);
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
      alert(err instanceof Error ? err.message : "Couldn't collapse this chain.");
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

  // Selecting settles this same cell in place (is_picker -> false, showing
  // the kept image) and, if there are leftover candidates, spawns a *new*
  // track/row below for them -- see Grid.tsx's onSelectCandidate. This node
  // itself is never removed by selecting.
  const selectCandidate = async (asset: Asset) => {
    const others = outputs.filter((a) => a.id !== asset.id);
    await onSelectCandidate(node, asset, others);
    await refreshOutputs(node.id);
  };

  // Discarding down to zero (never selecting anything) empties the picker
  // out entirely -- it disappears on its own instead of leaving an empty
  // cell behind. Only safe to remove automatically when it's still the last
  // cell in its track (see NodeCell's isLastInTrack invariant for manual
  // deletes).
  const discardCandidate = async (asset: Asset) => {
    if (!confirm("Discard this image? This can't be undone.")) return;
    await assetsApi.remove(asset.id);
    await refreshOutputs(node.id);
    const remaining = await nodesApi.outputs(node.id).catch(() => outputs);
    if (remaining.length === 0 && isLastInTrack) {
      await nodesApi.remove(node.id);
      removeNode(node.id);
    }
  };

  const discardAll = async () => {
    if (!confirm(`Discard all ${outputs.length} remaining images? This can't be undone.`)) return;
    for (const asset of outputs) {
      await assetsApi.remove(asset.id);
    }
    await refreshOutputs(node.id);
    if (isLastInTrack) {
      await nodesApi.remove(node.id);
      removeNode(node.id);
    }
  };

  // "Select" only ever settles *one* candidate at a time (the rest move to a
  // single leftover picker) -- keeping every candidate as its own line means
  // repeating that: settle the first, then settle the first of whatever
  // picker it just pushed the rest into, and so on until nothing's left.
  const selectAll = async () => {
    let currentNode = node;
    let remaining = outputs;
    while (remaining.length > 0) {
      const [first, ...rest] = remaining;
      const nextPicker = await onSelectCandidate(currentNode, first, rest);
      if (!nextPicker) break;
      currentNode = nextPicker;
      remaining = await nodesApi.outputs(nextPicker.id).catch(() => []);
    }
    await refreshOutputs(node.id);
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
  );

  return (
    <div
      ref={(el) => registerRef(node.id, el)}
      className={cls}
      tabIndex={outputs.length === 0 && !isCandidatesGrid ? 0 : undefined}
      onPaste={handlePaste}
      onClick={() => compareActive && onCellClicked(node)}
    >
      <div className="node-cell-header">
        <span>{isCandidatesGrid ? "Asset Select" : "Asset"}</span>
        {isManualPlacement && (
          <span title="Manually placed -- not this workflow's output" style={{ fontSize: 11, opacity: 0.7 }}>
            📌
          </span>
        )}
        <span className="status-pill">{node.status}</span>
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
            Download
          </a>
        )}
        {outputs.length === 0 && (
          <button className="primary" onClick={() => fileInputRef.current?.click()} title="Or click this cell and press Ctrl+V to paste an image from the clipboard">
            Upload…
          </button>
        )}
        {isCandidatesGrid && (
          <>
            <button className="primary" onClick={selectAll} title="Keep every remaining candidate, each as its own new track">
              Select all
            </button>
            <button onClick={discardAll} title="Reject every remaining candidate">
              Discard all
            </button>
          </>
        )}
        {!isCandidatesGrid && outputs.length >= 1 && (
          <button onClick={() => onStartRef(node)} title="Place a reference to this asset in another empty cell">
            + ref elsewhere
          </button>
        )}
        {!isCandidatesGrid && node.created_by_node_id && (
          <button
            onClick={detachFromWorkflow}
            title="Unbind this asset from its creator workflow, then delete that workflow -- this asset stays, and is then free to move anywhere"
          >
            detach ✂
          </button>
        )}
        {!isCandidatesGrid && node.created_by_node_id && (
          <button
            onClick={collapseChain}
            title="Fold this asset and its creator/consumer workflow nodes into one card, and lock them from regenerating -- for a finished chain (e.g. crop then upscale) you just want the lineage of, not the intermediate steps"
          >
            ⛶ collapse chain
          </button>
        )}
        {!isCandidatesGrid && (
          <button onClick={deleteCell} title="Delete this cell only">
            Delete
          </button>
        )}
      </div>

      {node.error && <div className="error-text">{node.error}</div>}

      {outputs.length === 0 && !isCandidatesGrid && (
        <div style={{ fontSize: 10, color: "var(--text-dim)" }}>click, then Ctrl+V to paste an image</div>
      )}

      {isCandidatesGrid ? (
        <CandidatesGrid outputs={outputs} onSelect={selectCandidate} onDiscard={discardCandidate} onImageOpen={openImage} onCompare={startCompare} />
      ) : outputs.length === 1 ? (
        <SingleOutput asset={outputs[0]} onImageOpen={openImage} onCompare={startCompare} />
      ) : null}

      {isComparingSource && (
        <div style={{ fontSize: 10, color: "var(--accent)" }}>click another asset node to compare with…</div>
      )}

      {isRefSource && (
        <div style={{ fontSize: 10, color: "var(--warning)" }}>click an empty cell to place the reference…</div>
      )}

      {fullSizeUrl &&
        createPortal(
          // Portalled out of this cell's DOM subtree -- the cell wrapper
          // (Grid.tsx) is HTML5 draggable={true}, and `draggable` on an
          // ancestor claims the whole subtree as its native drag source
          // regardless of position:fixed/z-index, so a pan gesture started
          // inside this modal was being hijacked into a native image-drag
          // (the ghost/duplicate you see mid-drag) instead of reaching
          // ZoomableImage's own pointer handling. asset.select cells aren't
          // draggable, which is why that modal never had this problem.
          <div className="image-modal-backdrop" onClick={closeImage}>
            <div className="image-modal-content" onClick={(e) => e.stopPropagation()}>
              <button type="button" className="image-modal-close" onClick={closeImage} title="Close full-size image">
                ×
              </button>
              <ZoomableImage src={fullSizeUrl} />
            </div>
          </div>,
          document.body,
        )}
    </div>
  );
}

function BaseWorkflowNodeView({ node, templates, backends, capabilities, registerRef, onShrinkToFit, collapseInfo }: Props) {
  const setNode = useProjectStore((s) => s.setNode);
  const tracks = useProjectStore((s) => s.tracks);
  const nodesById = useProjectStore((s) => s.nodesById);
  const outputsByNode = useProjectStore((s) => s.outputsByNode);
  const refreshNodeOutputs = useProjectStore((s) => s.refreshNodeOutputs);
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
  // Read-only peek at the 3 hidden cells of a collapsed chain (this node,
  // the pass-through asset, the consumer workflow -- see collapseInfo's
  // docstring) -- no editing, no slot resolution, just "what was this".
  const [viewChainOpen, setViewChainOpen] = useState(false);

  const cropGroups = useMemo(() => (template ? detectCropGroups(template.param_schema.fields ?? []) : []), [template]);
  const [cropImages, setCropImages] = useState<Record<string, string | null>>({});

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
        const asset = await resolveSlotAsset(node, slotIndex, tracks, nodesById, outputsByNode, refreshNodeOutputs);
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
        const asset = await resolveSlotAsset(node, slotIndex, tracks, nodesById, outputsByNode, refreshNodeOutputs);
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
    if (!confirm("Delete this workflow cell and all of its own outputs? This can't be undone.")) return;
    await nodesApi.remove(node.id);
    const projectId = tracks.find((t) => t.id === node.track_id)?.project_id;
    if (projectId) await loadProject(projectId);
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
      if (projectId) await loadProject(projectId);
    } catch (err) {
      alert(err instanceof Error ? err.message : "Couldn't expand this chain.");
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

  const cls = cx("node-cell", `status-${node.status}`, isNative && "node-cell-native");

  // Row-span paradigm: a slot's source is purely positional now (which row,
  // within this node's own span, feeds it -- see cell_index in types/index.ts
  // and Grid.tsx's rowSpanByNode/effectiveRow). The old self_prev/
  // track_below_prev/upload/"pick cell…" dropdown doesn't apply anymore --
  // where the input comes from is decided by dragging the source asset (or a
  // RefAsset stand-in) to align with this row, not by choosing a ref kind
  // here. This is just the number that says *which* row.
  const slotSourceSelects = template &&
    slotFields(template.param_schema).map((field, slotIndex) => {
      const ref = node.inputs[slotIndex];
      const index = ref?.type === "cell_index" ? ref.index : slotIndex;
      return (
        <div key={field.name} className="slot-row">
          <span>{field.label ?? field.name}: cell</span>
          <input
            type="number"
            min={0}
            value={index}
            title="Row offset within this node's span (0 = its own row)"
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
            <textarea
              rows={2}
              value={(node.params[field.name] as string) ?? ""}
              onChange={(e) => updateParam(field.name, e.target.value)}
            />
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

  // Locked/history (see collapseInfo) means there's no regeneration left to
  // tune params for, so the params modal/gear/hint are all just noise here --
  // suppressed outright rather than shown-but-pointless.
  const hasExtraParams =
    !collapseInfo && Boolean((paramFieldInputs && paramFieldInputs.length > 0) || cropGroups.length > 0 || maskGroups.length > 0);

  return (
    <div
      ref={(el) => registerRef(node.id, el)}
      className={cls}
      onDoubleClick={() => hasExtraParams && setParamsOpen(true)}
      title={hasExtraParams ? "⚙ or double-click to edit parameters" : undefined}
    >
      <div className="node-cell-header">
        <span>{collapseInfo?.combinedLabel ?? template?.name ?? "(choose template)"}</span>
        {isNative && !collapseInfo && (
          <span className="native-pill" title="Native node type: built into the backend code, not a DB-stored workflow.json template">
            native
          </span>
        )}
        {collapseInfo && (
          // Replaces the native pill rather than sitting alongside it -- two
          // badges plus the status pill and 3 icon buttons was overflowing
          // the header row (2026-07-21, reported from a real collapsed cell
          // on prod).
          <span
            className="native-pill"
            title="Part of a collapsed chain -- generate/re-roll/discard are locked; expand to work on these cells individually again"
          >
            🔗 collapsed
          </span>
        )}
        <span className="status-pill">{node.status}</span>
        {hasExtraParams && (
          // Double-click still works (desktop), but it's an unreliable gesture
          // on a phone once the face is full of tappable fields -- a plain
          // button is a sure thing on both.
          <button
            onClick={(e) => {
              e.stopPropagation();
              setParamsOpen(true);
            }}
            title="Edit parameters"
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
            title="See the 3 hidden cells this card folds together, read-only"
          >
            👁
          </button>
        )}
        <button
          onClick={(e) => {
            e.stopPropagation();
            onShrinkToFit(node);
          }}
          title="Recompute this card's height and remove any empty extra rows it doesn't actually need"
        >
          ⤢
        </button>
        <button
          onClick={(e) => {
            e.stopPropagation();
            deleteCell();
          }}
          title="Remove this workflow cell and all of its own outputs"
        >
          ×
        </button>
      </div>

      {!template && (
        <select onChange={(e) => chooseTemplate(e.target.value)} value="" onClick={(e) => e.stopPropagation()}>
          <option value="" disabled>
            Select node type…
          </option>
          {/* Grouped so native (code-registry, no DB row) and template.<slug>
              (uploaded workflow.json) types are visually distinguishable
              here too, not just on the placed cell. */}
          <optgroup label="Native">
            {templates
              .filter((t) => t.node_type.startsWith("native."))
              .map((t) => (
                <option key={t.id} value={t.id}>
                  ⚡ {t.name}
                </option>
              ))}
          </optgroup>
          <optgroup label="Templates">
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
              <label>Variants</label>
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
              <label>Backend</label>
              <select
                value={node.backend_mode}
                onChange={async (e) => {
                  const updated = await nodesApi.update(node.id, { backend_mode: e.target.value as NodeItem["backend_mode"] });
                  setNode(updated);
                }}
              >
                <option value="auto">Auto (balance)</option>
                <option value="comfyui_only">ComfyUI only</option>
                {/* Picking this without "Use API" checked would just never
                    dispatch anywhere -- dispatcher.eligible_capabilities hard-
                    gates every api_call capability on Node.use_api regardless
                    of backend_mode, so there's nothing this option could do
                    yet. Only offered once it could actually work. */}
                {node.use_api && apiCapability && <option value="api_only">API only</option>}
                <option value="manual">Manual…</option>
              </select>
              {node.backend_mode === "manual" && (
                <select
                  value={node.manual_backend_id ?? ""}
                  onChange={async (e) => {
                    const updated = await nodesApi.update(node.id, { manual_backend_id: e.target.value || null });
                    setNode(updated);
                  }}
                >
                  <option value="">choose backend…</option>
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
              <label title="Explicit opt-in: even 'Auto'/'API only' backend modes never make a paid call unless this is checked.">
                Use API (paid)
              </label>
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

      {hasExtraParams && <div className="node-cell-hint">⚙ for more parameters</div>}

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

      {template && collapseInfo && (
        <div className="node-actions" onClick={(e) => e.stopPropagation()} onDoubleClick={(e) => e.stopPropagation()}>
          <button onClick={expandChain} title="Show this chain's cells individually again, unlocking generate/re-roll/discard">
            ⛶ expand
          </button>
        </div>
      )}

      {template && !collapseInfo && (
        <div className="node-actions" onClick={(e) => e.stopPropagation()} onDoubleClick={(e) => e.stopPropagation()}>
          <button className="primary" onClick={requestGenerate} disabled={node.status === "queued" || node.status === "running"}>
            Generate
          </button>
          {(node.status === "queued" || node.status === "running") && (
            <button onClick={cancel} title="Stop the in-progress generation -- already-finished variants are kept">
              Cancel
            </button>
          )}
          {(node.status === "done" || node.status === "error") && (
            <button onClick={requestReroll} title="Discard and regenerate">
              Re-roll
            </button>
          )}
          <button onClick={discard} disabled={node.status === "discarded"}>
            Discard
          </button>
        </div>
      )}

      {payConfirmAction &&
        createPortal(
          <div className="image-modal-backdrop" onClick={() => setPayConfirmAction(null)}>
            <div className="image-modal-content" onClick={(e) => e.stopPropagation()} style={{ maxWidth: 380 }}>
              <div className="node-cell-header">
                <span>This is paid</span>
              </div>
              <p style={{ fontSize: 13 }}>
                This will call <strong>{apiBackend?.name ?? apiBackend?.provider ?? "a paid API"}</strong>
                {apiCapability?.config.model_id ? ` (${apiCapability.config.model_id as string})` : ""}. Continue with this generation?
              </p>
              <div className="node-actions">
                <button className="primary" onClick={confirmPaidAction}>
                  Yes, generate (paid)
                </button>
                <button onClick={() => setPayConfirmAction(null)}>Cancel</button>
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
              <button type="button" className="image-modal-close" onClick={() => setParamsOpen(false)} title="Close">
                ×
              </button>
              <div className="node-cell-header">
                <span>{template.name}</span>
                <span className="status-pill">{node.status}</span>
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
                        No source image to preview yet -- x={box.x}, y={box.y}, w={box.width}, h={box.height}
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
                      <div style={{ fontSize: 11, color: "var(--text-dim)" }}>No source image to paint on yet</div>
                    )}
                  </div>
                );
              })}

              {paramFieldInputs}
            </div>
          </div>,
          document.body,
        )}

      {viewChainOpen &&
        collapseInfo &&
        createPortal(
          <div className="image-modal-backdrop" onClick={() => setViewChainOpen(false)}>
            <div className="params-modal-content" onClick={(e) => e.stopPropagation()}>
              <button type="button" className="image-modal-close" onClick={() => setViewChainOpen(false)} title="Close">
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
  const template = templates.find((t) => t.node_type === node.node_type);
  return (
    <div className="field-row" style={{ flexDirection: "column", alignItems: "flex-start", gap: 4 }}>
      <label>
        {node.kind === "asset" ? "Asset" : (template?.name ?? node.node_type ?? "?")} <span className="status-pill">{node.status}</span>
      </label>
      {node.kind === "asset" ? (
        outputs[0] ? (
          <img src={resolveAssetUrl(outputs[0].url)} alt="" style={{ maxWidth: 200, borderRadius: 4 }} />
        ) : (
          <span style={{ fontSize: 11, color: "var(--text-dim)" }}>no output</span>
        )
      ) : Object.keys(node.params).length > 0 ? (
        <pre style={{ fontSize: 11, whiteSpace: "pre-wrap", margin: 0 }}>{JSON.stringify(node.params, null, 2)}</pre>
      ) : (
        <span style={{ fontSize: 11, color: "var(--text-dim)" }}>no params</span>
      )}
    </div>
  );
}

// A RefAsset is a lightweight pointer, not a real asset: kind="asset",
// node_type="asset.refasset", inputs=[{type:"explicit", node_id, output_id}]
// pointing at the real node/output it stands in for (see Grid.tsx's
// createRefAssetAt). It owns no Asset row itself -- it borrows the real one
// for display via the same resolveSlotAsset helper BaseWorkflowNodeView uses
// for crop previews, since ref.type "explicit" resolution is identical
// either way. This is the ONE case position-based layout can't express (the
// real asset is already spoken for elsewhere), so it's also the only node
// left that gets a drawn arrow (Grid.tsx's edges memo, kind "ref").
function RefAssetNodeView({ node, registerRef, compareActive, onCellClicked }: Props) {
  const tracks = useProjectStore((s) => s.tracks);
  const nodesById = useProjectStore((s) => s.nodesById);
  const outputsByNode = useProjectStore((s) => s.outputsByNode);
  const refreshNodeOutputs = useProjectStore((s) => s.refreshNodeOutputs);
  const removeNode = useProjectStore((s) => s.removeNode);
  const [resolved, setResolved] = useState<Asset | null>(null);
  const [fullSizeUrl, setFullSizeUrl] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    resolveSlotAsset(node, 0, tracks, nodesById, outputsByNode, refreshNodeOutputs).then((asset) => {
      if (!cancelled) setResolved(asset);
    });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [node.inputs, outputsByNode]);

  const deleteCell = async () => {
    if (!confirm("Remove this reference? The real asset elsewhere is untouched.")) return;
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
        <span>↗ Reference</span>
        <span className="status-pill">ref</span>
      </div>

      {resolved && (
        <div className="output-grid">
          <div className="output-item">
            {resolved.kind === "mesh" ? (
              <Model3DThumb url={resolveAssetUrl(resolved.url)} />
            ) : (
              <img
                src={resolveAssetUrl(resolved.url)}
                alt="referenced output"
                onDoubleClick={() => setFullSizeUrl(resolveAssetUrl(resolved.url))}
                title="Double-click to open full size"
                style={{ cursor: "zoom-in", opacity: 0.85 }}
              />
            )}
          </div>
        </div>
      )}

      <div className="node-actions">
        <button onClick={deleteCell} title="Remove this reference (doesn't touch the real asset)">
          Delete
        </button>
      </div>

      {fullSizeUrl &&
        createPortal(
          // Portalled out of this cell's DOM subtree -- see BaseAssetNodeView's
          // identical modal for why (Grid.tsx's cell wrapper is HTML5
          // draggable={true}, which hijacks pan gestures started anywhere
          // inside it, portal or not, into a native drag).
          <div className="image-modal-backdrop" onClick={() => setFullSizeUrl(null)}>
            <div className="image-modal-content" onClick={(e) => e.stopPropagation()}>
              <button type="button" className="image-modal-close" onClick={() => setFullSizeUrl(null)} title="Close full-size image">
                ×
              </button>
              <ZoomableImage src={fullSizeUrl} />
            </div>
          </div>,
          document.body,
        )}
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
};

export function NodeCell(props: Props) {
  const override = props.node.node_type ? NODE_VIEWS[props.node.node_type] : undefined;
  if (override) {
    const View = override;
    return <View {...props} />;
  }
  return props.node.kind === "asset" ? <BaseAssetNodeView {...props} /> : <BaseWorkflowNodeView {...props} />;
}
