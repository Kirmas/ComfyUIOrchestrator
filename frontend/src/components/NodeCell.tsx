import { useEffect, useMemo, useRef, useState } from "react";
import { resolveAssetUrl } from "../api/client";
import { assetsApi, jobsApi, nodesApi } from "../api/endpoints";
import { detectCropGroups, resolveCropImageField } from "../cropUtils";
import { resolveSlotAsset } from "../slotResolution";
import { useProjectStore } from "../state/projectStore";
import { defaultInputsForSchema, slotFields } from "../templateUtils";
import type { Asset, Backend, Capability, InputRef, Job, NodeItem, NodeTemplate } from "../types";
import { cx } from "../utils";
import { CropPreview, type CropBox } from "./CropPreview";
import { Model3DThumb } from "./Model3DThumb";
import { ZoomableImage } from "./ZoomableImage";

interface Props {
  node: NodeItem;
  templates: NodeTemplate[];
  backends: Backend[];
  capabilities: Capability[];
  outputs: Asset[];
  pickingActive: boolean;
  isPickingSource: boolean;
  compareActive: boolean;
  isComparingSource: boolean;
  isLastInTrack: boolean;
  registerRef: (nodeId: string, el: HTMLDivElement | null) => void;
  onStartPicking: (nodeId: string, slotIndex: number) => void;
  onCellClicked: (node: NodeItem) => void;
  onSelectCandidate: (node: NodeItem, kept: Asset, others: Asset[]) => Promise<NodeItem | undefined>;
  onStartCompare: (node: NodeItem, asset: Asset) => void;
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
            <>
              <img
                src={resolveAssetUrl(asset.url)}
                alt="output"
                onDoubleClick={() => onImageOpen(resolveAssetUrl(asset.url))}
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
            </>
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
  return (
    <div className="output-grid">
      <div className="output-item">
        {asset.kind === "mesh" ? (
          <Model3DThumb url={resolveAssetUrl(asset.url)} />
        ) : (
          <>
            <img
              src={resolveAssetUrl(asset.url)}
              alt="output"
              onDoubleClick={() => onImageOpen(resolveAssetUrl(asset.url))}
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
          </>
        )}
      </div>
    </div>
  );
}

