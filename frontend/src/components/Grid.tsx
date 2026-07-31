import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import { annotationsApi, backendsApi, capabilitiesApi, nodesApi, nodeTemplatesApi, projectsApi, tracksApi } from "../api/endpoints";
import { useProjectWs } from "../api/useProjectWs";
import { readClipboard, type CopiedAsset } from "../assetClipboard";
import { useProjectStore } from "../state/projectStore";
import { resolveSlotAsset } from "../slotResolution";
import type { Asset, Backend, Capability, NodeItem, NodeKind, NodeTemplate, Project, Track } from "../types";
import { useT } from "../i18n";
import { cx } from "../utils";
import { isFileDrag } from "../dragUtils";
import { AnnotationFrame } from "./AnnotationFrame";
import { ReferencePicker } from "./ReferencePicker";
import { ArrowsOverlay, type Edge } from "./ArrowsOverlay";
import { CompareModal } from "./CompareModal";
import { NodeCell } from "./NodeCell";

// Discrete zoom levels for the grid canvas (index 2 == 100%). Module-scope so
// the wheel/pinch handlers can read it without it being a fresh array each
// render. Pinch snaps to the nearest of these.
const ZOOM_SCALES = [0.6, 0.8, 1, 1.2, 1.4];
const nearestZoomIdx = (scale: number): number => {
  let best = 0;
  let bestD = Infinity;
  ZOOM_SCALES.forEach((s, i) => {
    const d = Math.abs(s - scale);
    if (d < bestD) {
      bestD = d;
      best = i;
    }
  });
  return best;
};

// A workflow node's result always materializes as a *following* asset node
// (see worker/tasks.py), so the next free step after a workflow node is
// step+2 -- step+1 is reserved for that asset node even before it exists.
function nextStepIndexFor(nodes: NodeItem[]): number {
  if (nodes.length === 0) return 0;
  const last = nodes[nodes.length - 1];
  return last.kind === "workflow" ? last.step_index + 2 : last.step_index + 1;
}

// Mirrors _kind_for_step in backend/app/api/routes/nodes.py: once a
// project's start_kind is set, every column alternates asset/workflow from
// it regardless of which track it's in -- so a brand-new track's first
// column is never a free choice, it's whatever the pattern already says.
function kindForStep(startKind: NodeKind, stepIndex: number): NodeKind {
  const opposite: NodeKind = startKind === "asset" ? "workflow" : "asset";
  return stepIndex % 2 === 0 ? startKind : opposite;
}

// "pick cell..." may only target a settled asset cell with exactly one
// resolved output -- a chooser cell (node_type "asset.select", still showing
// several undecided candidates) has no single well-defined picture to grab,
// so letting it be picked meant silently grabbing whichever candidate
// happened to be "selected" or first in the list, never a choice the user
// actually made. Resolve the chooser (select ★ one) first, then it becomes
// pickable (node_type flips to "asset.single").
function isPickable(node: NodeItem, outputs: Asset[]): boolean {
  if (node.kind !== "asset" || node.node_type === "asset.select") return false;
  // A refasset owns no Asset row of its own (outputs is always empty for
  // it) -- it's pickable via its borrowed/resolved asset instead, resolved
  // on demand in resolvePrimaryOutput.
  return node.node_type === "asset.refasset" || outputs.length > 0;
}

