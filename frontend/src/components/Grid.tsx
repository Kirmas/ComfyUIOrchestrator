import { useEffect, useMemo, useRef, useState } from "react";
import { assetsApi, backendsApi, capabilitiesApi, nodesApi, nodeTemplatesApi, projectsApi, tracksApi } from "../api/endpoints";
import { useProjectWs } from "../api/useProjectWs";
import { useProjectStore } from "../state/projectStore";
import { resolveSlotAsset } from "../slotResolution";
import { slotFields } from "../templateUtils";
import type { Asset, Backend, Capability, NodeItem, NodeKind, NodeTemplate, Project } from "../types";
import { ArrowsOverlay, type Edge } from "./ArrowsOverlay";
import { CompareModal } from "./CompareModal";
import { NodeCell } from "./NodeCell";

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
  const { tracks, nodesById, outputsByNode, loadProject, applyProgressEvent, addTrack, addNode, removeTrack, refreshNodeOutputs, setTracks } =
    useProjectStore();
  const [templates, setTemplates] = useState<NodeTemplate[]>([]);
  // desiredRowSpanByNode looks up each workflow's template to count its
  // image/file input slots -- until this first resolves, `templates` is `[]`
  // and that lookup silently reads as "0 slots", making every workflow's
  // desired span look smaller than it really is. The auto-expand effect
  // reacts to that transient (wrong) value just like a real one, which can
  // insert/move real track rows before the correct template data ever
  // arrives (2026-07-17 incident: rows shifted on a plain page load, no drag
  // involved). Gate span-desire and the auto-expand effect on this instead
  // of just checking `templates.length` -- an unlikely-but-real project with
  // zero templates at all must still be treated as "loaded", not "pending".
  const [templatesLoaded, setTemplatesLoaded] = useState(false);
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
  // How many leading columns an as-yet-empty track should skip before its
  // first real cell -- purely a UI notion (never sent to the backend on its
  // own): a track only gets a step_index once a node actually exists at it,
  // so "skip" just moves where the "+ asset"/"+ step" button appears next,
  // and gets attached to the node the moment one is finally created there.
  // Irrelevant (and cleared) once the track has any node.
  const [emptyTrackSkip, setEmptyTrackSkip] = useState<Record<string, number>>({});
  const containerRef = useRef<HTMLDivElement>(null);
  const cellRefs = useRef(new Map<string, HTMLDivElement>()).current;
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
    nodeTemplatesApi.list().then((list) => {
      setTemplates(list);
      setTemplatesLoaded(true);
    });
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

  // A node with created_by_node_id set (a workflow's own materialized
  // output, see backend's Node.created_by_node_id docstring) is rigidly
  // bound to its creator: only allowed at the creator's own output step
  // (creator.step_index + 1), in a row that's either within the creator's
  // own row-span (its home row through home row + however many image/file
  // input slots its template declares -- the same span rowSpanByNode grows
  // real tracks for) or a track spawned from it -- exactly the tracks
  // onSelectCandidate ever moves a settled/leftover output into once
  // there's more results than the creator's own span has room for. A node
  // with no creator (upload, "+ asset", RefAsset) has nothing to check here
  // and is free to land anywhere. Mirrored by the backend's own
  // _ensure_output_binding (api/routes/nodes.py) -- this is the
  // fast-path/UI-facing check, that's the enforced one; a stale local
  // `nodesById`/`tracks` snapshot here just means a 409 from there instead
  // of a silent success, not a way to actually bypass the rule.
  const isPositionAllowedFor = (node: NodeItem, row: number, step: number): boolean => {
    if (!node.created_by_node_id) return true;
    const creator = nodesById[node.created_by_node_id];
    if (!creator) return true;
    if (step !== creator.step_index + 1) return false;
    const targetTrack = trackByRowIndex.get(row);
    if (!targetTrack) return false;
    if (targetTrack.spawned_from_node_id === creator.id) return true;
    const creatorRow = rowIndexOfTrack(creator.track_id);
    const template = templates.find((t) => t.node_type === creator.node_type);
    const slotCount = template ? slotFields(template.param_schema).length : 0;
    const span = Math.max(slotCount, 1);
    return targetTrack.row_index >= creatorRow && targetTrack.row_index < creatorRow + span;
  };

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
  // This is the *desired* size -- see rowSpanByNode below for what actually
  // fits right now, and the auto-expand effect further down for closing the
  // gap between the two by inserting real track rows.
  const desiredRowSpanByNode = useMemo(() => {
    const map = new Map<string, number>();
    for (const node of Object.values(nodesById)) {
      if (node.kind !== "workflow") continue;
      const template = templates.find((t) => t.node_type === node.node_type);
      const inputSlots = template ? slotFields(template.param_schema).length : 0;
      const spawnedRows = tracks.filter((t) => t.spawned_from_node_id === node.id).length;
      map.set(node.id, Math.max(inputSlots, 1 + spawnedRows, 1));
    }
    return map;
  }, [nodesById, templates, tracks]);

  // What actually fits *right now*, without overlapping another node: capped
  // at the first row (going down from its own) that's already taken by an
  // unrelated node in this SAME step_index column -- every track fills every
  // column independently per the project's asset/workflow alternation
  // pattern (see kindForStep), so a wholly unrelated workflow cell routinely
  // sits at the very same column a few tracks down (e.g. two independently
  // created chains that happen to reach the same step). This is only ever a
  // *transient* value: whenever it falls short of desiredRowSpanByNode, the
  // auto-expand effect below inserts real track rows to close the gap, so
  // rendering always has a collision-safe size to use even in the brief
  // window before that finishes.
  const rowSpanByNode = useMemo(() => {
    const map = new Map<string, number>();
    for (const node of Object.values(nodesById)) {
      if (node.kind !== "workflow") continue;
      const desired = desiredRowSpanByNode.get(node.id) ?? 1;
      const start = effectiveRow(node);
      let span = 1;
      while (span < desired && !nodesByRowStep.has(`${start + span}:${node.step_index}`)) span++;
      map.set(node.id, span);
    }
    return map;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nodesById, desiredRowSpanByNode, tracks, nodesByRowStep]);

  // Rows a spanning workflow node covers in ITS OWN column, one track row
  // down from its own start -- a completely unrelated track sharing that
  // exact column (every track fills every column independently, see
  // kindForStep) has to treat that cell as taken, or its own "+ step"/"+
  // asset" button would render right on top of the spanning card, and
  // clicking it would create a real node overlapping it. Maps to the actual
  // occupying node (not just a boolean) so any caller can say *which*
  // workflow card is responsible for a given blocked cell.
  const blockingNodeByCell = useMemo(() => {
    const map = new Map<string, NodeItem>();
    for (const node of Object.values(nodesById)) {
      if (node.kind !== "workflow") continue;
      const start = effectiveRow(node);
      const span = rowSpanByNode.get(node.id) ?? 1;
      for (let r = start + 1; r < start + span; r++) {
        map.set(`${r}:${node.step_index}`, node);
      }
    }
    return map;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nodesById, rowSpanByNode, tracks]);

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
      blockingNodeByCell.has(`${rowIndex}:${s}`) ||
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
      // Mirrors canAddStep's constraint below: a "+ step" after a
      // manually-filled asset cell always needs to land on a workflow-parity
      // column, so it may have to skip an extra column beyond a merely-free one.
      const requiredKind = lastNode?.kind === "asset" ? "workflow" : undefined;
      max = Math.max(max, nextFreeStep(track.row_index, step, requiredKind));
    }
    return Math.max(max, maxStep);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tracks, nodesByTrack, maxStep, emptyTrackSkip, blockingNodeByCell]);

  const maxRowSpanBottom = useMemo(() => {
    let max = sortedTracks.length;
    for (const node of Object.values(nodesById)) {
      if (node.kind !== "workflow") continue;
      max = Math.max(max, effectiveRow(node) + (rowSpanByNode.get(node.id) ?? 1));
    }
    return max;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nodesById, rowSpanByNode, sortedTracks, tracks]);

  // Rows a spanning workflow node's merged cell covers but that have no node
  // in its input/output column yet -- offered as manual "+ asset" drop
  // targets (Change 3) and as valid targets for the drag/ref gestures below.
  const emptyReachableCells = useMemo(() => {
    const map = new Map<string, { row: number; step: number }>();
    for (const node of Object.values(nodesById)) {
      if (node.kind !== "workflow") continue;
      const start = effectiveRow(node);
      const span = rowSpanByNode.get(node.id) ?? 1;
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
  }, [nodesById, rowSpanByNode, tracks, nodesByRowStep, trackByRowIndex]);

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
    // instead of a drawn arrow now (see rowSpanByNode/effectiveRow above).
    for (const node of Object.values(nodesById)) {
      if (node.node_type !== "asset.refasset") continue;
      const ref = node.inputs[0];
      if (ref?.type === "explicit") result.push({ from: ref.node_id, to: node.id, kind: "ref" });
    }

    return result;
  }, [nodesById, nodesByTrack, tracks]);

  const registerRef = (nodeId: string, el: HTMLDivElement | null) => {
    if (el) cellRefs.set(nodeId, el);
    else cellRefs.delete(nodeId);
  };

  const addTrackRow = async () => {
    const rowIndex = tracks.length === 0 ? 0 : Math.max(...tracks.map((t) => t.row_index)) + 1;
    const track = await tracksApi.create({ project_id: projectId, row_index: rowIndex });
    addTrack(track);
  };

  // row_index is both the visible "track N" label and, via track_below_prev
  // inputs (see the edges memo above) plus the row-span paradigm's
  // effectiveRow, an adjacency/position link -- so a delete has to reindex
  // every affected track, not just remove the one row. A plain delete
  // left every track after the removed one still holding its old row_index,
  // opening a gap: track labels (rendered at their position in sortedTracks,
  // an array index) desync from their own row's node cells (rendered at
  // their raw, now-gapped row_index), corrupting the whole grid layout below
  // the deleted row (2026-07-17 incident). Shift everything after it down by
  // one to keep the 0..N-1 run contiguous, the same as insertTracksAt does
  // in the opposite direction.
  const deleteTrackRow = async (trackId: string) => {
    if (!confirm("Delete this whole track and every cell in it? This can't be undone.")) return;
    const deletedRow = tracks.find((t) => t.id === trackId)?.row_index;
    await tracksApi.remove(trackId);
    removeTrack(trackId);
    if (deletedRow === undefined) return;

    const remaining = useProjectStore.getState().tracks;
    const toShift = remaining.filter((t) => t.row_index > deletedRow);
    setTracks(remaining.map((t) => (t.row_index > deletedRow ? { ...t, row_index: t.row_index - 1 } : t)));
    await Promise.all(toShift.map((t) => tracksApi.update(t.id, { row_index: t.row_index - 1 })));
  };

  // Makes room for a workflow node's full desired span (see the auto-expand
  // effect below) by inserting `count` brand-new, empty tracks right at
  // `position`: every existing track at or after that row_index shifts down
  // by `count` first (as a block, preserving everything about them except
  // their row label), then the freed rows get real, empty Track rows so the
  // spanning card has genuine grid rows to grow into rather than just a
  // bigger number with nothing backing it. Same row_index/track_below_prev
  // caveat as deleteTrackRow above: this reindexes every affected track's
  // row_index, which is also an adjacency link for "track below" inputs, not
  // just a display label.
  const insertTracksAt = async (position: number, count: number) => {
    const toShift = tracks.filter((t) => t.row_index >= position);
    const shiftedIds = new Set(toShift.map((t) => t.id));
    setTracks(tracks.map((t) => (shiftedIds.has(t.id) ? { ...t, row_index: t.row_index + count } : t)));
    await Promise.all(toShift.map((t) => tracksApi.update(t.id, { row_index: t.row_index + count })));
    for (let i = 0; i < count; i++) {
      const track = await tracksApi.create({ project_id: projectId, row_index: position + i });
      addTrack(track);
    }
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
      const template = templates.find((t) => t.node_type === creator.node_type);
      const slotCount = template ? slotFields(template.param_schema).length : 0;
      const span = Math.max(slotCount, 1);
      const newCreatorRow = creatorRow + (creatorRow >= position ? count : 0);
      const newOutputRow = outputRow + (outputRow >= position ? count : 0);
      return !(newCreatorRow <= newOutputRow && newOutputRow < newCreatorRow + span);
    });

  const addTrackAbove = async (trackId: string) => {
    const track = tracks.find((t) => t.id === trackId);
    if (!track) return;
    if (wouldBreakOutputBinding(track.row_index, 1)) {
      alert("Can't insert a track there -- it would push a workflow's own output out of its valid range. Try inserting elsewhere.");
      return;
    }
    await insertTracksAt(track.row_index, 1);
  };

  // Column counterpart of insertTracksAt -- but unlike rows, a column has no
  // backing entity to create (step_index is just a number every node
  // carries, not a real Track-like row), so this is purely a bulk renumber:
  // every node (in ANY track, project-wide -- columns are shared across the
  // whole project, see kindForStep) at or after `position` gets
  // step_index += count. Caller is responsible for `count` being even
  // (preserves the project-wide asset/workflow parity) and for `position`
  // not splitting any existing workflow node's own [step-1, step+1] unit
  // (see wouldSplitAnyWorkflow below) -- this function itself doesn't
  // validate either, it just shifts.
  const insertColumnsAt = async (position: number, count: number) => {
    const toShift = Object.values(nodesById).filter((n) => n.step_index >= position);
    for (const n of toShift) {
      const updated = await nodesApi.update(n.id, { step_index: n.step_index + count });
      addNode(updated);
    }
  };

  // Whether inserting/removing columns at `position` would split some OTHER
  // workflow node's own 3-column unit [step-1, step, step+1] -- e.g. its own
  // materialized-output column (step+1, see _get_or_create_output_asset_node)
  // shifting away while the workflow itself (step) doesn't, or vice versa.
  // cell_index input resolution (_asset_at_cell_index) hard-codes "one
  // column back" for that workflow, so splitting it would silently detach
  // its inputs from whatever's supposed to feed them.
  const wouldSplitAnyWorkflow = (position: number, excludeId: string): boolean =>
    Object.values(nodesById).some(
      (n) => n.kind === "workflow" && n.id !== excludeId && position > n.step_index - 1 && position <= n.step_index + 1,
    );

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

  // No leftovers: there's only one possible outcome for this cell, so it's
  // not really "a complex node becoming a simple one" in any sense worth
  // avoiding -- just flip its node_type in place. (See below for the case
  // that actually matters.)
  const settleSoleCandidate = async (sourceNode: NodeItem): Promise<void> => {
    const settledNode = await nodesApi.update(sourceNode.id, { node_type: "asset.single" });
    addNode(settledNode);
    await refreshNodeOutputs(sourceNode.id);
  };

  // When there ARE leftovers, this is a genuine fork: the cell that
  // generated N candidates now has to become two different things (one
  // settled result, one still-undecided picker for the rest) -- and rather
  // than mutating sourceNode's role in place to be whichever one is more
  // convenient (see the 2026-07-17 review: "чому мутуємо складнішу ноду в
  // простішу, а не навпаки"), neither identity gets repurposed:
  //   1. sourceNode itself relocates (same id, same job/asset history) into
  //      the new spawned track, keeping its "still needs choosing" role and
  //      its remaining (non-kept) candidates.
  //   2. A brand new "asset.single" node is created in the vacated original
  //      cell, holding only the kept asset.
  // Moving (PATCH track_id) rather than delete+recreate matters here: DELETE
  // /api/nodes/{id} cascades forward through the rest of the track (right
  // for the user's trash-icon action, wrong for this internal reshuffle).
  //
  // Returns the relocated picker node (or undefined if there were no
  // leftovers) -- NodeCell.tsx's "Select all" calls this repeatedly in a
  // tight loop to cascade every candidate into its own line (pick one, get
  // the leftover picker back, pick one from *that*, and so on), so it reads
  // live store state via getState() instead of this render's closed-over
  // tracks/nodesByTrack -- those go stale after the first iteration of a
  // cascade that never waits for Grid to re-render in between.
  const onSelectCandidate = async (sourceNode: NodeItem, kept: Asset, others: Asset[]): Promise<NodeItem | undefined> => {
    const originalTrackId = sourceNode.track_id;
    const originalStepIndex = sourceNode.step_index;
    let relocatedPicker: NodeItem | undefined;

    if (others.length === 0) {
      await settleSoleCandidate(sourceNode);
    } else {
      // Attribute the branch to whatever actually produced these candidates,
      // not to sourceNode itself: usually the preceding workflow cell in the
      // same track ("Create image"). But sourceNode may itself be a leftover
      // picker from an earlier split in a "Select all" cascade -- it has
      // nothing before it in its own (freshly created) track, so in that
      // case carry forward whatever cause its *own* track was already
      // spawned from, rather than re-deriving one from this intermediate
      // cell. That keeps every arrow in a cascade pointing back at the one
      // true source instead of chaining picker -> picker -> picker.
      const liveState = useProjectStore.getState();
      const precedingWorkflow = Object.values(liveState.nodesById).find(
        (n) => n.track_id === originalTrackId && n.step_index === originalStepIndex - 1 && n.kind === "workflow",
      );
      const ownTrack = liveState.tracks.find((t) => t.id === originalTrackId);
      const causeNodeId = precedingWorkflow?.id ?? ownTrack?.spawned_from_node_id ?? sourceNode.id;

      const liveTracks = liveState.tracks;
      const rowIndex = liveTracks.length === 0 ? 0 : Math.max(...liveTracks.map((t) => t.row_index)) + 1;
      const newTrack = await tracksApi.create({
        project_id: projectId,
        row_index: rowIndex,
        spawned_from_node_id: causeNodeId,
        spawned_from_output_id: kept.id,
      });
      addTrack(newTrack);

      // 1) Create the settled single-asset node in the vacated original cell...
      // carrying sourceNode's OWN created_by_node_id forward (not sourceNode
      // itself -- an asset.select picker isn't a workflow, it has no
      // step_index+1 of its own to be a creator of). This settled node is
      // just as much the original workflow's output as the picker it's
      // replacing, so it stays bound to the same creator (see
      // _ensure_output_binding) rather than becoming a free-floating,
      // unbound asset just because it happened to be created here instead
      // of by _get_or_create_output_asset_node.
      const settledNode = await nodesApi.create({
        track_id: originalTrackId,
        step_index: originalStepIndex,
        kind: "asset",
        node_type: "asset.single",
        created_by_node_id: sourceNode.created_by_node_id,
      });
      addNode(settledNode);
      await assetsApi.move(kept.id, settledNode.id);
      await refreshNodeOutputs(settledNode.id);

      // 2) ...then relocate sourceNode (still holding `others`) into the new
      // track. Only after the kept asset is safely off of it -- otherwise a
      // crash between these two steps would leave the kept asset attached
      // to a node about to be moved out from under the user's expectation
      // of "this cell now shows the picked image".
      const movedSource = await nodesApi.update(sourceNode.id, { track_id: newTrack.id });
      addNode(movedSource);
      await refreshNodeOutputs(movedSource.id);

      relocatedPicker = movedSource;
    }

    const workflowStepIndex = originalStepIndex + 1;
    const liveNodes = Object.values(useProjectStore.getState().nodesById);
    const hasNextStep = liveNodes.some((n) => n.track_id === originalTrackId && n.step_index === workflowStepIndex);
    if (!hasNextStep) {
      const workflowCell = await nodesApi.create({ track_id: originalTrackId, step_index: workflowStepIndex, kind: "workflow" });
      addNode(workflowCell);
    }

    return relocatedPicker;
  };

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
        alert("This cell has no outputs yet.");
        return;
      }
      setComparePair([compareFor.asset, asset]);
      setCompareFor(null);
    }
  };

  const onStartCompare = (node: NodeItem, asset: Asset) => setCompareFor({ nodeId: node.id, asset });

  // Resolves the track at `row` and reassigns `node`'s track_id AND
  // step_index to land it exactly at (row, step) -- there's no
  // display-only position, only track_id/step_index. Whether it's still
  // "this workflow's output" or gets the manual-placement badge (📌, see
  // isWorkflowOutput) falls out of this purely positionally, same as
  // everything else in the row-span paradigm -- nothing extra to track
  // here for that.
  const moveAssetTo = async (node: NodeItem, row: number, step: number): Promise<NodeItem> => {
    const targetTrack = trackByRowIndex.get(row);
    if (!targetTrack) throw new Error(`No track at row ${row}`);
    const updated = await nodesApi.update(node.id, { track_id: targetTrack.id, step_index: step });
    addNode(updated);
    return updated;
  };

  // Two nodes can't both sit at each other's (track, step) at once, so
  // trading places needs a scratch track as a temporary parking spot: park
  // b there, move a into b's old spot, move b (from the scratch track) into
  // a's old spot, then remove the scratch track. Carries step_index along
  // with track_id -- swapping two assets in different columns is just as
  // valid a drag target as the same-column reorder this was originally
  // written for.
  const swapAssetPositions = async (a: NodeItem, b: NodeItem): Promise<void> => {
    const aTrackId = a.track_id;
    const aStep = a.step_index;
    const bTrackId = b.track_id;
    const bStep = b.step_index;
    const scratchRowIndex = tracks.length === 0 ? 0 : Math.max(...tracks.map((t) => t.row_index)) + 1;
    const scratch = await tracksApi.create({ project_id: projectId, row_index: scratchRowIndex });
    addTrack(scratch);
    await nodesApi.update(b.id, { track_id: scratch.id }).then(addNode);
    await nodesApi.update(a.id, { track_id: bTrackId, step_index: bStep }).then(addNode);
    await nodesApi.update(b.id, { track_id: aTrackId, step_index: aStep }).then(addNode);
    await tracksApi.remove(scratch.id);
    removeTrack(scratch.id);
  };

  // Manual drag of an asset node onto any other empty cell -- a real
  // track_id/step_index reassignment (moveAssetTo), so the view never has
  // anything to say about a node's position that the model doesn't already
  // say too. Not limited to the source or destination being within some
  // workflow's span: an asset dropped somewhere no workflow claims as its
  // own input/output just renders with the manual-placement badge
  // (isWorkflowOutput is purely positional, see the render below), same as
  // one dropped inside a span but not aligned with the workflow's actual
  // materialized output. If the target cell is already taken by another
  // repositionable asset, this is just a reorder -- swap the two positions
  // (swapAssetPositions) rather than treating it as a collision. RefAsset is
  // a separate, deliberate action ("+ ref elsewhere") for pointing at an
  // asset from a second place without moving the original, not something
  // drag-and-drop falls back to.
  const dropAssetAt = async (targetRow: number, targetStep: number) => {
    const draggedId = draggingAssetId;
    setDraggingAssetId(null);
    if (!draggedId) return;
    const dragged = nodesById[draggedId];
    if (!dragged || dragged.kind !== "asset") return;

    const draggedRow = rowIndexOfTrack(dragged.track_id);
    if (targetRow === draggedRow && targetStep === dragged.step_index) return;

    // Refuse to start a second structural op while one's still applying its
    // (possibly multi-node) sequence of updates -- both would plan against
    // the same pre-operation snapshot and could independently decide the
    // same target cell is free, landing two different nodes on top of each
    // other once both finish (2026-07-17 incident: two different RefAsset
    // nodes both ended up at the same row because two drags overlapped).
    if (structuralOpRef.current) {
      alert("Another move is still in progress -- try again in a moment.");
      return;
    }
    structuralOpRef.current = true;
    try {
      if (!isPositionAllowedFor(dragged, targetRow, targetStep)) {
        alert("This asset is a workflow output -- it can only move among its own creator's positions.");
        return;
      }

      const occupant = nodesByRowStep.get(`${targetRow}:${targetStep}`);
      if (occupant && occupant.id !== dragged.id) {
        if (occupant.kind !== "asset" || occupant.node_type === "asset.select") {
          alert("Can't swap with that cell.");
          return;
        }
        if (!isPositionAllowedFor(occupant, draggedRow, dragged.step_index)) {
          alert("Can't swap -- the other cell's asset is a workflow output tied to a different position.");
          return;
        }
        await swapAssetPositions(dragged, occupant);
        return;
      }

      await moveAssetTo(dragged, targetRow, targetStep);
    } finally {
      structuralOpRef.current = false;
    }
  };


  // Freshly computed from live store state rather than this render's
  // memoized nodesByRowStep/rowSpanByNode/effectiveRow -- needed because
  // dropWorkflowAt can run applyColumnMove immediately followed by
  // applyRowMove within the same call, and the row phase must see what the
  // column phase just wrote (the component's own memoized values won't
  // reflect that until the next render). Using the stale closures there
  // made the row phase compute dependents/collisions against pre-column-move
  // positions -- silently missing the real dependents (or grabbing whatever
  // unrelated node happened to sit at that stale key) and applying the move
  // partially before a resulting backend 409 aborted the rest (2026-07-17
  // incident: a diagonal drag left an input asset stranded one column off,
  // on the wrong side of its workflow node).
  const liveGridSnapshot = () => {
    const { nodesById: liveNodesById, tracks: liveTracks } = useProjectStore.getState();
    const rowIndexOf = (trackId: string): number => liveTracks.find((t) => t.id === trackId)?.row_index ?? 0;
    const byRowStep = new Map<string, NodeItem>();
    for (const node of Object.values(liveNodesById)) {
      byRowStep.set(`${rowIndexOf(node.track_id)}:${node.step_index}`, node);
    }
    const spanOf = (node: NodeItem): number => {
      if (node.kind !== "workflow") return 1;
      const template = templates.find((t) => t.node_type === node.node_type);
      const inputSlots = template ? slotFields(template.param_schema).length : 0;
      const spawnedRows = liveTracks.filter((t) => t.spawned_from_node_id === node.id).length;
      const desired = Math.max(inputSlots, 1 + spawnedRows, 1);
      const start = rowIndexOf(node.track_id);
      let span = 1;
      while (span < desired && !byRowStep.has(`${start + span}:${node.step_index}`)) span++;
      return span;
    };
    return { tracks: liveTracks, rowIndexOf, byRowStep, spanOf };
  };

  // Row-only relocation at the node's CURRENT column (used standalone for a
  // vertical-only drag, and as phase 2 of the full 2D move below, run after
  // any column phase has already landed the node on its target column).
  const applyRowMove = async (workflowNode: NodeItem, targetRow: number) => {
    const live = liveGridSnapshot();
    const originalRow = live.rowIndexOf(workflowNode.track_id);
    if (targetRow === originalRow) return;
    const delta = targetRow - originalRow;
    const span = live.spanOf(workflowNode);

    const dependents: NodeItem[] = [];
    for (let r = originalRow; r < originalRow + span; r++) {
      for (const step of [workflowNode.step_index - 1, workflowNode.step_index + 1]) {
        const n = live.byRowStep.get(`${r}:${step}`);
        if (n) dependents.push(n);
      }
    }
    const moves = [
      { node: workflowNode, targetRow },
      ...dependents.map((n) => ({ node: n, targetRow: live.rowIndexOf(n.track_id) + delta })),
    ];
    const movingIds = new Set(moves.map((m) => m.node.id));
    const blocked = moves.some(({ node, targetRow: tr }) => {
      const occupant = live.byRowStep.get(`${tr}:${node.step_index}`);
      return occupant && !movingIds.has(occupant.id);
    });

    if (blocked) {
      // Only a clean move -- destination range doesn't overlap the node's
      // current one at all -- gets pushed through, in either direction:
      // insert `span` new empty tracks right at targetRow (same mechanism
      // as the auto-expand effect above), shifting whatever's there, and
      // everything below it (including, for an upward move, this node's
      // own current position), down to make room. A move that overlaps
      // its own current span is refused instead: inserting rows there
      // would shift some of the very nodes this plan is about to move by
      // an amount this plan didn't account for, corrupting it.
      const overlapsOwnSpan = targetRow < originalRow + span && targetRow + span > originalRow;
      if (overlapsOwnSpan) {
        alert("Can't move there -- there isn't empty room for the node and everything currently aligned to it.");
        return;
      }
      await insertTracksAt(targetRow, span);
    }

    // Read live track state -- insertTracksAt may have just created the
    // tracks these targets need, and shifted row_index on existing ones;
    // this closure's `tracks` won't reflect that until the next render.
    const liveTrackByRow = new Map(useProjectStore.getState().tracks.map((t) => [t.row_index, t]));

    // Every node in `moves` shifts by the same `delta`, so applying them in
    // the wrong order can send one PATCH straight at a cell another moving
    // node hasn't vacated yet -- the backend's own slot-uniqueness guard
    // (api/routes/nodes.py's _ensure_slot_free) then 409s, aborting the loop
    // partway through with some nodes moved and others stranded (2026-07-17
    // incident: moving a workflow node with a real generated output landed
    // an input on the output's still-occupied old cell, aborting before the
    // output itself got a turn to move out of the way). Processing
    // furthest-along-the-shift-first (descending current row for a downward
    // move, ascending for an upward one) guarantees each node's target is
    // already empty by the time its own PATCH runs, since nothing further
    // back in the direction of travel can be a live occupant of a cell
    // something further ahead hasn't already left.
    // The workflow node itself always goes first, ahead of the vacate-order
    // rule below -- a dependent that's this workflow's own materialized
    // output is checked server-side against its creator's CURRENT (DB) step
    // (_ensure_output_binding in nodes.py), so if the output's PATCH lands
    // before the workflow's own, the backend still sees the old creator
    // position and 409s the output's move as "not its creator's own
    // position" (2026-07-18 incident: moving a workflow node with a
    // materialized output rejected the output's own move this way).
    const orderedMoves = [...moves].sort((a, b) => {
      if (a.node.id === workflowNode.id) return -1;
      if (b.node.id === workflowNode.id) return 1;
      const ra = live.rowIndexOf(a.node.track_id);
      const rb = live.rowIndexOf(b.node.track_id);
      return delta > 0 ? rb - ra : ra - rb;
    });

    for (const { node, targetRow: tr } of orderedMoves) {
      const targetTrack = liveTrackByRow.get(tr);
      if (!targetTrack) continue;
      const updated = await nodesApi.update(node.id, { track_id: targetTrack.id });
      addNode(updated);
    }
  };

  // Column relocation: moves a workflow node (and everything currently
  // sitting in its input/output columns within its row-span) sideways by an
  // even delta -- odd deltas would land it on an asset-parity column,
  // breaking the project-wide kind alternation every other column relies on.
  // Returns the node's fresh state on success, or null if refused (either
  // the destination overlaps the node's own current column range, or the
  // only way to clear it would split some OTHER workflow's own
  // input/self/output triple -- see wouldSplitAnyWorkflow).
  const applyColumnMove = async (workflowNode: NodeItem, targetStep: number): Promise<NodeItem | null> => {
    const originalStep = workflowNode.step_index;
    if (targetStep === originalStep) return workflowNode;
    const stepDelta = targetStep - originalStep;
    if (stepDelta % 2 !== 0) {
      alert("Can only move a workflow node left/right in steps of 2 -- keeps the asset/workflow column pattern intact.");
      return null;
    }

    const live = liveGridSnapshot();
    const row = live.rowIndexOf(workflowNode.track_id);
    const span = live.spanOf(workflowNode);
    const dependents: NodeItem[] = [];
    for (let r = row; r < row + span; r++) {
      for (const step of [originalStep - 1, originalStep + 1]) {
        const n = live.byRowStep.get(`${r}:${step}`);
        if (n) dependents.push(n);
      }
    }
    const moves = [
      { node: workflowNode, step: targetStep },
      ...dependents.map((n) => ({ node: n, step: n.step_index + stepDelta })),
    ];
    const movingIds = new Set(moves.map((m) => m.node.id));
    const blocked = moves.some(({ node, step }) => {
      const r = live.rowIndexOf(node.track_id);
      const occupant = live.byRowStep.get(`${r}:${step}`);
      return occupant && !movingIds.has(occupant.id);
    });

    if (blocked) {
      const originalLo = originalStep - 1;
      const originalHi = originalStep + 1;
      const targetLo = targetStep - 1;
      const targetHi = targetStep + 1;
      const overlapsOwnRange = targetLo <= originalHi && targetHi >= originalLo;
      if (overlapsOwnRange) {
        alert("Can't move there -- there isn't empty room for the node and everything currently aligned to it.");
        return null;
      }
      const insertPos = stepDelta > 0 ? originalHi + 1 : targetLo;
      const insertCount = Math.abs(stepDelta);
      if (wouldSplitAnyWorkflow(insertPos, workflowNode.id)) {
        alert("Can't move there -- it would split another workflow node from its own input/output column.");
        return null;
      }
      await insertColumnsAt(insertPos, insertCount);
    }

    // Same ordering hazard applyRowMove's own comment describes: every node
    // here shifts by the same stepDelta, and a workflow's input column
    // (step-1) sits exactly 2 apart from its output column (step+1) -- the
    // smallest stepDelta this function even allows -- so a plain left/right
    // nudge of a node that already has a real generated output routinely
    // sends its input straight at the cell its OWN output hasn't vacated
    // yet. Furthest-along-the-shift-first ordering (descending current step
    // for a rightward move, ascending for a leftward one) avoids it the
    // same way. But the workflow node itself must still go first, ahead of
    // that rule -- see applyRowMove's identical guard for why (its
    // materialized output is checked server-side against the creator's
    // CURRENT DB step, which is stale until the workflow's own PATCH lands).
    const orderedMoves = [...moves].sort((a, b) => {
      if (a.node.id === workflowNode.id) return -1;
      if (b.node.id === workflowNode.id) return 1;
      return stepDelta > 0 ? b.node.step_index - a.node.step_index : a.node.step_index - b.node.step_index;
    });

    let fresh: NodeItem = workflowNode;
    for (const { node, step } of orderedMoves) {
      const updated = await nodesApi.update(node.id, { step_index: step });
      addNode(updated);
      if (node.id === workflowNode.id) fresh = updated;
    }
    return fresh;
  };

  // Diagonal relocation: row AND column both change in the same drop.
  // Checked and applied against the TRUE final destination directly, in one
  // pass -- not decomposed into a row phase and a column phase the way a
  // pure single-axis move is (applyRowMove/applyColumnMove below), because
  // each phase of that decomposition only knows how to check room at an
  // INTERMEDIATE resting spot (wherever the node is after phase one), not
  // the actual target. That mismatch could reject a genuinely free
  // destination, or -- worse -- "fix" a phantom collision at the
  // intermediate spot by inserting new tracks there, shifting unrelated
  // tracks' unrelated cells down even though the real target had room all
  // along (2026-07-17 incident: dropping a workflow node diagonally onto
  // two empty target rows still pushed OTHER tracks' column-0 cells down a
  // row).
  //
  // DOES still grow room the same way applyRowMove does (insertTracksAt at
  // targetRow), but only once -- against the real target, computed above,
  // never an intermediate one -- so landing a workflow (plus its
  // input/output cells) back onto a fresh, nothing-claims-it-yet pair of
  // rows works the same as creating it there fresh would, even if some
  // unrelated node happens to already sit in one of the cells it needs
  // (2026-07-17: moving a workflow back onto its own original home rows,
  // whose input column had since been claimed by unrelated leftover test
  // nodes, was wrongly refused outright instead of growing room for it).
  const applyDiagonalMove = async (workflowNode: NodeItem, targetRow: number, targetStep: number) => {
    const stepDelta = targetStep - workflowNode.step_index;
    if (stepDelta % 2 !== 0) {
      alert("Can only move a workflow node left/right in steps of 2 -- keeps the asset/workflow column pattern intact.");
      return;
    }

    let live = liveGridSnapshot();
    const originalStep = workflowNode.step_index;
    let originalRow = live.rowIndexOf(workflowNode.track_id);
    const span = live.spanOf(workflowNode);

    const dependents: NodeItem[] = [];
    for (let r = originalRow; r < originalRow + span; r++) {
      for (const step of [originalStep - 1, originalStep + 1]) {
        const n = live.byRowStep.get(`${r}:${step}`);
        if (n) dependents.push(n);
      }
    }

    const computeMoves = () => {
      const rowDelta = targetRow - originalRow;
      return [
        { node: workflowNode, row: targetRow, step: targetStep },
        ...dependents.map((n) => ({
          node: n,
          row: live.rowIndexOf(n.track_id) + rowDelta,
          step: n.step_index + stepDelta,
        })),
      ];
    };
    const isBlocked = (moves: ReturnType<typeof computeMoves>) => {
      const movingIds = new Set(moves.map((m) => m.node.id));
      return moves.some(({ row, step }) => {
        const occupant = live.byRowStep.get(`${row}:${step}`);
        return occupant && !movingIds.has(occupant.id);
      });
    };

    let moves = computeMoves();

    if (isBlocked(moves)) {
      // Growing room by inserting tracks is only for the FIRST workflow
      // column in the project's pattern (step 0 or 1, whichever
      // kindForStep says is workflow for this project's start_kind) --
      // that's the one lane a fresh multi-row workflow naturally lives in
      // from the start, so treating a move back onto an unclaimed pair of
      // rows there like a fresh placement is legitimate. Any later
      // workflow column (step 3, 5, ...) is a branch/secondary point, not
      // a primary lane -- growing the whole grid there as a side effect of
      // one drag would be surprising, so it gets a strict fits-or-refused
      // check instead, same as applyDiagonalMove's blocked case used to be
      // for every column before this.
      const firstWorkflowStep = project?.start_kind === "workflow" ? 0 : 1;
      if (targetStep !== firstWorkflowStep) {
        alert("Can't move there -- something's already in the way. Pick an empty spot the same size as this node's own span.");
        return;
      }
      // Only safe to grow room for: the destination span doesn't overlap
      // the node's own current one at all (same guard applyRowMove uses) --
      // inserting there would otherwise shift some of the very nodes this
      // plan is about to move by an amount it didn't account for.
      const overlapsOwnSpan = targetRow < originalRow + span && targetRow + span > originalRow;
      if (overlapsOwnSpan) {
        alert("Can't move there -- there isn't empty room for the node and everything currently aligned to it.");
        return;
      }
      await insertTracksAt(targetRow, span);
      live = liveGridSnapshot();
      originalRow = live.rowIndexOf(workflowNode.track_id);
      moves = computeMoves();
      if (isBlocked(moves)) {
        alert("Can't move there -- still something in the way even after making room.");
        return;
      }
    }

    const liveTrackByRow = new Map(live.tracks.map((t) => [t.row_index, t]));
    if (moves.some(({ row, step }) => step < 0 || !liveTrackByRow.has(row))) {
      alert("Can't move there.");
      return;
    }

    // Same ordering hazard as applyRowMove/applyColumnMove: every node here
    // shifts by the same (rowDelta, stepDelta) pair, so a naive application
    // order can PATCH a node straight at a cell another moving node hasn't
    // vacated yet. Sort by how far each node's CURRENT position projects
    // onto the shift direction, descending -- the node furthest along the
    // direction of travel has nothing else in this batch still standing on
    // its target, so it can always go first; working backwards from there,
    // by the time any other node's turn comes, whatever used to be at its
    // target (if anything in this batch) has already moved out.
    // The workflow node itself must still go first, ahead of this projection
    // order -- see applyRowMove's identical guard for why (a materialized
    // output dependent is checked server-side against its creator's CURRENT
    // DB step, stale until the workflow's own PATCH lands).
    const rowDelta = targetRow - originalRow;
    const orderedMoves = [...moves].sort((a, b) => {
      if (a.node.id === workflowNode.id) return -1;
      if (b.node.id === workflowNode.id) return 1;
      const projA = live.rowIndexOf(a.node.track_id) * rowDelta + a.node.step_index * stepDelta;
      const projB = live.rowIndexOf(b.node.track_id) * rowDelta + b.node.step_index * stepDelta;
      return projB - projA;
    });

    for (const { node, row, step } of orderedMoves) {
      const targetTrack = liveTrackByRow.get(row)!;
      const updated = await nodesApi.update(node.id, { track_id: targetTrack.id, step_index: step });
      addNode(updated);
    }
  };

  const dropWorkflowAt = async (workflowNode: NodeItem, targetRow: number, targetStep: number) => {
    // See dropAssetAt's comment -- never let a second structural op start
    // while one's still applying its updates.
    if (structuralOpRef.current) {
      alert("Another move is still in progress -- try again in a moment.");
      return;
    }
    const rowChanged = targetRow !== effectiveRow(workflowNode);
    const stepChanged = targetStep !== workflowNode.step_index;
    structuralOpRef.current = true;
    try {
      if (rowChanged && stepChanged) {
        await applyDiagonalMove(workflowNode, targetRow, targetStep);
        return;
      }
      let node = workflowNode;
      if (stepChanged) {
        const moved = await applyColumnMove(node, targetStep);
        if (!moved) return;
        node = moved;
      }
      if (rowChanged) {
        await applyRowMove(node, targetRow);
      }
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
  const emptyWorkflowCells = useMemo(() => {
    if (!draggingWorkflowId || !project?.start_kind) return [];
    const cells: { row: number; step: number }[] = [];
    for (const row of tracks.map((t) => t.row_index)) {
      for (let step = 0; step <= maxButtonStep; step++) {
        if (kindForStep(project.start_kind, step) !== "workflow") continue;
        if (nodesByRowStep.has(`${row}:${step}`)) continue;
        cells.push({ row, step });
      }
    }
    return cells;
  }, [draggingWorkflowId, project, tracks, maxButtonStep, nodesByRowStep]);

  // Same idea as emptyWorkflowCells, but for an asset drag: every currently-
  // empty, asset-parity cell across the WHOLE grid, not just the ones
  // reachable from some workflow's own span (emptyReachableCells, which
  // keeps rendering its own "+ asset" button there regardless of a drag, so
  // it's excluded here to avoid stacking two drop targets on the same
  // cell). A manually-placed asset isn't tied to any one workflow's
  // territory -- moveAssetTo/isWorkflowOutput decide what a cell means
  // purely from where it ends up, not from where it started -- so dragging
  // one to an unrelated empty cell elsewhere on the board has to actually
  // be reachable to drop on (2026-07-18: dragging an asset out to a cell no
  // workflow's span reached did nothing at all -- no drop target was even
  // rendered there).
  const emptyAssetDropCells = useMemo(() => {
    if (!draggingAssetId || !project?.start_kind) return [];
    const reachable = new Set(emptyReachableCells.map(({ row, step }) => `${row}:${step}`));
    const cells: { row: number; step: number }[] = [];
    for (const row of tracks.map((t) => t.row_index)) {
      for (let step = 0; step <= maxButtonStep; step++) {
        if (kindForStep(project.start_kind, step) !== "asset") continue;
        if (nodesByRowStep.has(`${row}:${step}`)) continue;
        if (reachable.has(`${row}:${step}`)) continue;
        cells.push({ row, step });
      }
    }
    return cells;
  }, [draggingAssetId, project, tracks, maxButtonStep, nodesByRowStep, emptyReachableCells]);

  // Places a RefAsset node (a lightweight pointer, not a copy) at (row, step),
  // referencing sourceNode's currently resolved output -- used both by the
  // drag-collision fallback above and by "+ ref elsewhere"'s click-to-complete
  // gesture below.
  const createRefAssetAt = async (sourceNode: NodeItem, row: number, step: number) => {
    const targetTrack = trackByRowIndex.get(row);
    if (!targetTrack) return;
    const asset = await resolvePrimaryOutput(sourceNode);
    if (!asset) {
      alert("This asset has no resolved output yet -- can't reference it.");
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

  const onStartRef = (node: NodeItem) => setRefFor({ nodeId: node.id });

  const completeRefAt = async (row: number, step: number) => {
    if (!refFor) return;
    const sourceNode = nodesById[refFor.nodeId];
    setRefFor(null);
    if (!sourceNode) return;
    await createRefAssetAt(sourceNode, row, step);
  };

  // Closes the gap between what a workflow node wants (desiredRowSpanByNode)
  // and what currently fits without overlapping something else
  // (rowSpanByNode) by inserting real track rows -- a node that doesn't fit
  // grows its own room instead of just quietly rendering smaller than it
  // needs. Fixes one node's gap per effect run (insertTracksAt's own writes
  // change `tracks`, which re-fires this effect for whatever gap is left,
  // one at a time, rather than computing every gap against a single
  // about-to-be-stale snapshot of row_index values).
  useEffect(() => {
    if (!templatesLoaded || structuralOpRef.current) return;
    for (const node of Object.values(nodesById)) {
      if (node.kind !== "workflow") continue;
      const desired = desiredRowSpanByNode.get(node.id) ?? 1;
      const achieved = rowSpanByNode.get(node.id) ?? 1;
      if (achieved >= desired) continue;
      const start = effectiveRow(node);
      structuralOpRef.current = true;
      insertTracksAt(start + achieved, desired - achieved).finally(() => {
        structuralOpRef.current = false;
      });
      break;
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nodesById, desiredRowSpanByNode, rowSpanByNode, tracks, templatesLoaded]);

  return (
    <div className="main-area" ref={containerRef}>
      <div className="grid-wrapper">
        <ArrowsOverlay edges={edges} cellRefs={cellRefs} containerRef={containerRef} deps={[nodesById, tracks]} />
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
          {sortedTracks.map((track, rowIdx) => (
            <div
              key={`label-${track.id}`}
              className="track-label"
              style={{ gridColumn: 1, gridRow: rowIdx + 1, display: "flex", alignItems: "center", justifyContent: "space-between" }}
            >
              track {track.row_index}
              <div style={{ display: "flex", gap: 4 }}>
                <button
                  onClick={() => addTrackAbove(track.id)}
                  title="Insert a new empty track above this one"
                  style={{ fontSize: 10, padding: "1px 4px" }}
                >
                  + track above
                </button>
                <button onClick={() => deleteTrackRow(track.id)} title="Delete this track and all its cells" style={{ fontSize: 10, padding: "1px 4px" }}>
                  ×
                </button>
              </div>
            </div>
          ))}

          {sortedTracks.map((track) => {
            const trackNodes = nodesByTrack.get(track.id) ?? [];
            const lastNodeId = trackNodes[trackNodes.length - 1]?.id;
            return trackNodes.map((node) => {
              const row = effectiveRow(node);
              const gridRow = node.kind === "workflow" ? `${row + 1} / span ${rowSpanByNode.get(node.id) ?? 1}` : row + 1;
              // A still-undecided candidates picker ("asset.select") has no
              // single well-defined picture to grab (same reason isPickable
              // above excludes it). A refasset still occupies a real (row,
              // column) slot like any other asset node, so it's just as
              // repositionable -- only dragging is meaningless for workflow
              // cells (their own start row isn't draggable, only their span
              // grows/shrinks automatically).
              const isDraggableAsset = node.kind === "asset" && node.node_type !== "asset.select";
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
                  style={{ gridColumn: node.step_index + 2, gridRow, display: "flex" }}
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
                    isLastInTrack={node.id === lastNodeId}
                    isManualPlacement={isDraggableAsset && node.step_index > 0 && !isWorkflowOutput(node)}
                    isRefSource={refFor?.nodeId === node.id}
                    registerRef={registerRef}
                    onCellClicked={onCellClicked}
                    onSelectCandidate={onSelectCandidate}
                    onStartCompare={onStartCompare}
                    onStartRef={onStartRef}
                  />
                </div>
              );
            });
          })}

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
                <button style={{ fontSize: 10, padding: "1px 4px" }} title="Place the reference here">
                  place ref here
                </button>
              ) : (
                <button
                  style={{ fontSize: 10, padding: "1px 4px", opacity: 0.6 }}
                  title="Load an asset here manually -- not tied to the adjacent workflow node's output"
                  onClick={async () => {
                    const targetTrack = trackByRowIndex.get(row);
                    if (!targetTrack) return;
                    const created = await nodesApi.create({ track_id: targetTrack.id, step_index: step, kind: "asset" });
                    addNode(created);
                  }}
                >
                  + asset
                </button>
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
              }}
              onDragOver={(e) => e.preventDefault()}
              onDrop={(e) => {
                e.preventDefault();
                const dragged = draggingWorkflowId ? nodesById[draggingWorkflowId] : null;
                setDraggingWorkflowId(null);
                if (dragged) dropWorkflowAt(dragged, row, step);
              }}
            />
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
            // A workflow cell always grows its own next step on its own --
            // draft (choose a template, generate) or done (its output already
            // materialized the following asset cell) -- so "+ step" would
            // only ever add a premature, redundant slot after it. It's only
            // ever needed after a manually-filled asset cell (no generation
            // involved, so nothing auto-chains forward from it).
            const lastNode = (nodesByTrack.get(track.id) ?? []).slice(-1)[0];
            const canAddStep = !showStartChoice && lastNode?.kind === "asset";
            // A track with no cells yet (freshly added via "+ New track")
            // has nothing to auto-chain its first cell either -- same
            // problem as the asset case above, just for whichever column its
            // button currently sits on. Unlike the very first track in the
            // project, it's not a free choice: start_kind already fixes what
            // kind every column is, so show the one button that matches
            // instead of asking -- but "empty" lets the user push that
            // button (and the kind it offers) out to the next column, for
            // tracks that shouldn't start where the pattern says.
            const rawButtonStep = !showStartChoice && !lastNode ? nextStepIndex(track.id) + (emptyTrackSkip[track.id] ?? 0) : nextStepIndex(track.id);
            // If this track's own next slot lands inside another track's
            // spanning workflow card (blockingNodeByCell), auto-advance past it
            // -- that cell is already visually and physically taken, so
            // offering a button there would let the user create a node right
            // on top of it.
            const buttonStep = showStartChoice
              ? rawButtonStep
              : nextFreeStep(track.row_index, rawButtonStep, canAddStep ? "workflow" : undefined);
            const emptyTrackKind = !showStartChoice && !lastNode ? kindForStep(project!.start_kind!, buttonStep) : null;
            const skipColumn = () => setEmptyTrackSkip((prev) => ({ ...prev, [track.id]: (prev[track.id] ?? 0) + 1 }));
            return (
              <div
                key={`add-${track.id}`}
                style={{ gridColumn: buttonStep + 2, gridRow: rowIdx + 1, alignSelf: "center", display: "flex", gap: 4 }}
              >
                {showStartChoice ? (
                  <>
                    <button onClick={() => addStep(track.id, "asset")} title="First column in this project: asset cells (manual upload)">
                      + start with asset
                    </button>
                    <button onClick={() => addStep(track.id, "workflow")} title="First column in this project: workflow cells">
                      + start with workflow
                    </button>
                  </>
                ) : canAddStep ? (
                  <button onClick={() => addStep(track.id, undefined, buttonStep)} title="Add a workflow cell after this manually-filled asset cell">
                    + step
                  </button>
                ) : emptyTrackKind === "asset" ? (
                  refFor ? (
                    <button onClick={() => completeRefAt(track.row_index, buttonStep)} title="Place the reference here">
                      place ref here
                    </button>
                  ) : (
                    <>
                      <button onClick={() => addStep(track.id, "asset", buttonStep)} title="This column in this track: asset cell (manual upload)">
                        + asset
                      </button>
                      <button onClick={skipColumn} title="Leave this column blank for this track and offer the next one instead">
                        empty
                      </button>
                    </>
                  )
                ) : emptyTrackKind === "workflow" ? (
                  <>
                    <button onClick={() => addStep(track.id, "workflow", buttonStep)} title="This column in this track: workflow cell">
                      + step
                    </button>
                    <button onClick={skipColumn} title="Leave this column blank for this track and offer the next one instead">
                      empty
                    </button>
                  </>
                ) : null}
              </div>
            );
          })}
        </div>

        <div style={{ marginTop: 16 }}>
          <button onClick={addTrackRow}>+ New track</button>
          {compareFor && (
            <button style={{ marginLeft: 8 }} onClick={() => setCompareFor(null)}>
              Cancel comparing
            </button>
          )}
          {refFor && (
            <button style={{ marginLeft: 8 }} onClick={() => setRefFor(null)}>
              Cancel reference
            </button>
          )}
        </div>
      </div>

      {comparePair && <CompareModal left={comparePair[0]} right={comparePair[1]} onClose={() => setComparePair(null)} />}
    </div>
  );
}