function AssetNodeCell({
  node,
  outputs,
  pickingActive,
  compareActive,
  isComparingSource,
  isLastInTrack,
  registerRef,
  onCellClicked,
  onSelectCandidate,
  onStartCompare,
}: Props) {
  const setNode = useProjectStore((s) => s.setNode);
  const refreshOutputs = useProjectStore((s) => s.refreshNodeOutputs);
  const removeNode = useProjectStore((s) => s.removeNode);
  const tracks = useProjectStore((s) => s.tracks);
  const loadProject = useProjectStore((s) => s.loadProject);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const isCandidatesGrid = node.is_picker;

  const [fullSizeUrl, setFullSizeUrl] = useState<string | null>(null);

  const openImage = (url: string) => setFullSizeUrl(url);
  const closeImage = () => setFullSizeUrl(null);

  // Starting a compare here just arms Grid-level state (compareFor) -- the
  // second asset comes from clicking a *different* asset node cell anywhere
  // in the project, same click-to-complete gesture slot-picking already uses.
  const startCompare = (asset: Asset) => onStartCompare(node, asset);

  // Deleting cascades forward (everything after this cell in the same track
  // depends on it) and may promote a sibling spawned-track into the freed-up
  // slot (see nodes.py's delete_node) -- either can touch tracks/nodes well
  // beyond this one cell, so a full reload is simpler and safer than trying
  // to hand-patch the store to match.
  const deleteCell = async () => {
    if (!confirm("Delete this cell and everything after it in this track? This can't be undone.")) return;
    await nodesApi.remove(node.id);
    const projectId = tracks.find((t) => t.id === node.track_id)?.project_id;
    if (projectId) await loadProject(projectId);
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

  const cls = cx(
    "node-cell",
    "node-cell-asset",
    `status-${node.status}`,
    (pickingActive || compareActive) && "picking-target",
    isComparingSource && "picking-source",
  );

  return (
    <div
      ref={(el) => registerRef(node.id, el)}
      className={cls}
      tabIndex={outputs.length === 0 && !isCandidatesGrid ? 0 : undefined}
      onPaste={handlePaste}
      onClick={() => (pickingActive || compareActive) && onCellClicked(node)}
    >
      <div className="node-cell-header">
        <span>{isCandidatesGrid ? "Asset Select" : "Asset"}</span>
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
        {!isCandidatesGrid && (
          <button onClick={deleteCell} title="Delete this cell and everything after it in this track">
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

      {fullSizeUrl && (
        <div className="image-modal-backdrop" onClick={closeImage}>
          <div className="image-modal-content" onClick={(e) => e.stopPropagation()}>
            <button type="button" className="image-modal-close" onClick={closeImage} title="Close full-size image">
              ×
            </button>
            <ZoomableImage src={fullSizeUrl} />
          </div>
        </div>
      )}
    </div>
  );
}

function WorkflowNodeCell({ node, templates, backends, capabilities, pickingActive, isPickingSource, isLastInTrack, registerRef, onStartPicking, onCellClicked }: Props) {
  const setNode = useProjectStore((s) => s.setNode);
  const removeNode = useProjectStore((s) => s.removeNode);
  const tracks = useProjectStore((s) => s.tracks);
  const nodesById = useProjectStore((s) => s.nodesById);
  const outputsByNode = useProjectStore((s) => s.outputsByNode);
  const refreshNodeOutputs = useProjectStore((s) => s.refreshNodeOutputs);
  const template = templates.find((t) => t.id === node.template_id) ?? null;
  const [jobs, setJobs] = useState<Job[]>([]);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [pendingSlot, setPendingSlot] = useState<number | null>(null);
  // The face only shows what's needed to glance at status and hit Generate --
  // template name, plan, and other node's worth of pixels (see NodeCell for
  // AssetNodeCell's compact size). Everything else (slot sources, param
  // fields, variants, backend) lives behind this modal so the node stays the
  // same footprint whether it has 2 fields or 20.
  const [paramsOpen, setParamsOpen] = useState(false);

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

  const deleteCell = async () => {
    if (!confirm("Delete this cell? This can't be undone.")) return;
    await nodesApi.remove(node.id);
    removeNode(node.id);
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
    const updated = await nodesApi.update(node.id, { template_id: templateId, inputs, params });
    setNode(updated);
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

  const uploadForSlot = async (slotIndex: number, file: File) => {
    const asset = await assetsApi.upload(file);
    await setSlotSource(slotIndex, { type: "upload", asset_id: asset.id });
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

  const cls = cx("node-cell", `status-${node.status}`, pickingActive && "picking-target", isPickingSource && "picking-source");

  const slotSourceSelects = template &&
    slotFields(template.param_schema).map((field, slotIndex) => {
      const ref = node.inputs[slotIndex];
      return (
        <div key={field.name} className="slot-row">
          <span>{field.label ?? field.name}:</span>
          <select
            value={ref?.type ?? "self_prev"}
            onChange={(e) => {
              const type = e.target.value as InputRef["type"];
              if (type === "upload") {
                setPendingSlot(slotIndex);
                fileInputRef.current?.click();
              } else if (type === "explicit") {
                onStartPicking(node.id, slotIndex);
              } else {
                setSlotSource(slotIndex, { type } as InputRef);
              }
            }}
          >
            <option value="self_prev">prev asset cell</option>
            <option value="track_below_prev">track below</option>
            <option value="upload">upload…</option>
            <option value="explicit">pick cell…</option>
          </select>
        </div>
      );
    });

  const cropFieldNames = useMemo(
    () => new Set(cropGroups.flatMap((g) => [g.xField, g.yField, g.widthField, g.heightField])),
    [cropGroups],
  );

  const paramFieldInputs = template &&
    (template.param_schema.fields ?? [])
      // "seed" fields are randomized fresh per variant by the backend
      // (resolve_node_inputs in worker/tasks.py) -- nothing for the user to set.
      // Crop-group fields render as one draggable box instead (see cropGroups
      // below) -- 4 raw x/y/width/height numbers are one entity, not 4 params.
      .filter((f) => f.type !== "image" && f.type !== "file" && f.type !== "seed" && !cropFieldNames.has(f.name))
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

  const hasExtraParams = Boolean((paramFieldInputs && paramFieldInputs.length > 0) || cropGroups.length > 0);

  return (
    <div
      ref={(el) => registerRef(node.id, el)}
      className={cls}
      onClick={() => pickingActive && onCellClicked(node)}
      onDoubleClick={() => hasExtraParams && setParamsOpen(true)}
      title={hasExtraParams ? "⚙ or double-click to edit parameters" : undefined}
    >
      <div className="node-cell-header">
        <span>{template?.name ?? "(choose template)"}</span>
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
        {isLastInTrack && (
          <button
            onClick={(e) => {
              e.stopPropagation();
              deleteCell();
            }}
            title="Remove this cell entirely (only the last cell in a track can be deleted)"
          >
            ×
          </button>
        )}
      </div>

      {!template && (
        <select onChange={(e) => chooseTemplate(e.target.value)} value="" onClick={(e) => e.stopPropagation()}>
          <option value="" disabled>
            Select node type…
          </option>
          {templates.map((t) => (
            <option key={t.id} value={t.id}>
              {t.name}
            </option>
          ))}
        </select>
      )}

      {template && (
        <div onClick={(e) => e.stopPropagation()} onDoubleClick={(e) => e.stopPropagation()}>
          {slotSourceSelects}

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
              <option value="api_only">API only</option>
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
                {backends.map((b) => (
                  <option key={b.id} value={b.id}>
                    {b.name}
                  </option>
                ))}
              </select>
            )}
          </div>
        </div>
      )}

      {hasExtraParams && <div className="node-cell-hint">⚙ for more parameters</div>}

      <input
        ref={fileInputRef}
        type="file"
        accept="image/*"
        style={{ display: "none" }}
        onChange={(e) => {
          const file = e.target.files?.[0];
          if (file && pendingSlot !== null) uploadForSlot(pendingSlot, file);
          e.target.value = "";
        }}
      />

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

      {template && (
        <div className="node-actions" onClick={(e) => e.stopPropagation()} onDoubleClick={(e) => e.stopPropagation()}>
          <button className="primary" onClick={generate} disabled={node.status === "queued" || node.status === "running"}>
            Generate
          </button>
          {(node.status === "queued" || node.status === "running") && (
            <button onClick={cancel} title="Stop the in-progress generation -- already-finished variants are kept">
              Cancel
            </button>
          )}
          {(node.status === "done" || node.status === "error") && (
            <button onClick={reroll} title="Discard and regenerate">
              Re-roll
            </button>
          )}
          <button onClick={discard} disabled={node.status === "discarded"}>
            Discard
          </button>
        </div>
      )}

      {isPickingSource && <div style={{ fontSize: 10, color: "var(--accent)" }}>picking source for input…</div>}

      {paramsOpen && template && (
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

            {paramFieldInputs}
          </div>
        </div>
      )}
    </div>
  );
}

export function NodeCell(props: Props) {
  return props.node.kind === "asset" ? <AssetNodeCell {...props} /> : <WorkflowNodeCell {...props} />;
}