export function Grid({ projectId }: { projectId: string }) {
  const t = useT();
  const { tracks, nodesById, outputsByNode, spans, blockedCells, annotations, dashboardId, navStack, loadProject, reloadTracks, reloadAnnotations, applyProgressEvent, addNode, setNode, removeTrack, refreshNodeOutputs, leaveDashboard } =
    useProjectStore();
  // Nodes ticked for grouping into a comment block. Shift/Ctrl/Cmd-click on a
  // cell toggles membership; a plain click is left alone so it keeps meaning
  // whatever it meant before (compare, ref, open).
  const [selectedNodeIds, setSelectedNodeIds] = useState<Set<string>>(new Set());
  const [templates, setTemplates] = useState<NodeTemplate[]>([]);
  const [backends, setBackends] = useState<Backend[]>([]);
  const [capabilities, setCapabilities] = useState<Capability[]>([]);
  const [project, setProject] = useState<Project | null>(null);
  // Compare mode spans the whole project, not just one node's own candidates
  // -- compareFor is the anchor asset (from wherever "⇄" was clicked), and
  // clicking any other pickable asset-node cell completes the pair (same
  // click-to-complete gesture the ref gesture below also uses).
  const [compareFor, setCompareFor] = useState<{ nodeId: string; asset: Asset } | null>(null);
  const [comparePair, setComparePair] = useState<[Asset, Asset] | null>(null);
  // Asset node currently being dragged (native HTML5 DnD) to a different row
  // within a workflow node's span -- see dropAssetAt below.
  const [draggingAssetId, setDraggingAssetId] = useState<string | null>(null);
  // Workflow node currently being dragged to a different row -- separate
  // gesture from draggingAssetId (see dropWorkflowAt below): moving a
  // workflow node carries every input/output currently aligned to its span
  // along with it, rather than just repositioning one cell.
  const [draggingWorkflowId, setDraggingWorkflowId] = useState<string | null>(null);
  // "+ ref elsewhere" click-to-complete gesture (same shape as compareFor):
  // armed by a real asset node's own button, completed by
  // clicking any reachable empty cell, which creates a RefAsset there instead
  // of relocating the original.
  const [refFor, setRefFor] = useState<{ nodeId: string } | null>(null);
  // "⧉" click-to-complete gesture, the workflow-node counterpart of refFor:
  // armed by a workflow card's own button, completed by clicking any empty
  // workflow-parity cell, which creates a real second node there carrying every
  // one of this one's local settings (nodesApi.duplicate). Separate state from
  // refFor on purpose -- a workflow copy and an asset reference target different
  // cells and mean different things (see duplicate_node's docstring).
  const [copyFor, setCopyFor] = useState<{ nodeId: string } | null>(null);
  // True while an OS file drag (isFileDrag) is over the canvas -- switches on
  // the drop targets over every empty asset cell (see fileDropCells below).
  // Files can also be dropped straight onto an existing empty asset cell, which
  // NodeCell handles itself.
  const [fileDragActive, setFileDragActive] = useState(false);
  // Which empty cell the reference picker is filling, if open.
  const [pickRefAt, setPickRefAt] = useState<{ row: number; step: number } | null>(null);
  // Re-read on every change (including from another tab) so "paste ref" shows
  // up the moment something is copied, without a manual refresh.
  const [clipboard, setClipboard] = useState<CopiedAsset | null>(() => readClipboard());
  useEffect(() => {
    const sync = () => setClipboard(readClipboard());
    window.addEventListener("asset-clipboard-changed", sync);
    window.addEventListener("storage", sync);
    return () => {
      window.removeEventListener("asset-clipboard-changed", sync);
      window.removeEventListener("storage", sync);
    };
  }, []);
  // How many leading columns an as-yet-empty track should skip before its
  // first real cell -- purely a UI notion (never sent to the backend on its
  // own): a track only gets a step_index once a node actually exists at it,
  // so "skip" just moves where the "+ asset"/"+ step" button appears next,
  // and gets attached to the node the moment one is finally created there.
  // Irrelevant (and cleared) once the track has any node.
  const [emptyTrackSkip, setEmptyTrackSkip] = useState<Record<string, number>>({});
  const containerRef = useRef<HTMLDivElement>(null);
  const cellRefs = useRef(new Map<string, HTMLDivElement>()).current;
  // Click-drag panning of the grid's own scroll container, armed only when
  // the pointerdown didn't land on a node cell or any interactive control
  // (button/input/select/etc, and anything HTML5-draggable) -- letting it
  // start from those would fight their own click/drag gestures. Panning
  // itself is just scrolling containerRef by the drag delta, not a
  // transform -- the grid's own layout (track labels, cell positions)
  // never needs to know panning happened at all.
  const [isPanning, setIsPanning] = useState(false);
  // Mouse-wheel zoom, 5 discrete levels (-2..+2, index 2 == 0 == today's
  // 100% size). Applied as a CSS transform: scale() on gridWrapperRef,
  // never touching layout (gridTemplateColumns/Rows) itself -- an outer
  // sizer div (below, sized to naturalSize * zoomScale) gives the scroll
  // container the correct scaled scroll extent, since transform alone
  // doesn't change an element's own layout box/offsetWidth. naturalSize is
  // gridWrapperRef's un-scaled size (ResizeObserver -- transform doesn't
  // affect what that reports on the element carrying it), kept in sync
  // whenever real content (tracks/nodes) changes its footprint.
  const [zoomIndex, setZoomIndex] = useState(2);
  const zoomScale = ZOOM_SCALES[zoomIndex];
  const zoomIndexRef = useRef(zoomIndex);
  zoomIndexRef.current = zoomIndex;
  const gridWrapperRef = useRef<HTMLDivElement>(null);
  const [naturalSize, setNaturalSize] = useState({ w: 0, h: 0 });
  // Active touch pointers on the canvas background, for two-finger pinch-zoom
  // (the mobile equivalent of the mouse wheel below). Single-finger touch
  // still just pans, via the same drag path as the mouse (onBackgroundPointerDown).
  const touchPtsRef = useRef<Map<number, { x: number; y: number }>>(new Map());
  const pinchRef = useRef<{ startDist: number; startIdx: number } | null>(null);
  // Cleanup for an in-progress background pan, so a pinch (second finger
  // landing) can cancel the pan the first finger started.
  const panCleanupRef = useRef<null | (() => void)>(null);

  useEffect(() => {
    const el = gridWrapperRef.current;
    if (!el) return;
    const observer = new ResizeObserver(() => setNaturalSize({ w: el.offsetWidth, h: el.offsetHeight }));
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  // Zoom the canvas to an absolute level, keeping the point under
  // (focalClientX, focalClientY) fixed on screen: converts it to "natural"
  // (unscaled) coordinates before changing scale, then restores that same
  // point under the focal at the new scale, on the next frame once the sizer
  // div has picked up the resize. Shared by the mouse wheel and touch pinch.
  const applyZoomStep = (nextIdx: number, focalClientX: number, focalClientY: number) => {
    setZoomIndex((idx) => {
      const clamped = Math.min(ZOOM_SCALES.length - 1, Math.max(0, nextIdx));
      if (clamped === idx) return idx;
      const el = containerRef.current;
      if (!el) return clamped;
      const rect = el.getBoundingClientRect();
      const offsetX = focalClientX - rect.left;
      const offsetY = focalClientY - rect.top;
      const oldScale = ZOOM_SCALES[idx];
      const newScale = ZOOM_SCALES[clamped];
      const naturalX = (el.scrollLeft + offsetX) / oldScale;
      const naturalY = (el.scrollTop + offsetY) / oldScale;
      requestAnimationFrame(() => {
        el.scrollLeft = naturalX * newScale - offsetX;
        el.scrollTop = naturalY * newScale - offsetY;
      });
      return clamped;
    });
  };

  // Plain wheel, no modifier needed -- panning already took over scrolling
  // (see onBackgroundPointerDown above), so the wheel is free to mean zoom
  // instead. Attached as a native (non-React) listener because React's own
  // onWheel is passive by default; calling preventDefault() there just
  // warns and does nothing, and without it the browser would ALSO scroll
  // the container on every zoom step.
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const handler = (e: WheelEvent) => {
      e.preventDefault();
      applyZoomStep(zoomIndexRef.current + (e.deltaY < 0 ? 1 : -1), e.clientX, e.clientY);
    };
    el.addEventListener("wheel", handler, { passive: false });
    return () => el.removeEventListener("wheel", handler);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  // Guards the auto-expand effect below against firing WHILE any multi-node
  // structural operation (its own insert, or a manual dropAssetAt/
  // dropWorkflowAt drag) is still applying its updates one at a time. Each
  // individual node update changes `tracks`/`nodesById`, which would
  // otherwise re-trigger the effect against a partially-applied, transiently
  // inconsistent state -- e.g. mid-way through dropWorkflowAt moving a
  // workflow node before its dependents have followed it yet, the effect
  // could "see" a phantom collision and insert its own extra rows on top,
  // compounding into corrupted track layout (2026-07-17 incident).
  const structuralOpRef = useRef(false);

  const reloadProject = () => projectsApi.get(projectId).then(setProject);

  useEffect(() => {
    loadProject(projectId);
    reloadProject();
    nodeTemplatesApi.list().then(setTemplates);
    backendsApi.list().then(setBackends);
    capabilitiesApi.list().then(setCapabilities);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId, loadProject]);

  useProjectWs(projectId, applyProgressEvent);

  const nodesByTrack = useMemo(() => {
    const map = new Map<string, NodeItem[]>();
    for (const node of Object.values(nodesById)) {
      if (!map.has(node.track_id)) map.set(node.track_id, []);
      map.get(node.track_id)!.push(node);
    }
    for (const list of map.values()) list.sort((a, b) => a.step_index - b.step_index);
    return map;
  }, [nodesById]);

  const maxStep = useMemo(() => {
    let max = 0;
    for (const node of Object.values(nodesById)) max = Math.max(max, node.step_index);
    return max;
  }, [nodesById]);

  const sortedTracks = useMemo(() => [...tracks].sort((a, b) => a.row_index - b.row_index), [tracks]);

  const trackByRowIndex = useMemo(() => new Map(tracks.map((t) => [t.row_index, t])), [tracks]);
  const rowIndexOfTrack = (trackId: string): number => tracks.find((t) => t.id === trackId)?.row_index ?? 0;

  // (Output-binding placement rules used to be mirrored here as
  // isPositionAllowedFor -- deleted. The backend's _ensure_output_binding is
  // now the only copy; the client sends a move intent and the backend accepts
  // or 409s. No more "keep these two in sync".)

  // Whether `node` is the *actual* materialized output of some workflow --
  // created_by_node_id is the authoritative, backend-set answer (doesn't
  // care where the node currently sits -- see isPositionAllowedFor above),
  // so it's the ONLY thing this checks now. Used to derive this
  // positionally instead (same track's next step, or the first node of a
  // track spawned from a workflow) -- dropped after it proved to cut both
  // ways once nodes actually started moving around: it false-negatived a
  // real output moved elsewhere in its creator's span (already fixed by
  // checking created_by_node_id first), but it ALSO false-positived a
  // plain manually-placed asset that just happened to land back in
  // whatever cell looks like "the" output slot for some workflow sitting
  // right before it (2026-07-18: a manual asset dragged onto a creator's
  // home output cell silently lost its "not this workflow's output" badge,
  // despite never having been that workflow's actual output). Anything
  // with no creator is just parked there manually (Change 3's "+ asset" in
  // an otherwise-empty spanned row, or any node that predates the
  // created_by_node_id column, see migration 0006) and gets the badge.
  const isWorkflowOutput = (node: NodeItem): boolean => Boolean(node.created_by_node_id);

  // Where a node actually renders: always exactly its own track's row_index
  // -- there's no display-only position. "Moving" a node to a different row
  // means reassigning which track it belongs to (see dropAssetAt/
  // applyRowMove below), the same real relocation onSelectCandidate already
  // used before any of this existed. For a workflow node this is its span's
  // start row; moving it carries its currently-aligned inputs/outputs along
  // by the same delta, each via their own track reassignment.
  const effectiveRow = (node: NodeItem): number => rowIndexOfTrack(node.track_id);

  const nodesByRowStep = useMemo(() => {
    const map = new Map<string, NodeItem>();
    for (const node of Object.values(nodesById)) {
      map.set(`${effectiveRow(node)}:${node.step_index}`, node);
    }
    return map;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nodesById, tracks]);

  // The ideal number of rows a workflow node's merged cell wants: enough to
  // reach every image/file input slot on one side, and every row currently
  // holding one of its materialized outputs on the other (its own home
  // track's next step, plus one more per sibling track spawned from a
  // multi-select in its candidates grid -- see onSelectCandidate below).
  // This is the *desired* size -- see spans below for what actually
  // fits right now, and the auto-expand effect further down for closing the
  // gap between the two by inserting real track rows.
  //
  // A prior version of this measured each output's actual row OFFSET from
  // this node's home row (rowIndexOfTrack(out.track_id) - creatorRow)
  // instead of counting spawned tracks, to also reach rows the BACKEND
  // grows into (worker/tasks.py's _locate_output_row, which doesn't tag its
  // inserted Track with spawned_from_node_id). That fed back on itself: the
  // auto-expand effect's insertTracksAt(position, count) shifts every track
  // at row_index >= position down by count -- including this node's own
  // output tracks whenever they sit at or past the insertion point -- which
  // increases their measured offset, which increases desired again next
  // render, which inserts again, forever (2026-07-20 incident: a project's
  // track count jumped from ~18 to 55 and climbing before this was caught).
  // Counting spawned tracks instead is immune to that: insertTracksAt never
  // changes how many tracks are spawned_from_node_id-tagged to this node,
  // only their row_index, so the desired value it feeds back into is
  // stable. The tradeoff (reverted along with this) is the backend-grown-row
  // case above doesn't stretch the merged cell to visually reach it -- a
  // narrower, real bug, not an infinite one.
  // (Span formulas spans / spans / blockedCells
  // used to be computed here -- deleted. The backend now computes them
  // (core/grid_layout.py, GET /projects/{id}/layout) and the store exposes
  // them as `spans` (per workflow: {desired, achieved}) and `blockedCells` (a
  // Set of "row:col"). This killed the frontend/backend span-drift bug class.)
  const spanDesired = (nodeId: string): number => spans[nodeId]?.desired ?? 1;
  const spanAchieved = (nodeId: string): number => spans[nodeId]?.achieved ?? 1;

  // Which workflow node is the *first* half of a collapsed chain
  // (Node.collapse_target_id, set via the pass-through asset node's own
  // "Collapse chain" button -- see db/models.py's docstring): only the
  // creator gets an entry now (used for its combined label, its expand
  // button, and to span it across the 3 columns the whole chain used to
  // occupy -- see hiddenNodeIds/gridColumn below). The consumer and the
  // pass-through asset itself don't render their own cell at all anymore
  // (2026-07-21: shrinking their *content* while still drawing 3 separate
  // bordered boxes side by side didn't read as "one cell" the way the user
  // actually wanted -- they wanted the old positions visually gone, not
  // just minimized).
  const collapseInfoByNode = useMemo(() => {
    const map = new Map<string, { assetId: string; consumerId: string; combinedLabel: string }>();
    for (const asset of Object.values(nodesById)) {
      if (asset.kind !== "asset" || !asset.collapse_target_id || !asset.created_by_node_id) continue;
      const creator = nodesById[asset.created_by_node_id];
      const consumer = nodesById[asset.collapse_target_id];
      if (!creator || !consumer) continue;
      const creatorName = templates.find((t) => t.node_type === creator.node_type)?.name ?? creator.node_type ?? "?";
      const consumerName = templates.find((t) => t.node_type === consumer.node_type)?.name ?? consumer.node_type ?? "?";
      map.set(creator.id, { assetId: asset.id, consumerId: consumer.id, combinedLabel: `${creatorName}+${consumerName}` });
    }
    return map;
  }, [nodesById, templates]);

  // The pass-through asset and the consumer workflow node of every collapsed
  // chain -- skipped entirely from the main render loop below (no wrapper
  // div at all, not even a minimized one) so the creator's own cell, spanning
  // all 3 columns, is the only thing left where 3 separate boxes used to sit.
  // Collapse eligibility (collapse_node, nodes.py) already guarantees nothing
  // else in the project explicitly references the pass-through asset, so
  // nothing else needs a DOM ref into a cell that's about to not exist.
  const hiddenChainNodeIds = useMemo(() => {
    const ids = new Set<string>();
    for (const asset of Object.values(nodesById)) {
      if (asset.kind !== "asset" || !asset.collapse_target_id) continue;
      ids.add(asset.id);
      ids.add(asset.collapse_target_id);
    }
    return ids;
  }, [nodesById]);

  // What actually fits *right now*, without overlapping another node: capped
  // at the first row (going down from its own) that's already taken by an
  // unrelated node in this SAME step_index column -- every track fills every
  // column independently per the project's asset/workflow alternation
  // pattern (see kindForStep), so a wholly unrelated workflow cell routinely
  // sits at the very same column a few tracks down (e.g. two independently
  // created chains that happen to reach the same step). This is only ever a
  // *transient* value: whenever it falls short of spans, the
  // auto-expand effect below inserts real track rows to close the gap, so
  // rendering always has a collision-safe size to use even in the brief
  // window before that finishes.
  // Every asset-kind node whose very next cell (same row, step+1) is empty
  // and not claimed by some other workflow's spanning card gets its own
  // "+ step" offer -- not just whichever node happens to be its track's
  // current tail. The per-track button below used to be the ONLY way to
  // add a workflow step, anchored to nextStepIndexFor's notion of "the
  // track's last node" -- so an asset dragged into an early, previously-
  // empty column of an already-long track (e.g. relocated there manually)
  // had no "+ step" anywhere near it: the track's real tail was still way
  // out past its other, unrelated cells, several columns over (2026-07-20
  // incident: an asset moved to column 0 of a track that already reached
  // column 8 got no button until scrolled all the way out to column 9).
  // Checking every asset node individually instead covers the track's tail
  // case too (nothing else claims the cell right after it either), so this
  // replaces that per-track "+ step" entirely rather than living alongside it.
  const assetNextStepCells = useMemo(() => {
    const list: { node: NodeItem; row: number; step: number }[] = [];
    for (const node of Object.values(nodesById)) {
      if (node.kind !== "asset") continue;
      // A collapsed chain's pass-through asset doesn't render its own cell
      // at all (see hiddenChainNodeIds) -- nothing for a "+ step" offer to
      // anchor next to, and it's locked/history besides.
      if (hiddenChainNodeIds.has(node.id)) continue;
      const row = effectiveRow(node);
      const step = node.step_index + 1;
      if (nodesByRowStep.has(`${row}:${step}`)) continue;
      if (blockedCells.has(`${row}:${step}`)) continue;
      list.push({ node, row, step });
    }
    return list;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nodesById, nodesByRowStep, blockedCells, tracks, hiddenChainNodeIds]);

  // requiredKind matters for callers that need the *next* step to stay a
  // specific kind (e.g. "+ step" after a manually-filled asset cell always
  // wants a workflow cell right there) -- a blocked column is always
  // workflow-parity (only workflow nodes get a span), so skipping it by a
  // single column would otherwise land on an asset-parity column instead,
  // silently producing the wrong kind once the backend re-derives it from
  // step_index parity (see _kind_for_step).
  const nextFreeStep = (rowIndex: number, step: number, requiredKind?: NodeKind): number => {
    let s = step;
    while (
      blockedCells.has(`${rowIndex}:${s}`) ||
      (requiredKind && project?.start_kind != null && kindForStep(project.start_kind, s) !== requiredKind)
    ) {
      s++;
    }
    return s;
  };

  // Widest column any track's own "+ step" button could land on -- each track's
  // button sits right after *its own* last cell, not a shared global column.
  // For a still-empty track, "skipped" columns (manual or auto, via
  // nextFreeStep) push that button out further still, so the grid needs to
  // be wide enough to show it.
  const maxButtonStep = useMemo(() => {
    let max = 0;
    for (const track of tracks) {
      const trackNodes = nodesByTrack.get(track.id) ?? [];
      const lastNode = trackNodes[trackNodes.length - 1];
      const base = nextStepIndexFor(trackNodes);
      const step = trackNodes.length === 0 ? base + (emptyTrackSkip[track.id] ?? 0) : base;
      // Mirrors assetNextStepCells' own constraint: a "+ step" after an
      // asset cell always needs to land on a workflow-parity column, so it
      // may have to skip an extra column beyond a merely-free one.
      const requiredKind = lastNode?.kind === "asset" ? "workflow" : undefined;
      max = Math.max(max, nextFreeStep(track.row_index, step, requiredKind));
    }
    return Math.max(max, maxStep);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tracks, nodesByTrack, maxStep, emptyTrackSkip, blockedCells]);

  const maxRowSpanBottom = useMemo(() => {
    let max = sortedTracks.length;
    for (const node of Object.values(nodesById)) {
      if (node.kind !== "workflow") continue;
      max = Math.max(max, effectiveRow(node) + (spanAchieved(node.id)));
    }
    return max;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nodesById, spans, sortedTracks, tracks]);

  // Rows a spanning workflow node's merged cell covers but that have no node
  // in its input/output column yet -- offered as manual "+ asset" drop
  // targets (Change 3) and as valid targets for the drag/ref gestures below.
  const emptyReachableCells = useMemo(() => {
    const map = new Map<string, { row: number; step: number }>();
    for (const node of Object.values(nodesById)) {
      if (node.kind !== "workflow") continue;
      // The consumer half of a collapsed chain doesn't render its own cell
      // (hiddenChainNodeIds) -- without this, its own still-unfilled input
      // slots (e.g. a 2nd/3rd image field never wired up) kept offering "+
      // asset" placeholders with no visible card left nearby to explain them
      // (2026-07-21, reported from a real "Crop+UpscaleHead" collapsed cell
      // on prod: dashed boxes floating in what should just be reclaimed
      // blank space). Collapsed also means locked/history -- soliciting a
      // new input for a chain the user isn't going to regenerate doesn't
      // make sense anyway.
      if (hiddenChainNodeIds.has(node.id)) continue;
      const start = effectiveRow(node);
      const span = spanAchieved(node.id);
      for (let r = start; r < start + span; r++) {
        if (!trackByRowIndex.has(r)) continue;
        for (const step of [node.step_index - 1, node.step_index + 1]) {
          if (step < 0) continue;
          const key = `${r}:${step}`;
          if (nodesByRowStep.has(key)) continue;
          map.set(key, { row: r, step });
        }
      }
    }
    return [...map.values()];
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nodesById, spans, tracks, nodesByRowStep, trackByRowIndex, hiddenChainNodeIds]);

  const edges = useMemo<Edge[]>(() => {
    const result: Edge[] = [];

    for (const track of tracks) {
      if (!track.spawned_from_node_id) continue;
      const firstNode = (nodesByTrack.get(track.id) ?? [])[0];
      if (firstNode) result.push({ from: track.spawned_from_node_id, to: firstNode.id, kind: "spawn" });
    }

    // The only other arrow left: a RefAsset node pointing back at the real
    // asset node it stands in for (see NodeCell's RefAssetNodeView). Ordinary
    // workflow<->input/output connections are conveyed by row alignment
    // instead of a drawn arrow now (see spans/effectiveRow above).
    for (const node of Object.values(nodesById)) {
      if (node.node_type !== "asset.refasset") continue;
      const ref = node.inputs[0];
      // No node_id means it points at a library asset, which has no owning
      // cell anywhere in the grid -- there's nothing on screen to draw an
      // arrow back to, so it just renders as an ordinary reference card.
      if (ref?.type === "explicit" && ref.node_id) result.push({ from: ref.node_id, to: node.id, kind: "ref" });
    }

    return result;
  }, [nodesById, nodesByTrack, tracks]);

  const registerRef = (nodeId: string, el: HTMLDivElement | null) => {
    if (el) cellRefs.set(nodeId, el);
    else cellRefs.delete(nodeId);
  };

  const toggleSelected = (nodeId: string) => {
    setSelectedNodeIds((prev) => {
      const next = new Set(prev);
      if (!next.delete(nodeId)) next.add(nodeId);
      return next;
    });
  };

  const createAnnotationFromSelection = async () => {
    if (selectedNodeIds.size === 0) return;
    await annotationsApi.create({ project_id: projectId, node_ids: [...selectedNodeIds], text: "" });
    setSelectedNodeIds(new Set());
    await reloadAnnotations(projectId);
  };

  // A frame's box is derived here, every render, from where its members
  // currently are -- nothing positional is stored on the annotation, so a
  // moved node carries its frame along instead of leaving it behind.
  const annotationBoxes = useMemo(() => {
    const rowOf = new Map(tracks.map((t) => [t.id, t.row_index]));
    return annotations
      .map((annotation) => {
        const positions = annotation.node_ids
          .map((id) => nodesById[id])
          .filter((n): n is NodeItem => Boolean(n))
          .map((n) => ({ row: rowOf.get(n.track_id), col: n.step_index }))
          .filter((p): p is { row: number; col: number } => p.row !== undefined);
        if (positions.length === 0) return null;
        const rows = positions.map((p) => p.row);
        const cols = positions.map((p) => p.col);
        return {
          annotation,
          box: {
            minRow: Math.min(...rows),
            maxRow: Math.max(...rows),
            minCol: Math.min(...cols),
            maxCol: Math.max(...cols),
          },
        };
      })
      .filter((x): x is { annotation: (typeof annotations)[number]; box: { minRow: number; maxRow: number; minCol: number; maxCol: number } } => x !== null);
  }, [annotations, nodesById, tracks]);

  const addTrackRow = async () => {
    // Tail append (no anchor, not head) -- backend splices it after the
    // current last track; reloadTracks re-derives row numbers. dashboard_id
    // matters here precisely because there's no anchor to infer the scope
    // from: without it a row added inside a sub-dashboard would land on the
    // project's main grid instead.
    await tracksApi.create({ project_id: projectId, dashboard_id: dashboardId });
    await reloadTracks(projectId);
  };

  // Delete is now a backend pointer splice (unlink + delete, see tracks.py):
  // the track leaves its chain, its two neighbours are stitched together, and
  // nothing else is renumbered. The old reindex -- an optimistic local shift
  // plus a fire-and-forget Promise.all of per-track row_index PATCHes that
  // could partially fail and leave a gap -- is gone entirely (that gap was
  // the 2026-07-21 data-loss surface). We just drop the deleted track's nodes
  // locally and re-derive every row number from the shorter list.
  const deleteTrackRow = async (trackId: string) => {
    if (!confirm(t("grid.confirmDeleteTrack"))) return;
    if (structuralOpRef.current) {
      alert(t("grid.moveInProgress"));
      return;
    }
    structuralOpRef.current = true;
    try {
      await tracksApi.remove(trackId);
      removeTrack(trackId);
      await reloadTracks(projectId);
    } catch (e) {
      // e.g. the backend refuses deleting a multi-input workflow's span row.
      alert(e instanceof Error ? e.message : t("grid.deleteTrackFailed"));
    } finally {
      structuralOpRef.current = false;
    }
  };

  // NodeCell's "⤢" recompute button: clean up a workflow's now-empty spawned
  // tracks -- candidate lines whose output was moved away or discarded, which
  // leave blank rows the card used to span. The card's span itself is tight
  // again once these are gone (the backend span no longer counts empty spawned
  // tracks -- see core/grid_layout.py). Each removal is a plain track delete;
  // the backend refuses any that are still part of a real span (delete_track's
  // 409), which we just skip -- no uncaught errors, no half-done loop.
  const shrinkWorkflowToFit = async (node: NodeItem) => {
    if (structuralOpRef.current) {
      alert(t("grid.moveInProgress"));
      return;
    }
    structuralOpRef.current = true;
    try {
      const live = useProjectStore.getState();
      const emptySpawned = live.tracks.filter(
        (t) =>
          t.spawned_from_node_id === node.id &&
          !Object.values(live.nodesById).some((n) => n.track_id === t.id),
      );
      if (emptySpawned.length === 0) {
        alert(t("grid.nothingToCleanUp"));
        return;
      }
      let removed = 0;
      for (const t of emptySpawned) {
        try {
          await tracksApi.remove(t.id);
          removeTrack(t.id);
          removed++;
        } catch {
          // still part of some workflow's span -- skip it
        }
      }
      if (removed) await reloadTracks(projectId);
    } finally {
      structuralOpRef.current = false;
    }
  };

  // Makes room for a workflow node's full desired span (see the auto-expand
  // effect below) by inserting `count` brand-new empty tracks so they occupy
  // positions [position .. position+count). This is now a backend linked-list
  // splice, not a renumber: each new track is spliced after the one before it
  // (the first after the track currently at position-1, or at the head when
  // position is 0), which shifts everyone below purely by *derived* position
  // -- no row_index is written anywhere. reloadTracks then re-derives the
  // numbers. Returns the created tracks with their fresh derived row_index.
  const insertTracksAt = async (
    position: number,
    count: number,
    extraFields?: { spawned_from_node_id?: string; spawned_from_output_id?: string },
  ): Promise<Track[]> => {
    const ordered = [...useProjectStore.getState().tracks].sort((a, b) => a.row_index - b.row_index);
    let afterId: string | undefined = position > 0 ? ordered[position - 1]?.id : undefined;
    const createdIds: string[] = [];
    for (let i = 0; i < count; i++) {
      const track = await tracksApi.create({
        project_id: projectId,
        // Ignored by the backend whenever after_track_id is given (the anchor's
        // own scope wins), but load-bearing for the place_at_head case, which
        // has no anchor to take a scope from.
        dashboard_id: dashboardId,
        after_track_id: afterId ?? null,
        place_at_head: position === 0 && i === 0,
        ...extraFields,
      });
      createdIds.push(track.id);
      afterId = track.id; // the next new track goes immediately after this one
    }
    await reloadTracks(projectId);
    const fresh = useProjectStore.getState().tracks;
    return createdIds.map((id) => fresh.find((t) => t.id === id)).filter((t): t is Track => t !== undefined);
  };

  // Whether inserting `count` new tracks at `position` (see insertTracksAt)
  // would push some bound output (Node.created_by_node_id) out of its own
  // valid range (isPositionAllowedFor's row-span check above). Uniformly
  // shifting every track at row_index >= position preserves every RELATIVE
  // distance between tracks that are EITHER both shifted or both left alone
  // -- safe on its own -- but a creator and one of its own outputs can land
  // on opposite sides of `position` (creator's row < position <= output's
  // row): only the output shifts, stretching the gap between them by
  // `count`, which can push it past creatorRow + span even though nothing
  // about the workflow's own template changed. A spawned-track output
  // (Track.spawned_from_node_id) isn't affected -- that link doesn't care
  // about row_index at all -- only the span-based path is checked here.
  const wouldBreakOutputBinding = (position: number, count: number): boolean =>
    Object.values(nodesById).some((node) => {
      if (!node.created_by_node_id) return false;
      const creator = nodesById[node.created_by_node_id];
      if (!creator) return false;
      const outputTrack = tracks.find((t) => t.id === node.track_id);
      if (outputTrack?.spawned_from_node_id === creator.id) return false;
      const creatorRow = rowIndexOfTrack(creator.track_id);
      const outputRow = rowIndexOfTrack(node.track_id);
      // Same span as isPositionAllowedFor -- see its own comment for why this
      // must match spans rather than re-deriving a narrower,
      // input-slots-only number that can drift from what's actually drawn.
      const span = spanDesired(creator.id);
      const newCreatorRow = creatorRow + (creatorRow >= position ? count : 0);
      const newOutputRow = outputRow + (outputRow >= position ? count : 0);
      return !(newCreatorRow <= newOutputRow && newOutputRow < newCreatorRow + span);
    });

  const addTrackAbove = async (trackId: string) => {
    const track = tracks.find((t) => t.id === trackId);
    if (!track) return;
    if (wouldBreakOutputBinding(track.row_index, 1)) {
      alert(t("grid.insertTrackBlocked"));
      return;
    }
    await insertTracksAt(track.row_index, 1);
  };

  // (Column-shift helpers insertColumnsAt / wouldSplitAnyWorkflow lived here
  // only for the frontend's own workflow column-move -- deleted along with it.
  // Any column shifting a move needs now happens in the backend move endpoint.)

  const nextStepIndex = (trackId: string): number => nextStepIndexFor(nodesByTrack.get(trackId) ?? []);

  // Column kind is a project-wide pattern, not a per-node choice (see nodes.py's
  // create_node): only the very first node in the whole project picks it; every
  // add after that just sends a placeholder kind that the backend overrides
  // based on step_index parity. stepIndexOverride is for a track whose first
  // cell got pushed past column 0 by the "empty" skip button below.
  const addStep = async (trackId: string, firstKindChoice?: NodeKind, stepIndexOverride?: number) => {
    const node = await nodesApi.create({
      track_id: trackId,
      step_index: stepIndexOverride ?? nextStepIndex(trackId),
      kind: firstKindChoice ?? "workflow",
    });
    addNode(node);
    if (project?.start_kind == null) reloadProject();
    setEmptyTrackSkip((prev) => {
      if (!(trackId in prev)) return prev;
      const next = { ...prev };
      delete next[trackId];
      return next;
    });
  };

  // (settleSoleCandidate + onSelectCandidate deleted -- the whole candidate
  // fork is one backend intent now: POST /api/nodes/{id}/pick-candidate and
  // /pick-all-candidates. NodeCell calls those directly + reloads.)

  // Shared by every gesture below that needs "whatever this node's currently
  // resolved output is" (compareFor and the ref gestures) -- picks the
  // selected candidate if there is one, else the first output. A refasset
  // owns no Asset row of its own, so it resolves through its "explicit"
  // input pointer instead (same mechanism as its thumbnail, see
  // resolveSlotAsset).
  const resolvePrimaryOutput = async (node: NodeItem): Promise<Asset | null> => {
    if (node.node_type === "asset.refasset") {
      return resolveSlotAsset(node, 0, tracks, nodesById, outputsByNode, refreshNodeOutputs);
    }
    const outputs = await nodesApi.outputs(node.id).catch(() => []);
    return outputs.find((a) => a.selected) ?? outputs[0] ?? null;
  };

  const onCellClicked = async (node: NodeItem) => {
    if (compareFor && compareFor.nodeId !== node.id) {
      const asset = await resolvePrimaryOutput(node);
      if (!asset) {
        alert(t("grid.noOutputsYet"));
        return;
      }
      setComparePair([compareFor.asset, asset]);
      setCompareFor(null);
    }
  };

  const onStartCompare = (node: NodeItem, asset: Asset) => setCompareFor({ nodeId: node.id, asset });

  // Manual drag of an asset node onto another cell. The frontend does NO
  // placement logic anymore -- it just sends the intent (the target grid
  // cell) and re-fetches the authoritative layout. All the checks
  // (allowed-position/output-binding, swap-vs-move, collision) live in the
  // backend's move endpoint (api/routes/nodes.py's move_node); a rejected
  // move comes back as a 409 whose message is shown as-is.
  const dropAssetAt = async (targetRow: number, targetStep: number) => {
    const draggedId = draggingAssetId;
    setDraggingAssetId(null);
    if (!draggedId) return;
    const dragged = nodesById[draggedId];
    if (!dragged || dragged.kind !== "asset") return;

    const draggedRow = rowIndexOfTrack(dragged.track_id);
    if (targetRow === draggedRow && targetStep === dragged.step_index) return;

    if (structuralOpRef.current) {
      alert(t("grid.moveInProgress"));
      return;
    }
    structuralOpRef.current = true;
    try {
      await nodesApi.move(dragged.id, { target_row: targetRow, target_step: targetStep });
      await loadProject(projectId);
    } catch (e) {
      alert(e instanceof Error ? e.message : t("grid.moveCellFailed"));
    } finally {
      structuralOpRef.current = false;
    }
  };


  // Like dropAssetAt: pure intent. The backend's move endpoint carries the
  // workflow node's dependents along, validates, and rejects (409) if the
  // target area isn't free -- the frontend plans nothing and just re-fetches
  // the authoritative layout.
  const dropWorkflowAt = async (workflowNode: NodeItem, targetRow: number, targetStep: number) => {
    if (structuralOpRef.current) {
      alert(t("grid.moveInProgress"));
      return;
    }
    if (targetRow === effectiveRow(workflowNode) && targetStep === workflowNode.step_index) return;
    structuralOpRef.current = true;
    try {
      await nodesApi.move(workflowNode.id, { target_row: targetRow, target_step: targetStep });
      await loadProject(projectId);
    } catch (e) {
      alert(e instanceof Error ? e.message : t("grid.moveNodeFailed"));
    } finally {
      structuralOpRef.current = false;
    }
  };

  // Every currently-empty, workflow-parity cell across the whole grid (any
  // track row x any column already in use, per kindForStep) -- rendered as
  // drop targets while a workflow drag is in progress, in both dimensions at
  // once rather than just the dragged node's own column. Dropping onto an
  // already-occupied cell (an existing workflow's wrapper div) is handled
  // separately and can still push/insert to make room; this set is only the
  // cells that need no pushing at all.
  // Also the placement targets for the "⧉" copy gesture (copyFor) -- an empty
  // workflow cell is an empty workflow cell whether a node is being dragged
  // into it or copied into it, so the same list serves both rather than a
  // second near-identical memo.
  const emptyWorkflowCells = useMemo(() => {
    if ((!draggingWorkflowId && !copyFor) || !project?.start_kind) return [];
    const cells: { row: number; step: number }[] = [];
    for (const row of tracks.map((t) => t.row_index)) {
      for (let step = 0; step <= maxButtonStep; step++) {
        if (kindForStep(project.start_kind, step) !== "workflow") continue;
        if (nodesByRowStep.has(`${row}:${step}`)) continue;
        // A cell some other workflow's spanning card already covers holds no
        // node of its own, so the check above passes -- but dropping (or
        // copying) a card there just puts two cards on top of each other, since
        // blockedCells is exactly "covered by a span in that span's own
        // column" (core/grid_layout.py) and a workflow's own column is what
        // both gestures land in.
        if (blockedCells.has(`${row}:${step}`)) continue;
        cells.push({ row, step });
      }
    }
    return cells;
  }, [draggingWorkflowId, copyFor, project, tracks, maxButtonStep, nodesByRowStep, blockedCells]);

  // Same idea as emptyWorkflowCells, but for the asset half of the grid: every
  // currently-empty asset-parity cell, regardless of any drag -- the raw set
  // both asset drop-target layers below are cut from.
  const emptyAssetCells = useMemo(() => {
    if (!project?.start_kind) return [];
    const cells: { row: number; step: number }[] = [];
    for (const row of tracks.map((t) => t.row_index)) {
      for (let step = 0; step <= maxButtonStep; step++) {
        if (kindForStep(project.start_kind, step) !== "asset") continue;
        if (nodesByRowStep.has(`${row}:${step}`)) continue;
        cells.push({ row, step });
      }
    }
    return cells;
  }, [project, tracks, maxButtonStep, nodesByRowStep]);

  // Drop targets for an internal asset-node drag: all of the above except the
  // cells reachable from some workflow's own span (emptyReachableCells, which
  // keeps rendering its own "+ asset" button there regardless of a drag, so
  // it's excluded to avoid stacking two drop targets on the same cell). A
  // manually-placed asset isn't tied to any one workflow's territory --
  // moveAssetTo/isWorkflowOutput decide what a cell means purely from where it
  // ends up, not from where it started -- so dragging one to an unrelated empty
  // cell elsewhere on the board has to actually be reachable to drop on
  // (2026-07-18: dragging an asset out to a cell no workflow's span reached did
  // nothing at all -- no drop target was even rendered there).
  const emptyAssetDropCells = useMemo(() => {
    if (!draggingAssetId) return [];
    const reachable = new Set(emptyReachableCells.map(({ row, step }) => `${row}:${step}`));
    return emptyAssetCells.filter(({ row, step }) => !reachable.has(`${row}:${step}`));
  }, [draggingAssetId, emptyAssetCells, emptyReachableCells]);

  // Drop targets for a file dragged in from the desktop. Unlike
  // emptyAssetDropCells this does NOT exclude emptyReachableCells: those render
  // their own "+ asset" button, which is a click affordance, not a drop one --
  // dropping a photo into a workflow's waiting input slot is the main thing
  // this feature is for. The layer renders after them in the DOM so it wins
  // hit-testing while the drag is in progress (grid items paint in DOM order).
  const fileDropCells = fileDragActive ? emptyAssetCells : [];

  // Places a RefAsset node (a lightweight pointer, not a copy) at (row, step),
  // referencing sourceNode's currently resolved output -- used both by the
  // drag-collision fallback above and by "+ ref elsewhere"'s click-to-complete
  // gesture below.
  const createRefAssetAt = async (sourceNode: NodeItem, row: number, step: number) => {
    const targetTrack = trackByRowIndex.get(row);
    if (!targetTrack) return;
    const asset = await resolvePrimaryOutput(sourceNode);
    if (!asset) {
      alert(t("grid.refNoOutput"));
      return;
    }
    const refNode = await nodesApi.create({
      track_id: targetTrack.id,
      step_index: step,
      kind: "asset",
      node_type: "asset.refasset",
      inputs: [{ type: "explicit", node_id: sourceNode.id, output_id: asset.id }],
    });
    addNode(refNode);
  };

  /** Places a library asset (one the idea board owns) in a cell.
   *
   * Same node shape as createRefAssetAt above, minus node_id: a library asset
   * has no owning node, and `explicit` refs resolve straight by asset id on
   * both ends (_explicit_ref_asset in worker/tasks.py, resolveSlotAsset here).
   * A refasset rather than a real asset node is the whole point -- Asset.node_id
   * cascades, so a cell that OWNED a board image would destroy it on deletion.
   */
  const placeLibraryAssetAt = async (asset: Asset, row: number, step: number) => {
    const targetTrack = trackByRowIndex.get(row);
    if (!targetTrack) return;
    const refNode = await nodesApi.create({
      track_id: targetTrack.id,
      step_index: step,
      kind: "asset",
      node_type: "asset.refasset",
      inputs: [{ type: "explicit", output_id: asset.id }],
    });
    addNode(refNode);
  };

  /** Pastes whatever asset is on the clipboard as a reference in this cell.
   *
   * Reuses placeLibraryAssetAt: a clipboard entry and a board library asset are
   * the same thing at this point -- an asset id with no owning node in this
   * grid -- so both become an `explicit` refasset by the identical path. The
   * clipboard is deliberately left loaded afterwards: pasting the same
   * reference into two or three cells is the normal case, and the user said
   * they would rather repeat the paste than manage a multi-slot buffer.
   */
  const pasteRefAt = async (row: number, step: number) => {
    const entry = readClipboard();
    if (!entry) return;
    try {
      await placeLibraryAssetAt({ id: entry.assetId } as Asset, row, step);
    } catch (err) {
      alert(err instanceof Error ? err.message : t("grid.pasteRefFailed"));
    }
  };

  const onStartRef = (node: NodeItem) => setRefFor({ nodeId: node.id });

  const completeRefAt = async (row: number, step: number) => {
    if (!refFor) return;
    const sourceNode = nodesById[refFor.nodeId];
    setRefFor(null);
    if (!sourceNode) return;
    await createRefAssetAt(sourceNode, row, step);
  };

  const onStartCopy = (node: NodeItem) => setCopyFor({ nodeId: node.id });

  // Completes the "⧉" gesture: pure intent, like every other placement now --
  // the backend copies the fields, validates the cell and materializes the
  // copy's own input-slot rows (duplicate_node), and we re-fetch the
  // authoritative layout. A rejected placement comes back as a 409 shown as-is.
  const completeCopyAt = async (row: number, step: number) => {
    if (!copyFor) return;
    const sourceId = copyFor.nodeId;
    setCopyFor(null);
    try {
      await nodesApi.duplicate(sourceId, { target_row: row, target_step: step });
      await loadProject(projectId);
      // A copy carrying a template can grow rows for its input slots
      // (ensure_span_rows), same as choosing a template on a fresh cell does.
      await reloadTracks(projectId);
    } catch (e) {
      alert(e instanceof Error ? e.message : t("grid.copyFailed"));
    }
  };

  /** Fills a still-empty grid cell straight from an OS file drop: creates the
   * asset node there first (nothing exists to upload into yet -- the same
   * nodesApi.create the cell's own "+ asset" button runs), then uploads every
   * dropped file into it. A file dropped on an already-existing empty asset
   * cell skips all this and is handled by NodeCell itself. */
  const dropFilesAt = async (row: number, step: number, files: File[]) => {
    const targetTrack = trackByRowIndex.get(row);
    if (!targetTrack || files.length === 0) return;
    try {
      const created = await nodesApi.create({ track_id: targetTrack.id, step_index: step, kind: "asset" });
      addNode(created);
      for (const file of files) {
        await nodesApi.uploadAsset(created.id, file);
      }
      await refreshNodeOutputs(created.id);
      // The upload flips the node's status to done server-side -- re-fetch so
      // the cell doesn't keep rendering as an empty draft. (No reloadProject
      // for start_kind here, unlike addStep: a file drop can only ever land on
      // a cell fileDropCells offered, which requires start_kind to be set
      // already.)
      setNode(await nodesApi.get(created.id));
    } catch (e) {
      alert(e instanceof Error ? e.message : t("grid.dropFileFailed"));
    }
  };

  // (The reactive auto-expand useEffect that used to grow a workflow's rows
  // client-side is gone -- growth is imperative on the backend now
  // (ensure_span_rows, called when a template is assigned). The frontend no
  // longer mutates grid structure as a render side-effect, which is what
  // once caused runaway track growth.)

  const onBackgroundPointerDown = (e: React.PointerEvent) => {
    if (e.button !== 0) return; // left button only -- middle/right keep their own browser behavior
    const target = e.target as HTMLElement;
    // .image-modal-backdrop covers every modal in the app (crop/mask paint
    // canvases, compare slider, zoom, pay-confirm, ...) -- they're all
    // createPortal'd to document.body (see NodeCell.tsx), so they're outside
    // this container in the real DOM, but React's synthetic pointerdown still
    // bubbles up the *React* tree to here regardless of portal target. Without
    // this exclusion, any pointerdown inside a modal (e.g. painting a mask)
    // arms this pan-drag, whose window-level pointermove then visibly drags
    // the grid underneath the modal as the user interacts with it.
    // .annotation-label is the interactive part of a comment block's frame;
    // like the modals, it sits inside the grid and would otherwise arm a pan.
    if (
      target.closest(
        "button, input, select, textarea, a, .node-cell, [draggable='true'], .image-modal-backdrop, .annotation-label",
      )
    )
      return;
    const container = containerRef.current;
    if (!container) return;
    // Track touch pointers for pinch-zoom. A second finger landing turns the
    // gesture into a pinch and cancels the pan the first finger started.
    if (e.pointerType === "touch") {
      touchPtsRef.current.set(e.pointerId, { x: e.clientX, y: e.clientY });
      if (touchPtsRef.current.size >= 2) {
        panCleanupRef.current?.();
        const pts = [...touchPtsRef.current.values()];
        pinchRef.current = { startDist: Math.hypot(pts[0].x - pts[1].x, pts[0].y - pts[1].y), startIdx: zoomIndexRef.current };
        return;
      }
    }
    e.preventDefault();
    const startX = e.clientX;
    const startY = e.clientY;
    const startScrollLeft = container.scrollLeft;
    const startScrollTop = container.scrollTop;
    setIsPanning(true);
    const onMove = (ev: PointerEvent) => {
      container.scrollLeft = startScrollLeft - (ev.clientX - startX);
      container.scrollTop = startScrollTop - (ev.clientY - startY);
    };
    const onUp = () => {
      setIsPanning(false);
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("pointercancel", onUp);
      panCleanupRef.current = null;
    };
    panCleanupRef.current = onUp;
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", onUp);
  };

  // Two-finger pinch on the canvas background -> discrete zoom, snapping to the
  // nearest level as the fingers spread/close, centered on the pinch midpoint.
  const onBackgroundPointerMove = (e: React.PointerEvent) => {
    if (e.pointerType !== "touch" || !touchPtsRef.current.has(e.pointerId)) return;
    touchPtsRef.current.set(e.pointerId, { x: e.clientX, y: e.clientY });
    const pinch = pinchRef.current;
    if (!pinch || touchPtsRef.current.size < 2) return;
    e.preventDefault();
    const pts = [...touchPtsRef.current.values()];
    const d = Math.hypot(pts[0].x - pts[1].x, pts[0].y - pts[1].y);
    const m = { x: (pts[0].x + pts[1].x) / 2, y: (pts[0].y + pts[1].y) / 2 };
    const targetScale = ZOOM_SCALES[pinch.startIdx] * (d / pinch.startDist);
    applyZoomStep(nearestZoomIdx(targetScale), m.x, m.y);
  };

  const onBackgroundPointerUp = (e: React.PointerEvent) => {
    if (e.pointerType !== "touch") return;
    touchPtsRef.current.delete(e.pointerId);
    if (touchPtsRef.current.size < 2) pinchRef.current = null;
  };

  return (
    <div
      className={cx("main-area", isPanning && "panning")}
      ref={containerRef}
      onPointerDown={onBackgroundPointerDown}
      onPointerMove={onBackgroundPointerMove}
      onPointerUp={onBackgroundPointerUp}
      onPointerCancel={onBackgroundPointerUp}
      // Arming the file-drop layer here, on the whole canvas, rather than on
      // each target cell: dataTransfer.files is unreadable until the drop
      // itself, so a target that only appeared once the pointer was already
      // over it could never be aimed at. This just detects "a file drag is
      // happening" (isFileDrag reads .types, which IS available) and lights
      // every empty asset cell up. No preventDefault here -- the target cells
      // do that themselves, so dropping on the empty canvas stays a non-drop.
      onDragOver={(e) => {
        if (isFileDrag(e.dataTransfer)) setFileDragActive(true);
      }}
      onDragLeave={(e) => {
        // Fires for every child boundary crossed too, so only a leave that
        // actually exits the container counts.
        if (!e.currentTarget.contains(e.relatedTarget as Node | null)) setFileDragActive(false);
      }}
      onDrop={() => setFileDragActive(false)}
    >
      {/* A sibling of the scaled sizer/grid-wrapper below, not a descendant --
          transform: scale() on an ancestor would otherwise hijack this as
          its own containing block instead of the real viewport/scroll
          container, breaking position: sticky. Mouse wheel over the canvas
          zooms (onBackgroundPointerDown's pan took over plain scrolling). */}
      <div className="zoom-indicator">
        {t("grid.zoom")} {zoomIndex > 2 ? `+${zoomIndex - 2}` : zoomIndex - 2} ({Math.round(zoomScale * 100)}%)
        <button onClick={() => setZoomIndex(2)} title={t("grid.resetZoomTitle")} disabled={zoomIndex === 2}>
          {t("common.reset")}
        </button>
      </div>
      {/* Only shown once you're actually inside something. Sibling of the
          scaled wrapper for the same containing-block reason as .zoom-indicator.
          Each crumb jumps straight to that depth rather than stepping out one
          level at a time. */}
      {navStack.length > 1 && (
        <div className="subgraph-breadcrumb">
          {navStack.map((entry, i) => (
            <Fragment key={`${entry.dashboardId ?? "root"}-${i}`}>
              {i > 0 && <span className="crumb-sep">›</span>}
              <button
                type="button"
                onClick={() => void leaveDashboard(i)}
                disabled={i === navStack.length - 1}
                title={t("subgraph.openTitle")}
              >
                {i === 0 ? t("subgraph.mainGrid") : entry.name || t("subgraph.untitled")}
              </button>
            </Fragment>
          ))}
        </div>
      )}
      {selectedNodeIds.size > 0 && (
        // Same reasoning as .zoom-indicator above: a sibling of the scaled
        // wrapper, so its position: sticky isn't captured by the zoom
        // transform's containing block.
        <div className="selection-bar">
          {selectedNodeIds.size === 1
            ? t("grid.selectedOne", { n: selectedNodeIds.size })
            : t("grid.selectedMany", { n: selectedNodeIds.size })}
          <button onClick={createAnnotationFromSelection} title={t("grid.addCommentTitle")}>
            {t("grid.addComment")}
          </button>
          <button onClick={() => setSelectedNodeIds(new Set())} title={t("grid.clearSelectionTitle")}>
            {t("common.clearLower")}
          </button>
        </div>
      )}
      {/* A sibling of the scaled grid-wrapper, not a descendant -- its path
          coordinates are already computed in real (post-transform) pixels
          via getBoundingClientRect (see ArrowsOverlay), so nesting it inside
          an ancestor with transform: scale() would scale the SVG itself a
          SECOND time on top of that, breaking every arrow at any zoom level
          other than 1 (2026-07-21 incident). */}
      <ArrowsOverlay edges={edges} cellRefs={cellRefs} containerRef={containerRef} deps={[nodesById, tracks, zoomScale]} />
      <div style={{ width: naturalSize.w * zoomScale, height: naturalSize.h * zoomScale }}>
        <div ref={gridWrapperRef} className="grid-wrapper" style={{ transform: `scale(${zoomScale})`, transformOrigin: "0 0" }}>
        <div
          className="grid-canvas"
          style={{
            gridTemplateColumns: `120px repeat(${maxButtonStep + 2}, 260px)`,
            // minmax floor, not plain "auto": an "auto" row with nothing else
            // in it collapses to zero, so a workflow node spanning several
            // otherwise-empty rows would render at the same height as a
            // single row -- no visible span at all. The floor guarantees a
            // spanning card is visibly N rows tall regardless of what (if
            // anything) sits in the rows it reaches into.
            gridTemplateRows: `repeat(${Math.max(maxRowSpanBottom, 1)}, minmax(120px, auto))`,
            gridAutoRows: "minmax(120px, auto)",
          }}
        >
          {sortedTracks.map((track) => (
            <div
              key={`label-${track.id}`}
              className="track-label"
              // track.row_index, not this map's own array index -- every
              // node's own gridRow (below) is positioned by effectiveRow(node)
              // = row_index directly, so once row_index has a gap (a track
              // deleted from the middle, e.g. row_index 0,1,2,5,6,7... with 3
              // and 4 gone), a sequential array index desyncs from that by
              // however large the gap is: this label would render N rows
              // above where its own track's real content sits. That's not
              // just cosmetic -- a track whose LABEL then lands on an empty-
              // looking row (nothing else genuinely there) but whose real
              // content rendered further down under a DIFFERENT track's
              // label reads as "this one's empty, safe to delete" when it
              // is not (2026-07-21 incident: deleting a track that looked
              // empty this way cascaded and deleted real, unrelated nodes
              // that only *appeared* to belong to some other row).
              style={{ gridColumn: 1, gridRow: track.row_index + 1, display: "flex", alignItems: "center", justifyContent: "space-between" }}
            >
              {t("grid.track", { n: track.row_index })}
              <div style={{ display: "flex", gap: 4 }}>
                <button
                  onClick={() => addTrackAbove(track.id)}
                  title={t("grid.addTrackAboveTitle")}
                  style={{ fontSize: 10, padding: "1px 4px" }}
                >
                  {t("grid.addTrackAbove")}
                </button>
                <button onClick={() => deleteTrackRow(track.id)} title={t("grid.deleteTrackTitle")} style={{ fontSize: 10, padding: "1px 4px" }}>
                  ×
                </button>
              </div>
            </div>
          ))}

          {sortedTracks.map((track) => {
            const trackNodes = nodesByTrack.get(track.id) ?? [];
            return trackNodes.map((node) => {
              if (hiddenChainNodeIds.has(node.id)) return null;
              const row = effectiveRow(node);
              const gridRow = node.kind === "workflow" ? `${row + 1} / span ${spanAchieved(node.id)}` : row + 1;
              // No span here (unlike an earlier version): collapse_node
              // (nodes.py) now actually relocates the pass-through asset and
              // consumer out of this row's S+1/S+2 columns rather than just
              // hiding their rendering (2026-07-21) -- those columns are
              // genuinely free, ordinary cells again (a moved-in asset gets
              // its own normal "+ step", etc.), not space this card should
              // keep visually claiming.
              const gridColumn = node.step_index + 2;
              // A still-undecided candidates picker ("asset.select") has no
              // single well-defined picture to grab for compare/ref/pick
              // (isPickable above still excludes it for that reason), but
              // dragging moves the whole picker -- all its candidates
              // together, none singled out -- so that's unambiguous and
              // just as valid a reposition as asset.single/refasset. A
              // refasset still occupies a real (row, column) slot like any
              // other asset node, so it's just as repositionable -- only
              // dragging is meaningless for workflow cells (their own start
              // row isn't draggable, only their span grows/shrinks
              // automatically).
              const isDraggableAsset = node.kind === "asset";
              const isDraggableWorkflow = node.kind === "workflow";
              return (
                <div
                  key={node.id}
                  // display:flex so the actual visible card (.node-cell, a
                  // plain block child with no explicit height) stretches to
                  // fill this wrapper's full spanned height -- without it,
                  // the wrapper itself grows to N rows (grid's default
                  // stretch already does that part) but the card just sits
                  // at its own natural content height inside that space,
                  // leaving invisible dead space below it and making a span
                  // look identical to a single row.
                  style={{ gridColumn, gridRow, display: "flex" }}
                  className={cx(selectedNodeIds.has(node.id) && "cell-selected")}
                  // Capture phase, so ticking a cell for a comment block never
                  // also triggers whatever a plain click on that cell does.
                  onClickCapture={(e) => {
                    if (!(e.shiftKey || e.ctrlKey || e.metaKey)) return;
                    e.preventDefault();
                    e.stopPropagation();
                    toggleSelected(node.id);
                  }}
                  draggable={isDraggableAsset || isDraggableWorkflow}
                  onDragStart={() => {
                    if (isDraggableAsset) setDraggingAssetId(node.id);
                    else if (isDraggableWorkflow) setDraggingWorkflowId(node.id);
                  }}
                  onDragEnd={() => {
                    setDraggingAssetId(null);
                    setDraggingWorkflowId(null);
                  }}
                  onDragOver={(e) => {
                    if ((node.kind === "asset" && draggingAssetId) || (node.kind === "workflow" && draggingWorkflowId)) {
                      e.preventDefault();
                    }
                  }}
                  onDrop={(e) => {
                    if (node.kind === "asset" && draggingAssetId) {
                      e.preventDefault();
                      dropAssetAt(row, node.step_index);
                    } else if (node.kind === "workflow" && draggingWorkflowId) {
                      e.preventDefault();
                      const dragged = nodesById[draggingWorkflowId];
                      setDraggingWorkflowId(null);
                      if (dragged) dropWorkflowAt(dragged, row, node.step_index);
                    }
                  }}
                >
                  <NodeCell
                    node={node}
                    templates={templates}
                    backends={backends}
                    capabilities={capabilities}
                    outputs={outputsByNode[node.id] ?? []}
                    compareActive={
                      compareFor !== null && compareFor.nodeId !== node.id && isPickable(node, outputsByNode[node.id] ?? [])
                    }
                    isComparingSource={compareFor?.nodeId === node.id}
                    isManualPlacement={isDraggableAsset && node.step_index > 0 && !isWorkflowOutput(node)}
                    isRefSource={refFor?.nodeId === node.id}
                    isCopySource={copyFor?.nodeId === node.id}
                    registerRef={registerRef}
                    onCellClicked={onCellClicked}
                    onStartCompare={onStartCompare}
                    onStartRef={onStartRef}
                    onStartCopy={onStartCopy}
                    onShrinkToFit={shrinkWorkflowToFit}
                    collapseInfo={collapseInfoByNode.get(node.id)}
                  />
                </div>
              );
            });
          })}

          {annotationBoxes.map(({ annotation, box }) => (
            <AnnotationFrame
              key={annotation.id}
              annotation={annotation}
              box={box}
              onSave={async (text) => {
                await annotationsApi.update(annotation.id, { text });
                await reloadAnnotations(projectId);
              }}
              onDelete={async () => {
                await annotationsApi.remove(annotation.id);
                await reloadAnnotations(projectId);
              }}
            />
          ))}

          {emptyReachableCells.map(({ row, step }) => (
            <div
              key={`empty-${row}-${step}`}
              style={{ gridColumn: step + 2, gridRow: row + 1, display: "flex", alignItems: "center", justifyContent: "center" }}
              onDragOver={(e) => {
                if (draggingAssetId) e.preventDefault();
              }}
              onDrop={(e) => {
                if (!draggingAssetId) return;
                e.preventDefault();
                dropAssetAt(row, step);
              }}
              onClick={() => refFor && completeRefAt(row, step)}
            >
              {refFor ? (
                <button style={{ fontSize: 10, padding: "1px 4px" }} title={t("grid.placeRefHereTitle")}>
                  {t("grid.placeRefHere")}
                </button>
              ) : (
                <>
                  <button
                    style={{ fontSize: 10, padding: "1px 4px", opacity: 0.6 }}
                    title={t("grid.addAssetManualTitle")}
                    onClick={async () => {
                      const targetTrack = trackByRowIndex.get(row);
                      if (!targetTrack) return;
                      const created = await nodesApi.create({ track_id: targetTrack.id, step_index: step, kind: "asset" });
                      addNode(created);
                    }}
                  >
                    {t("grid.addAsset")}
                  </button>
                  <button
                    style={{ fontSize: 10, padding: "1px 4px", opacity: 0.6, marginLeft: 4 }}
                    title={t("grid.fromReferencesTitle")}
                    onClick={() => setPickRefAt({ row, step })}
                  >
                    {t("grid.fromReferences")}
                  </button>
                  {clipboard && (
                    <button
                      style={{ fontSize: 10, padding: "1px 4px", opacity: 0.6, marginLeft: 4 }}
                      title={t("grid.pasteRefTitle", { label: clipboard.label })}
                      onClick={() => void pasteRefAt(row, step)}
                    >
                      {t("grid.pasteRef")}
                    </button>
                  )}
                </>
              )}
            </div>
          ))}

          {emptyWorkflowCells.map(({ row, step }) => (
            <div
              key={`wf-drop-${row}-${step}`}
              style={{
                gridColumn: step + 2,
                gridRow: row + 1,
                border: "2px dashed var(--accent)",
                borderRadius: 8,
                opacity: 0.5,
                display: copyFor ? "flex" : undefined,
                alignItems: "center",
                justifyContent: "center",
              }}
              onDragOver={(e) => e.preventDefault()}
              onDrop={(e) => {
                e.preventDefault();
                const dragged = draggingWorkflowId ? nodesById[draggingWorkflowId] : null;
                setDraggingWorkflowId(null);
                if (dragged) dropWorkflowAt(dragged, row, step);
              }}
              onClick={() => copyFor && completeCopyAt(row, step)}
            >
              {copyFor && (
                <button style={{ fontSize: 10, padding: "1px 4px" }} title={t("grid.placeCopyHereTitle")}>
                  {t("grid.placeCopyHere")}
                </button>
              )}
            </div>
          ))}

          {emptyAssetDropCells.map(({ row, step }) => (
            <div
              key={`asset-drop-${row}-${step}`}
              style={{
                gridColumn: step + 2,
                gridRow: row + 1,
                border: "2px dashed var(--success)",
                borderRadius: 8,
                opacity: 0.5,
              }}
              onDragOver={(e) => e.preventDefault()}
              onDrop={(e) => {
                e.preventDefault();
                dropAssetAt(row, step);
              }}
            />
          ))}

          {sortedTracks.map((track, rowIdx) => {
            const showStartChoice = project?.start_kind == null;
            const trackNodes = nodesByTrack.get(track.id) ?? [];
            const firstNode = trackNodes[0];
            // A track's own leading columns (before its first node) can be
            // just as unreachable as a wholly empty track's, even when the
            // track already has real content further out -- e.g. one of
            // character_chart's 8 input-slot tracks, each holding only its
            // own reference photo at step 6, with columns 0-5 never touched.
            // assetNextStepCells (every asset offers "+ step" once the cell
            // right after IT is empty) doesn't reach those leading columns
            // at all, since there's no asset node sitting in them to anchor
            // to -- only a track with literally ZERO nodes got a button
            // here before (2026-07-20 incident: a track with unrelated
            // content starting at step 6 showed nothing for its own empty
            // steps 0-5). `ceiling` is where this leading gap ends: the
            // first real node's own column, or unbounded for a wholly empty
            // track.
            const ceiling = firstNode ? firstNode.step_index : Infinity;
            if (ceiling === 0) return null;
            // A track with no cells yet (freshly added via "+ New track")
            // has nothing to auto-chain its first cell either -- same
            // problem as an asset cell with an empty next column, just for
            // whichever column its button currently sits on. Unlike the very
            // first track in the project, it's not a free choice: start_kind
            // already fixes what kind every column is, so show the one
            // button that matches instead of asking -- but "empty" lets the
            // user push that button (and the kind it offers) out to the next
            // column, for tracks that shouldn't start where the pattern says.
            const rawButtonStep = !showStartChoice ? emptyTrackSkip[track.id] ?? 0 : 0;
            // If this track's own next slot lands inside another track's
            // spanning workflow card (blockedCells), auto-advance past it
            // -- that cell is already visually and physically taken, so
            // offering a button there would let the user create a node right
            // on top of it.
            const buttonStep = showStartChoice ? rawButtonStep : nextFreeStep(track.row_index, rawButtonStep);
            if (buttonStep >= ceiling) return null;
            const emptyTrackKind = !showStartChoice ? kindForStep(project!.start_kind!, buttonStep) : null;
            const skipColumn = () => setEmptyTrackSkip((prev) => ({ ...prev, [track.id]: (prev[track.id] ?? 0) + 1 }));
            // The column right after buttonStep is just as reachable, and its
            // kind is always the opposite one by construction (parity
            // alternates every column) -- offer it too, right away, instead
            // of making the user click "empty" first just to reveal it. A
            // brand-new track with nothing in it yet is the common case this
            // matters for: both "start with an asset" and "start straight
            // into a workflow, no asset first" should be one click each, not
            // one click to skip plus one to create (2026-07-20: only
            // buttonStep's own button showed; the very next column, just as
            // free, needed an extra "empty" click to even see).
            const secondStep = buttonStep + 1;
            const secondKind = emptyTrackKind === "asset" ? "workflow" : emptyTrackKind === "workflow" ? "asset" : null;
            const secondAvailable =
              secondKind !== null && secondStep < ceiling && !blockedCells.has(`${track.row_index}:${secondStep}`);
            return (
              <Fragment key={track.id}>
                <div
                  style={{ gridColumn: buttonStep + 2, gridRow: rowIdx + 1, alignSelf: "center", display: "flex", gap: 4 }}
                >
                  {showStartChoice ? (
                    <>
                      <button onClick={() => addStep(track.id, "asset")} title={t("grid.startWithAssetTitle")}>
                        {t("grid.startWithAsset")}
                      </button>
                      <button onClick={() => addStep(track.id, "workflow")} title={t("grid.startWithWorkflowTitle")}>
                        {t("grid.startWithWorkflow")}
                      </button>
                    </>
                  ) : emptyTrackKind === "asset" ? (
                    refFor ? (
                      <button onClick={() => completeRefAt(track.row_index, buttonStep)} title={t("grid.placeRefHereTitle")}>
                        {t("grid.placeRefHere")}
                      </button>
                    ) : (
                      <>
                        <button onClick={() => addStep(track.id, "asset", buttonStep)} title={t("grid.addAssetHereTitle")}>
                          {t("grid.addAsset")}
                        </button>
                        <button onClick={skipColumn} title={t("grid.emptyTitle")}>
                          {t("grid.empty")}
                        </button>
                      </>
                    )
                  ) : emptyTrackKind === "workflow" ? (
                    <>
                      <button onClick={() => addStep(track.id, "workflow", buttonStep)} title={t("grid.addStepTitle")}>
                        {t("grid.addStep")}
                      </button>
                      <button onClick={skipColumn} title={t("grid.emptyTitle")}>
                        {t("grid.empty")}
                      </button>
                    </>
                  ) : null}
                </div>
                {!showStartChoice && secondAvailable && (
                  <div style={{ gridColumn: secondStep + 2, gridRow: rowIdx + 1, alignSelf: "center", display: "flex", gap: 4 }}>
                    {secondKind === "asset" ? (
                      <button onClick={() => addStep(track.id, "asset", secondStep)} title={t("grid.addAssetHereTitle")}>
                        {t("grid.addAsset")}
                      </button>
                    ) : (
                      <button onClick={() => addStep(track.id, "workflow", secondStep)} title={t("grid.addStepNoAssetTitle")}>
                        {t("grid.addStep")}
                      </button>
                    )}
                  </div>
                )}
              </Fragment>
            );
          })}

          {/* Last of the overlay layers on purpose: overlapping grid items are
              painted (and hit-tested) in DOM order, so while a file drag is in
              progress this sits on top of the "+ asset"/"from references" buttons
              in the same cell -- which are click affordances that would
              otherwise swallow the drop. */}
          {fileDropCells.map(({ row, step }) => (
            <div
              key={`file-drop-${row}-${step}`}
              style={{
                gridColumn: step + 2,
                gridRow: row + 1,
                border: "2px dashed var(--success)",
                borderRadius: 8,
                background: "color-mix(in srgb, var(--success) 12%, transparent)",
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                fontSize: 10,
                color: "var(--text-dim)",
              }}
              onDragOver={(e) => e.preventDefault()}
              onDrop={(e) => {
                e.preventDefault();
                setFileDragActive(false);
                dropFilesAt(row, step, Array.from(e.dataTransfer.files));
              }}
            >
              {t("grid.dropHere")}
            </div>
          ))}

          {assetNextStepCells.map(({ node, row, step }) => (
            <div
              key={`add-asset-${node.id}`}
              style={{ gridColumn: step + 2, gridRow: row + 1, alignSelf: "center", display: "flex", gap: 4 }}
            >
              <button onClick={() => addStep(node.track_id, undefined, step)} title={t("grid.addStepAfterAssetTitle")}>
                {t("grid.addStep")}
              </button>
            </div>
          ))}
        </div>

        <div style={{ marginTop: 16 }}>
          <button onClick={addTrackRow}>{t("grid.newTrack")}</button>
          {compareFor && (
            <button style={{ marginLeft: 8 }} onClick={() => setCompareFor(null)}>
              {t("grid.cancelCompare")}
            </button>
          )}
          {refFor && (
            <button style={{ marginLeft: 8 }} onClick={() => setRefFor(null)}>
              {t("grid.cancelRef")}
            </button>
          )}
          {copyFor && (
            <button style={{ marginLeft: 8 }} onClick={() => setCopyFor(null)}>
              {t("grid.cancelCopy")}
            </button>
          )}
        </div>
        </div>
      </div>

      {comparePair && <CompareModal left={comparePair[0]} right={comparePair[1]} onClose={() => setComparePair(null)} />}
      {pickRefAt && (
        <ReferencePicker
          projectId={projectId}
          onClose={() => setPickRefAt(null)}
          onPick={async (asset) => {
            const target = pickRefAt;
            setPickRefAt(null);
            await placeLibraryAssetAt(asset, target.row, target.step);
          }}
        />
      )}
    </div>
  );
}
